/**
 * Capability gate policy panel -- nanoinfraorg/nanoinfra#26.
 *
 * The panel shows the effective policy from config, plus the origin of each value. A default
 * must not look like a choice, so every value that comes from a shipped default carries a
 * marker. An empty table states what the deployment refuses, because the restrictive default is
 * the policy. The `all` scope has one legal value, so the panel renders fixed text for it.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { CircleAlert, Info, Loader2, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { updateGatesPolicy } from "@/lib/api";
import type {
  GatesIdentity,
  GatesPayload,
  GatesPolicy,
  GatesStandingGrant,
  SettingsPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type ContextKey = "interactive" | "unattended";
type ScopeField = "host" | "group" | "all";

const CONTEXT_KEYS: ContextKey[] = ["interactive", "unattended"];
const SCOPE_FIELDS: ScopeField[] = ["host", "group", "all"];

/**
 * The approver forms, and the one this row takes -- nanoinfraorg/nanoinfra#71.
 *
 * The first two are forms. The last three are mistakes the panel can name before a save.
 */
type ApproverShape = "webuiForm" | "chatForm" | "bareClaim" | "notAnAccountId" | "blank";

/** The WebUI path, which is also the whole actor of a deployment with no proxy. */
const WEBUI_CHANNEL = "webui";

/**
 * The channels whose account id is a number. Telegram is the one the gate documents, so it is
 * the one this panel checks. Every other channel gets the form and no check: a warning about a
 * shape nobody stated would teach an operator to ignore this line.
 */
const NUMERIC_ACCOUNT_ID_CHANNELS = new Set(["telegram"]);

/**
 * Read the shape of one approver row.
 *
 * The panel adds no prefix. The gate compares the whole string and strips nothing (#66), so an
 * operator has to be able to read this list and predict the match. A panel that completed the
 * value would save a string the operator never read.
 */
export function approverShape(channel: string, sender: string): ApproverShape {
  const name = channel.trim();
  const value = sender.trim();
  if (!value) return "blank";
  if (name === WEBUI_CHANNEL) {
    // ``webui`` is the whole actor of a deployment with no proxy, and not an unfinished value.
    if (value === WEBUI_CHANNEL) return "webuiForm";
    const claim = value.startsWith(`${WEBUI_CHANNEL}:`)
      ? value.slice(WEBUI_CHANNEL.length + 1).trim()
      : "";
    return claim ? "webuiForm" : "bareClaim";
  }
  if (NUMERIC_ACCOUNT_ID_CHANNELS.has(name)) {
    return /^-?\d+$/.test(value) ? "chatForm" : "notAnAccountId";
  }
  return "chatForm";
}

/** Read the gate block. An older gateway sends no block, and then the panel stays away. */
export function gatesPayloadFrom(settings: SettingsPayload): GatesPayload | null {
  const gates = settings.advanced.gates;
  if (!gates || !gates.policy || !gates.choices) return null;
  return gates;
}

