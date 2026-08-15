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
import { fetchWithTimeout } from "@/lib/http";
import type { SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const GATES_VALUES_HEADER = "X-Nanoinfra-Gates-Values";
const GATES_UPDATE_PATH = "/api/settings/gates/update";

/** Key spelling matches config.json, so no layer translates a policy key. */
export interface GatesScopePolicy {
  host: string;
  group: string;
  all: string;
}

export interface GatesContextPolicy {
  "mutate.remote": GatesScopePolicy;
  "mutate.inventory": string;
  "credential.access": string;
}

export interface GatesApprover {
  channel: string;
  sender: string;
}

export interface GatesStandingGrant {
  id?: string | null;
  contexts: string[];
  hosts: string[];
  commands: string[];
}

export interface GatesAuditPolicy {
  retentionDays: number;
  recordCommandText: boolean;
}

export interface GatesPolicy {
  approvers: GatesApprover[];
  approvalPaths: string[];
  interactive: GatesContextPolicy;
  unattended: GatesContextPolicy;
  standingGrants: GatesStandingGrant[];
  audit: GatesAuditPolicy;
}

export interface GatesPayload {
  policy: GatesPolicy;
  from_default: Record<string, boolean>;
  choices: {
    "mutate.remote": string[];
    "mutate.inventory": string[];
    "credential.access": string[];
    all: string[];
  };
}

type AdvancedWithGates = SettingsPayload["advanced"] & { gates?: GatesPayload };

type ContextKey = "interactive" | "unattended";
type ScopeField = "host" | "group" | "all";

const CONTEXT_KEYS: ContextKey[] = ["interactive", "unattended"];
const SCOPE_FIELDS: ScopeField[] = ["host", "group", "all"];

/** Read the gate block. An older gateway sends no block, and then the panel stays away. */
export function gatesPayloadFrom(settings: SettingsPayload): GatesPayload | null {
  const advanced = settings.advanced as AdvancedWithGates;
  const gates = advanced.gates;
  if (!gates || !gates.policy || !gates.choices) return null;
  return gates;
}

async function saveGatesPolicy(token: string, policy: GatesPolicy): Promise<SettingsPayload> {
  const response = await fetchWithTimeout(GATES_UPDATE_PATH, {
    headers: {
      Authorization: `Bearer ${token}`,
      [GATES_VALUES_HEADER]: encodeURIComponent(JSON.stringify(policy)),
    },
    credentials: "same-origin",
  });
  if (!response.ok) {
    const text = typeof response.text === "function" ? (await response.text()).trim() : "";
    throw new Error(text || `HTTP ${response.status}`);
  }
  return (await response.json()) as SettingsPayload;
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
    setSaving(true);
    setError(null);
    try {
      const payload = await saveGatesPolicy(token, draft);
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
              <div className="grid grid-cols-[minmax(0,1fr)_9.5rem_9.5rem] gap-3 pb-2 text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                <span>{tx("settings.gates.columns.policy", "Policy")}</span>
                <span>{contextLabel("interactive")}</span>
                <span>{contextLabel("unattended")}</span>
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
              <div
                key={index}
                className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:px-5"
              >
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
                  label={`${tx("settings.gates.approvers.remove", "Remove approver")} ${index + 1}`}
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
              error ? "text-destructive" : "text-muted-foreground",
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
              onClick={() => void save()}
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
    </div>
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
}) {
  // One legal value means no control. The schema types `all` as deny, so the panel offers no
  // way to widen that scope.
  if (choices.length < 2) {
    return (
      <span className="text-[13px] text-muted-foreground" aria-label={label}>
        {toLabel(value)} ({fixedSuffix})
      </span>
    );
  }
  return (
    <span className="flex min-w-0 items-center gap-1.5">
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
              label={`${fieldLabel} ${index + 1} remove`}
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
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
    >
      <Trash2 className="h-3.5 w-3.5" aria-hidden />
    </button>
  );
}