function clonePolicy(policy: GatesPolicy): GatesPolicy {
  return JSON.parse(JSON.stringify(policy)) as GatesPolicy;
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function GatesSettings({
  token,
  settings,
  onSaved,
}: {
  token: string;
  settings: SettingsPayload;
  onSaved: (payload: SettingsPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const gates = gatesPayloadFrom(settings);
  const savedJson = useMemo(() => JSON.stringify(gates?.policy ?? null), [gates?.policy]);
  const [draft, setDraft] = useState<GatesPolicy | null>(() =>
    gates ? clonePolicy(gates.policy) : null,
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // The unattended decisions a pending save would widen to `allow`, or null when it widens none.
  const [pendingWidening, setPendingWidening] = useState<string[] | null>(null);
  const [grantDraft, setGrantDraft] = useState<GatesStandingGrant | null>(null);

  // A new payload replaces the draft. The saved flag survives, because the parent applies the
  // saved payload right after a save, and the operator must still read that result.
  useEffect(() => {
    const parsed = JSON.parse(savedJson) as GatesPolicy | null;
    setDraft(parsed ? clonePolicy(parsed) : null);
    setError(null);
  }, [savedJson]);

  if (!gates || !draft) return null;

  const savedPolicy = gates.policy;
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedPolicy);

  /** A row keeps the marker while it still holds the saved default value. */
  const isDefault = (path: string, current: unknown, previous: unknown): boolean =>
    gates.from_default[path] === true && sameValue(current, previous);

  const updateDraft = (change: (previous: GatesPolicy) => GatesPolicy) => {
    setDraft((previous) => (previous ? change(previous) : previous));
    setSaved(false);
    setError(null);
  };

  const setScopeDecision = (context: ContextKey, field: ScopeField, value: string) => {
    updateDraft((previous) => ({
      ...previous,
      [context]: {
        ...previous[context],
        "mutate.remote": { ...previous[context]["mutate.remote"], [field]: value },
      },
    }));
  };

  const setContextDecision = (
    context: ContextKey,
    field: "mutate.inventory" | "credential.access",
    value: string,
  ) => {
    updateDraft((previous) => ({
      ...previous,
      [context]: { ...previous[context], [field]: value },
    }));
  };

  const save = async () => {
    setPendingWidening(null);
    setSaving(true);
    setError(null);
    try {
      const payload = await updateGatesPolicy(token, draft);
      setSaved(true);
      onSaved(payload);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const decisionLabel = (value: string): string => {
    const labels: Record<string, string> = {
      allow: tx("settings.gates.decision.allow", "Allow"),
      approve: tx("settings.gates.decision.approve", "Approve"),
      grant: tx("settings.gates.decision.grant", "Grant only"),
      deny: tx("settings.gates.decision.deny", "Deny"),
    };
    return labels[value] ?? value;
  };

  const scopeLabel = (field: ScopeField): string => {
    if (field === "host") return tx("settings.gates.scope.host", "one host");
    if (field === "group") return tx("settings.gates.scope.group", "host group");
    return tx("settings.gates.scope.all", "all hosts");
  };

  const contextLabel = (context: ContextKey): string =>
    context === "interactive"
      ? tx("settings.gates.context.interactive", "Interactive")
      : tx("settings.gates.context.unattended", "Unattended");

  const remoteLabel = tx("settings.gates.rows.remote", "Remote execution");
  const inventoryLabel = tx("settings.gates.rows.inventory", "Inventory writes");
  const secretLabel = tx("settings.gates.rows.secret", "Read a secret");
  const defaultTitle = tx(
    "settings.gates.defaultMarkerTitle",
    "This value comes from a shipped default. No operator set it.",
  );

  /** The unattended decisions this save would widen to `allow`, by their label. */
  const widenedUnattended = (): string[] => {
    const widened: string[] = [];
    const remote = draft.unattended["mutate.remote"];
    const savedRemote = savedPolicy.unattended["mutate.remote"];
    for (const field of SCOPE_FIELDS) {
      if (remote[field] === "allow" && savedRemote[field] !== "allow") {
        widened.push(`${remoteLabel}, ${scopeLabel(field)}`);
      }
    }
    for (const field of ["mutate.inventory", "credential.access"] as const) {
      if (draft.unattended[field] === "allow" && savedPolicy.unattended[field] !== "allow") {
        widened.push(field === "mutate.inventory" ? inventoryLabel : secretLabel);
      }
    }
    return widened;
  };

  const requestSave = () => {
    // One confirmation, and only for the widest value a control here can take. The panel already
    // refuses to widen `all` scope, and an operator changed the unattended column while they meant
    // the interactive one, which let a cron job reach a host with no person present.
    const widened = widenedUnattended();
    if (widened.length > 0) {
      setPendingWidening(widened);
      return;
    }
    void save();
  };

  const grantsInDraft = draft.standingGrants;
  const singlePath = draft.approvalPaths.length < 2;

  const removeGrant = (index: number) => {
    updateDraft((previous) => ({
      ...previous,
      standingGrants: previous.standingGrants.filter((_grant, position) => position !== index),
    }));
  };

  const commitGrant = (grant: GatesStandingGrant) => {
    updateDraft((previous) => ({
      ...previous,
      standingGrants: [
        ...previous.standingGrants,
        {
          contexts: grant.contexts,
          hosts: grant.hosts.map((host) => host.trim()).filter(Boolean),
          commands: grant.commands.map((command) => command.trim()).filter(Boolean),
        },
      ],
    }));
    setGrantDraft(null);
  };

  const statusMessage = error
    ? error
    : saved
      ? tx(
        "settings.gates.status.saved",
        "Saved. The gateway reads the new policy after a restart.",
      )
      : dirty
        ? tx("settings.gates.status.unsaved", "Unsaved policy changes.")
        : undefined;

  return (
    <div className="space-y-7" data-testid="gates-settings">
      <section>
        <GatesTitle>{tx("settings.gates.title", "Capability gates")}</GatesTitle>
        <GatesGroup>
          <div className="overflow-x-auto px-4 py-3.5 sm:px-5">
            <div className="min-w-[32rem]">
              {/*
                The header stays with the rows. A reader who scrolls the panel loses it otherwise,
                and the two decision columns then look alike, which is how a row gets read one place
                shifted.
              */}
              <div className="sticky top-0 z-10 grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] gap-3 bg-card pb-2 pt-1 text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                <span>{tx("settings.gates.columns.policy", "Policy")}</span>
                <span className="flex flex-col gap-0.5">
                  <span>{contextLabel("interactive")}</span>
                  <span className="text-[10.5px] font-normal normal-case tracking-normal">
                    {tx("settings.gates.columns.interactiveNote", "A person typed the request.")}
                  </span>
                </span>
                <span className="flex flex-col gap-0.5">
                  <span>{contextLabel("unattended")}</span>
                  <span className="text-[10.5px] font-normal normal-case tracking-normal text-destructive-text">
                    {tx(
                      "settings.gates.columns.unattendedNote",
                      "No person is present. A cron job, the heartbeat, Dream, and a subagent run here.",
                    )}
                  </span>
                </span>
              </div>
              <div className="divide-y divide-border/45">
                <div className="grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] gap-3 py-2.5">
                  <span className="text-[13.5px] font-medium text-foreground">{remoteLabel}</span>
                  <span />
                  <span />
                </div>
                {SCOPE_FIELDS.map((field) => (
                  <div
                    key={field}
                    className="grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] items-center gap-3 py-2.5"
                    data-testid={`gates-scope-row-${field}`}
                  >
                    <span className="pl-4 text-[13px] text-muted-foreground">
                      {scopeLabel(field)}
                    </span>
                    {CONTEXT_KEYS.map((context) => (
                      <DecisionCell
                        key={context}
                        cellTestId={`gates-cell-${context}`}
                        label={`${remoteLabel}, ${scopeLabel(field)}, ${contextLabel(context)}`}
                        value={draft[context]["mutate.remote"][field]}
                        choices={field === "all" ? gates.choices.all : gates.choices["mutate.remote"]}
                        showDefault={isDefault(
                          `${context}.mutate.remote.${field}`,
                          draft[context]["mutate.remote"][field],
                          savedPolicy[context]["mutate.remote"][field],
                        )}
                        defaultTitle={defaultTitle}
                        defaultLabel={tx("settings.gates.defaultMarker", "default")}
                        fixedSuffix={tx("settings.gates.fixed", "fixed")}
                        toLabel={decisionLabel}
                        onChange={(value) => setScopeDecision(context, field, value)}
                      />
                    ))}
                  </div>
                ))}
                <div className="grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] items-center gap-3 py-2.5">
                  <span className="text-[13.5px] font-medium text-foreground">
                    {inventoryLabel}
                  </span>
                  {CONTEXT_KEYS.map((context) => (
                    <DecisionCell
                      key={context}
                      cellTestId={`gates-cell-${context}`}
                      label={`${inventoryLabel}, ${contextLabel(context)}`}
                      value={draft[context]["mutate.inventory"]}
                      choices={gates.choices["mutate.inventory"]}
                      showDefault={isDefault(
                        `${context}.mutate.inventory`,
                        draft[context]["mutate.inventory"],
                        savedPolicy[context]["mutate.inventory"],
                      )}
                      defaultTitle={defaultTitle}
                      defaultLabel={tx("settings.gates.defaultMarker", "default")}
                      fixedSuffix={tx("settings.gates.fixed", "fixed")}
                      toLabel={decisionLabel}
                      onChange={(value) => setContextDecision(context, "mutate.inventory", value)}
                    />
                  ))}
                </div>
                <div className="grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] items-center gap-3 py-2.5">
                  <span className="text-[13.5px] font-medium text-foreground">{secretLabel}</span>
                  {CONTEXT_KEYS.map((context) => (
                    <DecisionCell
                      key={context}
                      cellTestId={`gates-cell-${context}`}
                      label={`${secretLabel}, ${contextLabel(context)}`}
                      value={draft[context]["credential.access"]}
                      choices={gates.choices["credential.access"]}
                      showDefault={isDefault(
                        `${context}.credential.access`,
                        draft[context]["credential.access"],
                        savedPolicy[context]["credential.access"],
                      )}
                      defaultTitle={defaultTitle}
                      defaultLabel={tx("settings.gates.defaultMarker", "default")}
                      fixedSuffix={tx("settings.gates.fixed", "fixed")}
                      toLabel={decisionLabel}
                      onChange={(value) => setContextDecision(context, "credential.access", value)}
                    />
                  ))}
                </div>
              </div>
            </div>
          </div>
          <GatesNote>
            {tx(
              "settings.gates.notes.allScope",
              "All hosts has one value only. This design has no runtime path to unbounded scope.",
            )}
          </GatesNote>
          <GatesNote>
            {tx(
              "settings.gates.notes.grantDecision",
              "Grant only allows an action when a standing grant matches it. Nothing else allows an unattended remote command.",
            )}
          </GatesNote>
          {singlePath ? (
            <GatesNote tone="warning" testId="gates-single-path-warning">
              {tx(
                "settings.gates.notes.singlePath",
                "Fewer than two authenticated paths are configured. A group action then has no runtime approval path. Add a path below, or declare a standing grant.",
              )}
            </GatesNote>
          ) : null}
        </GatesGroup>
      </section>

      <IdentitySection identity={gates.identity} />

      <section>
        <GatesTitle
          marker={
            isDefault("approvers", draft.approvers, savedPolicy.approvers)
              ? tx("settings.gates.defaultMarker", "default")
              : undefined
          }
          markerTitle={defaultTitle}
        >
          {tx("settings.gates.approvers.title", "Approvers")}
        </GatesTitle>
        <GatesGroup>
          {draft.approvers.length === 0 ? (
            <GatesEmpty testId="gates-approvers-empty">
              {tx(
                "settings.gates.approvers.empty",
                "No approvers. No operator can approve an action at run time. An approve decision then refuses the action.",
              )}
            </GatesEmpty>
          ) : (
            draft.approvers.map((approver, index) => (
              <div key={index} className="px-4 py-3 sm:px-5">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Input
                    value={approver.channel}
                    aria-label={`${tx("settings.gates.approvers.channel", "Channel")} ${index + 1}`}
                    placeholder={tx("settings.gates.approvers.channel", "Channel")}
                    onChange={(event) =>
                      updateDraft((previous) => ({
                        ...previous,
                        approvers: previous.approvers.map((row, position) =>
                          position === index ? { ...row, channel: event.target.value } : row,
                        ),
                      }))
                    }
                    className="h-9 rounded-[10px] text-[13px] sm:w-40"
                  />
                  <Input
                    value={approver.sender}
                    aria-label={`${tx("settings.gates.approvers.sender", "Sender")} ${index + 1}`}
                    placeholder={tx("settings.gates.approvers.sender", "Sender")}
                    onChange={(event) =>
                      updateDraft((previous) => ({
                        ...previous,
                        approvers: previous.approvers.map((row, position) =>
                          position === index ? { ...row, sender: event.target.value } : row,
                        ),
                      }))
                    }
                    className="h-9 flex-1 rounded-[10px] text-[13px]"
                  />
                  <RemoveButton
                    label={
                      `${tx("settings.gates.approvers.remove", "Remove approver")} ${index + 1}`
                    }
                    onClick={() =>
                      updateDraft((previous) => ({
                        ...previous,
                        approvers: previous.approvers.filter(
                          (_row, position) => position !== index,
                        ),
                      }))
                    }
                  />
                </div>
                <ApproverShapeNote
                  index={index}
                  channel={approver.channel}
                  sender={approver.sender}
                />
              </div>
            ))
          )}
          <GatesFooterRow>
            <AddButton
              label={tx("settings.gates.approvers.add", "Add an approver")}
              onClick={() =>
                updateDraft((previous) => ({
                  ...previous,
                  approvers: [...previous.approvers, { channel: "webui", sender: "" }],
                }))
              }
            />
          </GatesFooterRow>
          <GatesNote>
            {tx(
              "settings.gates.approvers.note",
              "Membership in a channel allowFrom list grants nothing here. Only this list decides who can approve.",
            )}
          </GatesNote>
          <GatesNote>
            {tx(
              "settings.gates.approvers.formNote",
              "This panel adds no prefix. The gate compares the whole string, so it matches what you write here and nothing else.",
            )}
          </GatesNote>
        </GatesGroup>
      </section>

      <section>
        <GatesTitle
          marker={
            isDefault("approvalPaths", draft.approvalPaths, savedPolicy.approvalPaths)
              ? tx("settings.gates.defaultMarker", "default")
              : undefined
          }
          markerTitle={defaultTitle}
        >
          {tx("settings.gates.paths.title", "Authenticated paths")}
        </GatesTitle>
        <GatesGroup>
          {draft.approvalPaths.length === 0 ? (
            <GatesEmpty testId="gates-paths-empty">
              {tx(
                "settings.gates.paths.empty",
                "No authenticated paths. No path can carry an approval, so every approve decision refuses the action.",
              )}
            </GatesEmpty>
          ) : (
            draft.approvalPaths.map((path, index) => (
              <div key={index} className="flex items-center gap-2 px-4 py-3 sm:px-5">
                <Input
                  value={path}
                  aria-label={`${tx("settings.gates.paths.field", "Authenticated path")} ${index + 1}`}
                  onChange={(event) =>
                    updateDraft((previous) => ({
                      ...previous,
                      approvalPaths: previous.approvalPaths.map((row, position) =>
                        position === index ? event.target.value : row,
                      ),
                    }))
                  }
                  className="h-9 flex-1 rounded-[10px] text-[13px]"
                />
                <RemoveButton
                  label={`${tx("settings.gates.paths.remove", "Remove path")} ${index + 1}`}
                  onClick={() =>
                    updateDraft((previous) => ({
                      ...previous,
                      approvalPaths: previous.approvalPaths.filter(
                        (_row, position) => position !== index,
                      ),
                    }))
                  }
                />
              </div>
            ))
          )}
          <GatesFooterRow>
            <AddButton
              label={tx("settings.gates.paths.add", "Add a path")}
              onClick={() =>
                updateDraft((previous) => ({
                  ...previous,
                  approvalPaths: [...previous.approvalPaths, ""],
                }))
              }
            />
          </GatesFooterRow>
          <GatesNote>
            {tx(
              "settings.gates.paths.note",
              "A path names a channel with a session concept. Two paths keep one account from both halves of an approval.",
            )}
          </GatesNote>
        </GatesGroup>
      </section>

      <section>
        <GatesTitle
          marker={
            isDefault("standingGrants", grantsInDraft, savedPolicy.standingGrants)
              ? tx("settings.gates.defaultMarker", "default")
              : undefined
          }
          markerTitle={defaultTitle}
        >
          {tx("settings.gates.grants.title", "Standing grants")}
        </GatesTitle>
        <GatesGroup>
          {grantsInDraft.length === 0 ? (
            <GatesEmpty testId="gates-grants-empty">
              {tx(
                "settings.gates.grants.empty",
                "No grants. No automation may run a remote command.",
              )}
            </GatesEmpty>
          ) : (
            grantsInDraft.map((grant, index) => (
              <div
                key={index}
                className="flex flex-col gap-2 px-4 py-3.5 sm:flex-row sm:items-start sm:px-5"
                data-testid={`gates-grant-row-${index}`}
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="text-[13px] font-medium text-foreground">
                    {grant.contexts.join(", ")}
                  </div>
                  <div className="text-[12px] leading-5 text-muted-foreground">
                    {tx("settings.gates.grants.hosts", "Hosts")}: {grant.hosts.join(", ")}
                  </div>
                  <div className="text-[12px] leading-5 text-muted-foreground">
                    {tx("settings.gates.grants.commands", "Commands")}:{" "}
                    <code className="rounded bg-muted px-1 py-0.5">
                      {grant.commands.join(" | ")}
                    </code>
                  </div>
                </div>
                <RemoveButton
                  label={`${tx("settings.gates.grants.remove", "Remove grant")} ${index + 1}`}
                  onClick={() => removeGrant(index)}
                />
              </div>
            ))
          )}
          <GatesFooterRow>
            <AddButton
              label={tx("settings.gates.grants.add", "Add a grant")}
              onClick={() =>
                setGrantDraft({ contexts: ["unattended"], hosts: [""], commands: [""] })
              }
            />
          </GatesFooterRow>
          <GatesNote>
            {tx(
              "settings.gates.grants.note",
              "A grant permits a remote command and nothing else. It cannot permit an inventory write.",
            )}
          </GatesNote>
        </GatesGroup>
      </section>

      <section>
        <GatesTitle>{tx("settings.gates.audit.title", "Audit")}</GatesTitle>
        <GatesGroup>
          <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <div className="min-w-0">
              <div className="text-[14px] font-medium leading-5 text-foreground">
                {tx("settings.gates.audit.retention", "Keep records")}
              </div>
              <div className="mt-0.5 max-w-[28rem] text-[12px] leading-5 text-muted-foreground">
                {tx(
                  "settings.gates.audit.retentionHelp",
                  "The gate keeps each decision for this many days.",
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={1}
                max={3650}
                value={draft.audit.retentionDays}
                aria-label={tx("settings.gates.audit.retention", "Keep records")}
                onChange={(event) => {
                  const parsed = Number(event.target.value);
                  if (!Number.isFinite(parsed)) return;
                  updateDraft((previous) => ({
                    ...previous,
                    audit: { ...previous.audit, retentionDays: Math.trunc(parsed) },
                  }));
                }}
                className="h-8 w-24 max-w-full rounded-full text-[13px]"
              />
              <span className="text-[12px] text-muted-foreground">
                {tx("settings.gates.audit.days", "days")}
              </span>
              {isDefault(
                "audit.retentionDays",
                draft.audit.retentionDays,
                savedPolicy.audit.retentionDays,
              ) ? (
                <DefaultMarker
                  label={tx("settings.gates.defaultMarker", "default")}
                  title={defaultTitle}
                />
              ) : null}
            </div>
          </div>
          <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <div className="min-w-0">
              <div className="text-[14px] font-medium leading-5 text-foreground">
                {tx("settings.gates.audit.fullText", "Record full command text")}
              </div>
              <div className="mt-0.5 max-w-[28rem] text-[12px] leading-5 text-amber-700 dark:text-amber-300">
                {tx(
                  "settings.gates.audit.fullTextWarning",
                  "Resolved commands often hold secrets. The log then holds them too.",
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <ToggleButton
                checked={draft.audit.recordCommandText}
                ariaLabel={tx("settings.gates.audit.fullText", "Record full command text")}
                label={
                  draft.audit.recordCommandText
                    ? tx("settings.values.on", "On")
                    : tx("settings.values.off", "Off")
                }
                onChange={(recordCommandText) =>
                  updateDraft((previous) => ({
                    ...previous,
                    audit: { ...previous.audit, recordCommandText },
                  }))
                }
              />
              {isDefault(
                "audit.recordCommandText",
                draft.audit.recordCommandText,
                savedPolicy.audit.recordCommandText,
              ) ? (
                <DefaultMarker
                  label={tx("settings.gates.defaultMarker", "default")}
                  title={defaultTitle}
                />
              ) : null}
            </div>
          </div>
        </GatesGroup>
      </section>

      <GatesGroup>
        <div className="flex min-h-[58px] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div
            className={cn(
              "min-w-0 text-[13px] leading-5",
              error ? "text-destructive-text" : "text-muted-foreground",
            )}
            data-testid="gates-status"
          >
            {statusMessage}
          </div>
          <div className="flex w-full shrink-0 flex-wrap justify-end gap-2 sm:w-auto">
            <Button
              size="sm"
              variant="ghost"
              className="rounded-full"
              disabled={!dirty || saving}
              onClick={() => {
                setDraft(clonePolicy(savedPolicy));
                setError(null);
                setSaved(false);
              }}
            >
              {tx("settings.gates.actions.discard", "Discard")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              disabled={!dirty || saving}
              onClick={requestSave}
            >
              {saving ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {tx("settings.gates.actions.save", "Save policy")}
            </Button>
          </div>
        </div>
      </GatesGroup>

      <GrantEditorDialog
        grant={grantDraft}
        onChange={setGrantDraft}
        onCancel={() => setGrantDraft(null)}
        onCommit={commitGrant}
      />

      {pendingWidening ? (
        <Dialog open onOpenChange={(open) => !open && setPendingWidening(null)}>
          <DialogContent className="max-w-lg rounded-[18px]">
            <DialogHeader>
              <DialogTitle>
                {tx("settings.gates.widen.title", "Widen an unattended decision?")}
              </DialogTitle>
              <DialogDescription>
                {tx(
                  "settings.gates.widen.description",
                  "No person is present in an unattended turn. A cron job, the heartbeat, Dream, and a subagent run there, so this permits an action nobody reads first.",
                )}
              </DialogDescription>
            </DialogHeader>
            <ul className="space-y-1 text-[13px] text-foreground">
              {pendingWidening.map((row) => (
                <li key={row} className="flex items-start gap-2">
                  <CircleAlert className="mt-[3px] h-3.5 w-3.5 shrink-0 text-destructive-text" />
                  <span>{row}</span>
                </li>
              ))}
            </ul>
            <DialogFooter className="gap-2">
              <Button
                size="sm"
                variant="ghost"
                className="rounded-full"
                onClick={() => setPendingWidening(null)}
              >
                {tx("settings.gates.widen.cancel", "Keep it as it is")}
              </Button>
              <Button size="sm" className="rounded-full" onClick={() => void save()}>
                {tx("settings.gates.widen.confirm", "Widen it")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  );
}

/**
 * Which authentication this deployment installed, and who it named here -- #85.
 *
 * The badge of #70 answers "who am I" in a narrow space, so it answers nothing else. This block
 * has room for the two questions beside it: which authentication the gateway installed, and
 * whether it worked for this request.
 *
 * The server sends a posture kind and facts, never a sentence. Every sentence here is a
 * translated string, so an operator reads it in their own language.
 *
 * The warning is the reason the block exists. A configured proxy that asserted nobody leaves
 * every approval on this path naming nobody, and the deployment believes it names somebody.
 */
function IdentitySection({ identity }: { identity?: GatesIdentity }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  // An older gateway sends no block. Absent is not a posture, so the panel claims none.
  if (!identity) return null;

  const postures: Record<string, string> = {
    no_proxy: tx(
      "settings.gates.identity.postures.noProxy",
      "Shared token. No proxy names a person.",
    ),
    verified: tx("settings.gates.identity.postures.verified", "Verified assertion (JWT)"),
    any_verified: tx(
      "settings.gates.identity.postures.anyVerified",
      "Verified assertion (JWT), open to every identity",
    ),
    plain: tx("settings.gates.identity.postures.plain", "Assertion header, not verified"),
  };
  // A gateway newer than this panel can name a fifth posture. The panel then shows the value it
  // received, because an unknown posture must not read as a known one.
  const postureLabel = postures[identity.posture] ?? identity.posture;

  const facts: Array<{ label: string; value: string }> = [];
  if (identity.issuer) {
    facts.push({ label: tx("settings.gates.identity.issuer", "Issuer"), value: identity.issuer });
  }
  if (identity.identityClaim) {
    facts.push({
      label: tx("settings.gates.identity.claim", "Identity claim"),
      value: identity.identityClaim,
    });
  }
  if (identity.assertionHeader) {
    facts.push({
      label: tx("settings.gates.identity.header", "Assertion header"),
      value: identity.assertionHeader,
    });
  }

  return (
    <section>
      <GatesTitle>{tx("settings.gates.identity.title", "Identity")}</GatesTitle>
      <GatesGroup>
        <div className="space-y-3 px-4 py-3.5 sm:px-5" data-testid="gates-identity">
          <IdentityRow label={tx("settings.gates.identity.postureLabel", "Posture")}>
            <div
              className="text-[13.5px] leading-5 text-foreground"
              data-testid="gates-identity-posture"
            >
              {postureLabel}
            </div>
            {facts.length > 0 ? (
              <dl className="mt-1 space-y-0.5">
                {/*
                  The label wraps rather than sits in a fixed column. A translated label is
                  longer than the English one, and a fixed column would cut it.
                */}
                {facts.map((fact) => (
                  <div
                    key={fact.label}
                    className="flex flex-wrap items-baseline gap-x-2 text-[12px] leading-5"
                  >
                    <dt className="text-muted-foreground">{fact.label}</dt>
                    <dd className="min-w-0 break-all font-mono text-[11.5px] text-foreground/85">
                      {fact.value}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </IdentityRow>
          <IdentityRow label={tx("settings.gates.identity.actorLabel", "You are")}>
            {/* The whole string, and no ellipsis. The gate compares all of it (#66). */}
            <code
              className="inline-block break-all rounded bg-muted px-1.5 py-0.5 text-[12px] text-foreground"
              data-testid="gates-identity-actor"
            >
              {identity.actor}
            </code>
            <p
              className="mt-1 text-[12px] leading-5 text-muted-foreground"
              data-testid="gates-identity-approver-row"
            >
              {tx(
                "settings.gates.identity.approverRow",
                "Write this exact value in an approver row.",
              )}
            </p>
          </IdentityRow>
        </div>
        {identity.assertionMissing ? (
          <GatesNote tone="warning" testId="gates-identity-assertion-missing">
            {tx(
              "settings.gates.identity.assertionMissing",
              "A proxy is configured, and no verified identity reached this request. The gateway read the shared token instead, so every approval here names nobody. Check the proxy and the assertion header.",
            )}
          </GatesNote>
        ) : null}
        {identity.posture === "plain" ? (
          <GatesNote tone="warning" testId="gates-identity-plain">
            {tx(
              "settings.gates.identity.plainCaution",
              "The gateway reads this header and verifies nothing. The proxy alone decides who reaches the agent.",
            )}
          </GatesNote>
        ) : null}
        {identity.posture === "any_verified" ? (
          <GatesNote tone="warning" testId="gates-identity-any-verified">
            {tx(
              "settings.gates.identity.anyVerifiedCaution",
              "Every identity the provider signs for may reach the agent. allowAnyVerifiedIdentity is set.",
            )}
          </GatesNote>
        ) : null}
      </GatesGroup>
    </section>
  );
}

/** One labelled row of the identity block: a short label, and the value beside it. */
function IdentityRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:gap-3">
      <span className="text-[12px] font-medium uppercase tracking-wide text-muted-foreground sm:w-28 sm:shrink-0">
        {label}
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/**
 * The form this approver row takes, or the mistake it carries -- nanoinfraorg/nanoinfra#71.
 *
 * One line, under the row it describes. A misconfigured approver is invisible until an approval
 * refuses, so the panel names the shape while the operator is still typing it.
 */
function ApproverShapeNote({
  index,
  channel,
  sender,
}: {
  index: number;
  channel: string;
  sender: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const shape = approverShape(channel, sender);
  const copy: Record<ApproverShape, { text: string; warn: boolean }> = {
    webuiForm: {
      text: tx(
        "settings.gates.approvers.webuiForm",
        "The WebUI form is webui, or webui: and then the claim.",
      ),
      warn: false,
    },
    chatForm: {
      text: tx(
        "settings.gates.approvers.chatForm",
        "The chat form is the account id of one person.",
      ),
      warn: false,
    },
    bareClaim: {
      text: tx(
        "settings.gates.approvers.bareClaim",
        "This sender is a bare claim, so it matches nobody. Write webui: and then the claim.",
      ),
      warn: true,
    },
    notAnAccountId: {
      text: tx(
        "settings.gates.approvers.notAnAccountId",
        "A chat approver is the numeric account id the channel gives.",
      ),
      warn: true,
    },
    blank: {
      text: tx(
        "settings.gates.approvers.blank",
        "This row names no sender, so it matches nobody.",
      ),
      warn: true,
    },
  };
  const { text, warn } = copy[shape];
  return (
    <p
      data-testid={`gates-approver-shape-${index}`}
      data-tone={warn ? "warning" : "info"}
      className={cn(
        "mt-1.5 text-[12px] leading-5",
        warn ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground",
      )}
    >
      {text}
    </p>
  );
}

function DecisionCell({
  label,
  value,
  choices,
  showDefault,
  defaultLabel,
  defaultTitle,
  fixedSuffix,
  toLabel,
  onChange,
  cellTestId,
}: {
  label: string;
  value: string;
  choices: string[];
  showDefault: boolean;
  defaultLabel: string;
  defaultTitle: string;
  fixedSuffix: string;
  toLabel: (value: string) => string;
  onChange: (value: string) => void;
  cellTestId?: string;
}) {
  // One legal value means no control. The schema types `all` as deny, so the panel offers no
  // way to widen that scope.
  if (choices.length < 2) {
    return (
      <span
        className="flex min-w-0 flex-col gap-0.5 text-[13px] text-muted-foreground"
        aria-label={label}
        data-testid={cellTestId}
      >
        <span>
          {toLabel(value)} ({fixedSuffix})
        </span>
      </span>
    );
  }
  return (
    /*
      A column, and not a row. The marker used to sit beside the control, which put it between this
      control and the next column's control with only a grid gap between them. A reader then took it
      for a label of the control on its right, and the whole row read one place shifted. The
      maintainer changed the unattended decision while they meant the interactive one, and a cron job
      could then run a remote command with no person present.
    */
    <span className="flex min-w-0 flex-col gap-0.5" data-testid={cellTestId}>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full min-w-0 rounded-[10px] border border-input bg-background px-2 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
      >
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {toLabel(choice)}
          </option>
        ))}
      </select>
      {showDefault ? <DefaultMarker label={defaultLabel} title={defaultTitle} /> : null}
    </span>
  );
}

function GrantEditorDialog({
  grant,
  onChange,
  onCancel,
  onCommit,
}: {
  grant: GatesStandingGrant | null;
  onChange: (grant: GatesStandingGrant) => void;
  onCancel: () => void;
  onCommit: (grant: GatesStandingGrant) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  if (!grant) return null;

  const hosts = grant.hosts.map((host) => host.trim()).filter(Boolean);
  const commands = grant.commands.map((command) => command.trim()).filter(Boolean);
  const missing: string[] = [];
  if (grant.contexts.length === 0) {
    missing.push(tx("settings.gates.grantEditor.needContext", "Select one context or more."));
  }
  if (hosts.length === 0) {
    missing.push(tx("settings.gates.grantEditor.needHost", "Name one host or more."));
  }
  if (commands.length === 0) {
    missing.push(tx("settings.gates.grantEditor.needCommand", "Name one command or more."));
  }

  const toggleContext = (context: string) => {
    onChange({
      ...grant,
      contexts: grant.contexts.includes(context)
        ? grant.contexts.filter((entry) => entry !== context)
        : [...grant.contexts, context],
    });
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="max-w-xl rounded-[18px]">
        <DialogHeader>
          <DialogTitle>{tx("settings.gates.grantEditor.title", "Standing grant")}</DialogTitle>
          <DialogDescription>
            {tx(
              "settings.gates.grantEditor.description",
              "A grant permits one exact command on named hosts. It permits nothing else.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <div className="text-[12px] font-medium text-muted-foreground">
              {tx("settings.gates.grantEditor.appliesTo", "Applies to")}
            </div>
            <div className="mt-1.5 flex gap-4">
              {["unattended", "interactive"].map((context) => (
                <label key={context} className="flex items-center gap-2 text-[13px]">
                  <input
                    type="checkbox"
                    checked={grant.contexts.includes(context)}
                    onChange={() => toggleContext(context)}
                    aria-label={context}
                  />
                  <span>{context}</span>
                </label>
              ))}
            </div>
          </div>

          <GrantValueList
            title={tx("settings.gates.grants.hosts", "Hosts")}
            addLabel={tx("settings.gates.grantEditor.addHost", "Add a host")}
            fieldLabel={tx("settings.gates.grantEditor.hostField", "Host")}
            note={tx(
              "settings.gates.grantEditor.hostNote",
              "These names come from inventory records. The gate compares the resolved address at run time, not the name.",
            )}
            values={grant.hosts}
            onChange={(hostValues) => onChange({ ...grant, hosts: hostValues })}
          />

          <GrantValueList
            title={tx("settings.gates.grants.commands", "Commands")}
            addLabel={tx("settings.gates.grantEditor.addCommand", "Add a command")}
            fieldLabel={tx("settings.gates.grantEditor.commandField", "Command")}
            note={tx(
              "settings.gates.grantEditor.commandNote",
              "Exact match only. This field is not a pattern. A command that differs by one character does not match.",
            )}
            values={grant.commands}
            onChange={(commandValues) => onChange({ ...grant, commands: commandValues })}
          />

          {missing.length > 0 ? (
            <div
              className="text-[12px] leading-5 text-amber-700 dark:text-amber-300"
              data-testid="gates-grant-editor-missing"
            >
              {missing.join(" ")}
            </div>
          ) : null}
        </div>
        <DialogFooter className="gap-2">
          <Button size="sm" variant="ghost" className="rounded-full" onClick={onCancel}>
            {tx("settings.gates.actions.cancel", "Cancel")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={missing.length > 0}
            onClick={() => onCommit({ ...grant, hosts, commands })}
          >
            {tx("settings.gates.grantEditor.commit", "Add grant")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function GrantValueList({
  title,
  addLabel,
  fieldLabel,
  note,
  values,
  onChange,
}: {
  title: string;
  addLabel: string;
  fieldLabel: string;
  note: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="text-[12px] font-medium text-muted-foreground">{title}</div>
      <div className="mt-1.5 space-y-1.5">
        {values.map((value, index) => (
          <div key={index} className="flex items-center gap-2">
            <Input
              value={value}
              aria-label={`${fieldLabel} ${index + 1}`}
              onChange={(event) =>
                onChange(
                  values.map((entry, position) =>
                    position === index ? event.target.value : entry,
                  ),
                )
              }
              className="h-9 flex-1 rounded-[10px] text-[13px]"
            />
            <RemoveButton
              label={t("settings.gates.grantEditor.removeValue", {
                defaultValue: "{{field}} {{index}} remove",
                field: fieldLabel,
                index: index + 1,
              })}
              onClick={() => onChange(values.filter((_entry, position) => position !== index))}
            />
          </div>
        ))}
      </div>
      <div className="mt-1.5">
        <AddButton label={addLabel} onClick={() => onChange([...values, ""])} />
      </div>
      <p className="mt-1.5 text-[12px] leading-5 text-muted-foreground">{note}</p>
    </div>
  );
}

function GatesTitle({
  children,
  marker,
  markerTitle,
}: {
  children: ReactNode;
  marker?: string;
  markerTitle?: string;
}) {
  return (
    <h2 className="mb-2 flex items-center gap-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
      <span>{children}</span>
      {marker ? <DefaultMarker label={marker} title={markerTitle ?? ""} /> : null}
    </h2>
  );
}

function GatesGroup({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-[22px] bg-settings-surface">
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

function GatesNote({
  children,
  tone = "info",
  testId,
}: {
  children: ReactNode;
  tone?: "info" | "warning";
  testId?: string;
}) {
  const Icon = tone === "warning" ? CircleAlert : Info;
  return (
    <div
      className={cn(
        "flex gap-2 px-4 py-3 text-[12px] leading-5 sm:px-5",
        tone === "warning" ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground",
      )}
      data-testid={testId}
      data-tone={tone}
    >
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

function GatesEmpty({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div className="px-4 py-3.5 text-[13px] leading-5 text-foreground sm:px-5" data-testid={testId}>
      {children}
    </div>
  );
}

function GatesFooterRow({ children }: { children: ReactNode }) {
  return <div className="flex justify-end px-4 py-2.5 sm:px-5">{children}</div>;
}

function DefaultMarker({ label, title }: { label: string; title: string }) {
  return (
    <span
      title={title}
      className="inline-flex shrink-0 items-center rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
    >
      {label}
    </span>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button size="sm" variant="ghost" className="rounded-full" onClick={onClick}>
      <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
      {label}
    </Button>
  );
}

function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive-text"
    >
      <Trash2 className="h-3.5 w-3.5" aria-hidden />
    </button>
  );
}
