/**
 * The agents this deployment names -- nanoinfraorg/nanoinfra#253, made writable in #262, and laid
 * out here the way the product the maintainer put beside it lays it out.
 *
 * A title, a line saying what the page is for, and `New agent` on the right. Below it the roster as
 * **cards**: the name in monospace because it is a token you copy, a badge, the model and provider
 * it answers with, and count chips. Rows in a single hairlined list read as a table of settings;
 * an agent is an object, and a card says so.
 *
 * Five decisions worth keeping in the file:
 *
 * - **Counts on the card, never the bindings.** A card says an agent carries three tool groups and
 *   one delegate; it does not say which. Enumerating them here would put the authorization model on
 *   screen for anybody who can open a settings page, and the card is not where that question is
 *   asked. The detail view is: opened deliberately, for one agent, in order to change it.
 * - **`all`, never `0`, for a list that is empty by way of meaning everything.** `toolGroups: []`
 *   is how an agent says *every group*, and `skills: []` is how it says *every skill, summarised*.
 *   A chip reading `0 tool groups` therefore said the opposite of the truth, and on a row whose
 *   three leading chips were all zero it said an agent could do nothing.
 * - **The default agent is the first row.** `agents.defaults` is the agent that answers when
 *   nobody picks one, the composer offers it by name, and this page used to list only
 *   `agents.named` -- so the one agent that actually answers a message was the only one with no row
 *   anywhere. See `AgentDefaultRow` for why it is a row that expands and not a page.
 * - **`model · provider`, not the preset name.** `primary` and `cheap` are names config chose and
 *   tell an operator nothing; which model each of them is, does.
 * - **Creation is inline, above the list.** Not a dialog and not the agent page in a second mode --
 *   see `AgentCreateForm`. The list stays visible behind the form, which is where the answer to
 *   *is this name taken* is.
 *
 * The only dialog left here is the delete confirm, which is the one thing on this page that
 * destroys something and therefore the one thing worth interrupting for.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Pencil, Plus, Trash2 } from "lucide-react";

import { AgentCreateForm } from "@/components/agents/AgentCreateForm";
import { AgentDefaultDetail } from "@/components/agents/AgentDefaultDetail";
import { AgentDefaultRow } from "@/components/agents/AgentDefaultRow";
import { AgentDetail } from "@/components/agents/AgentDetail";
import { AgentPromptBadge } from "@/components/agents/AgentPromptBadge";
import {
  agentsDelegatingTo,
  hasOwnPrompt,
  presetModelLine,
  rosterWithoutAgent,
} from "@/components/agents/agentValues";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { saveNamedAgents, serverReason } from "@/lib/api";
import type { NamedAgentRosterEntry, SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

export function AgentRoster({
  agents,
  token,
  base = "",
  modelPresets = [],
  declaredToolGroups,
  onSaved,
  onNavigateToToolGroups,
  defaultAgent,
  onNavigateToModels,
  onNavigateToSkills,
  onDetailOpenChange,
}: {
  agents: NamedAgentRosterEntry[];
  token: string;
  base?: string;
  /** The configured presets, so the editor picks a model instead of asking for one to be typed. */
  modelPresets?: SettingsPayload["model_presets"];
  /** `tools.groups` plus the built-ins, keyed by name, when this gateway reports them. */
  declaredToolGroups?: Record<string, unknown>;
  /**
   * The fresh settings payload a write answers with. Optional: a caller that only reads the roster
   * -- and there is one, in the tests -- has nothing to do with it.
   */
  onSaved?: (payload: SettingsPayload) => void;
  /** Opens the Tool groups panel, which is where a group is made. */
  onNavigateToToolGroups?: () => void;
  /**
   * `agents.defaults`, so the agent that answers when nobody picks one has a row here too.
   *
   * Optional: a caller that has no settings payload -- and there is one, in the tests -- renders
   * the named roster alone, which is what this panel did before the default agent had a row.
   */
  defaultAgent?: SettingsPayload["agent"];
  /** Where the default agent's model actually lives, which is `Settings > Models`. */
  onNavigateToModels?: () => void;
  /** Where a skill is installed or disabled. */
  onNavigateToSkills?: () => void;
  /**
   * True while one agent's own page is open, so the area around this panel can put its
   * deployment-wide settings away.
   *
   * Those settings are not this agent's: `Subagents at once` is how many subagents may run at
   * once anywhere in the deployment, and rendered under one agent's tabs it reads as a property of
   * that agent -- the exact confusion named agents exist to end.
   */
  onDetailOpenChange?: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  /**
   * Which agent's own page is open, or `null` for the list.
   *
   * Two kinds, because two config shapes: a named agent is a roster row, and the deployment's own
   * is `agents.defaults` -- read from the settings payload rather than carried as a row, so it has
   * nothing to key on but the discriminator.
   */
  const [open, setOpen] = useState<
    { kind: "named"; entry: NamedAgentRosterEntry } | { kind: "defaults" } | null
  >(null);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<NamedAgentRosterEntry | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    onDetailOpenChange?.(open !== null);
  }, [open, onDetailOpenChange]);

  const confirmDelete = async (entry: NamedAgentRosterEntry) => {
    if (deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      const payload = await saveNamedAgents(token, rosterWithoutAgent(agents, entry.name), base);
      setPendingDelete(null);
      onSaved?.(payload);
    } catch (err) {
      setDeleteError(serverReason(err));
    } finally {
      setDeleting(false);
    }
  };

  /** The row the gateway now holds for `name`, so a write reopens the agent it produced. */
  const rowFor = (payload: SettingsPayload, name: string) =>
    (payload.named_agents ?? []).find((each) => each.name === name);

  /*
   * The deployment's own agent first, because its page is a slightly different shape: the
   * agent-shaped fields of
   * `agents.defaults` written by their own route, against a named agent's nine written whole with
   * the roster. They share `AgentDetailFrame` and nothing else.
   */
  if (open?.kind === "defaults" && defaultAgent) {
    return (
      <AgentDefaultDetail
        agent={defaultAgent}
        entries={agents}
        declaredToolGroups={declaredToolGroups}
        token={token}
        base={base}
        onClose={() => setOpen(null)}
        onSaved={(payload) => onSaved?.(payload)}
        onNavigateToModels={onNavigateToModels}
        onNavigateToSkills={onNavigateToSkills}
        onNavigateToToolGroups={onNavigateToToolGroups}
      />
    );
  }

  if (open?.kind === "named") {
    // The roster is still passed in whole, because a save replaces the whole roster and the
    // delegate picker offers this agent's peers.
    return (
      <AgentDetail
        entry={open.entry}
        entries={agents}
        modelPresets={modelPresets}
        declaredToolGroups={declaredToolGroups}
        token={token}
        base={base}
        onClose={() => setOpen(null)}
        onSaved={(payload, name) => {
          onSaved?.(payload);
          // Re-keyed onto the row the gateway now holds. A payload that does not carry the row
          // leaves the view alone, because guessing would be worse than staying put.
          const row = rowFor(payload, name);
          if (row) setOpen({ kind: "named", entry: row });
        }}
        onNavigateToToolGroups={onNavigateToToolGroups}
      />
    );
  }

  return (
    <section data-testid="agent-roster">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2 px-1">
        <div className="min-w-0">
          {/*
            * `settings.nav.agents` rather than a heading string of this panel's own: the sidebar
            * and the page have to agree on the name of the place, and that key is already the
            * name of the place in eight languages.
            */}
          <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-foreground">
            {t("settings.nav.agents", { defaultValue: "Agents" })}
          </h2>
          <p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
            {t("agents.roster.subtitle", { defaultValue: "Manage AI agents" })}
          </p>
        </div>
        {/*
          * Hidden while the form is open: the form has its own Cancel, and a button that opens
          * something already open is a button that does nothing.
          */}
        {creating ? null : (
          <Button
            type="button"
            variant="ghost"
            onClick={() => setCreating(true)}
            className="h-8 shrink-0 rounded-full px-3 text-[12px]"
            data-testid="agent-roster-new"
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t("agents.roster.new", { defaultValue: "New agent" })}
          </Button>
        )}
      </div>

      {creating
        ? (
          <AgentCreateForm
            entries={agents}
            modelPresets={modelPresets}
            token={token}
            base={base}
            onCancel={() => setCreating(false)}
            onCreated={(payload, name) => {
              onSaved?.(payload);
              setCreating(false);
              // Straight into the agent that was just made: creation answers three of nine
              // questions, and the other six are on the page this opens.
              const row = rowFor(payload, name);
              if (row) setOpen({ kind: "named", entry: row });
            }}
          />
        )
        : null}

      <ul className="space-y-2.5">
        {/*
          * First, and **whether or not this deployment names an agent**.
          *
          * It used to render only alongside named agents, mirroring the composer: `AgentBadge`
          * shows nothing until a roster exists, so there was no picker entry to explain. That
          * reasoning held while this row was a read-only fact. It stopped holding the moment the
          * row became the way in to an editor, and what it cost was the whole point of the
          * feature: on a fresh install `agents.named` is empty, so the one agent that answers
          * every single turn -- and the only place to narrow the skills and MCP servers a
          * conversation pays for -- was reachable only after inventing a named agent you did not
          * want. There is nothing to seed here, because this agent *is* what a deployment has
          * before it names anything.
          */}
        {defaultAgent
          ? (
            <AgentDefaultRow
              agent={defaultAgent}
              toolGroupCount={declaredToolGroups
                ? Object.keys(declaredToolGroups).length
                : undefined}
              onOpen={() => setOpen({ kind: "defaults" })}
            />
          )
          : null}
        {agents.length === 0
          ? (
            <li
              className="rounded-[18px] bg-settings-surface/60 px-4 py-3.5 sm:px-5"
              data-testid="agent-roster-empty"
            >
              <p className="max-w-[38rem] text-[12px] leading-5 text-muted-foreground">
                {t("agents.roster.emptyHelp", {
                  defaultValue:
                    "No agent is named yet, so every message is answered by the agent above. Name one to give it its own model, its own tools and its own instructions, addressed as @agent:<name>.",
                })}
              </p>
            </li>
          )
          : null}
        {agents.map((agent) => {
              const line = presetModelLine(agent.model_preset, modelPresets);
              return (
                <li
                  key={agent.name}
                  className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
                  data-testid={`agent-row-${agent.name}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <button
                      type="button"
                      onClick={() => setOpen({ kind: "named", entry: agent })}
                      className="min-w-0 flex-1 text-left"
                      data-testid={`agent-open-${agent.name}`}
                    >
                      <span className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className="truncate font-mono text-[14px] font-semibold leading-5 text-foreground">
                          {agent.name}
                        </span>
                        <AgentPromptBadge
                          own={hasOwnPrompt(agent)}
                          testId={`agent-prompt-source-${agent.name}`}
                        />
                      </span>
                      <span
                        className="mt-0.5 block text-[11.5px] leading-4 text-muted-foreground"
                        data-testid={`agent-model-line-${agent.name}`}
                      >
                        {line ? `${line.model} · ${line.provider}` : agent.model_preset}
                      </span>
                      {agent.description
                        ? (
                          <span className="mt-1.5 block max-w-[36rem] text-[12px] leading-5 text-muted-foreground">
                            {agent.description}
                          </span>
                        )
                        : null}
                      <span className="mt-2 flex flex-wrap items-center gap-1.5">
                        <BindingChip
                          label={t("agents.roster.chip.toolGroups", {
                            defaultValue: "tool groups",
                          })}
                          declared={agent.tool_groups}
                          count={agent.tool_group_count}
                          testId={`agent-count-tool-groups-${agent.name}`}
                        />
                        <BindingChip
                          label={t("agents.roster.chip.skills", { defaultValue: "skills" })}
                          declared={agent.skills}
                          count={agent.skill_count}
                          testId={`agent-count-skills-${agent.name}`}
                        />
                        {/*
                          * Only when the gateway reports the key at all. There is no `mcp_count`
                          * on the wire, so an older payload gets no chip rather than a number that
                          * would read as *none bound* when it means *not reported*.
                          */}
                        {"mcp_servers" in agent
                          ? (
                            <BindingChip
                              label={t("agents.roster.chip.mcp", { defaultValue: "mcp" })}
                              declared={agent.mcp_servers}
                              testId={`agent-count-mcp-${agent.name}`}
                            />
                          )
                          : null}
                        <BindingChip
                          label={t("agents.roster.chip.delegates", { defaultValue: "delegates" })}
                          // Numeric, never `all` and never `none`: membership is the grant, so
                          // there is no third state here and `0 delegates` is simply true.
                          numeric
                          declared={agent.delegates}
                          count={agent.delegate_count}
                          testId={`agent-count-delegates-${agent.name}`}
                        />
                      </span>
                    </button>
                    {/*
                      * Siblings of the card's own button rather than children of it: a button
                      * inside a button is not a thing the DOM has, and the first casualty would be
                      * the keyboard.
                      */}
                    <span className="flex shrink-0 items-center gap-1">
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => setOpen({ kind: "named", entry: agent })}
                        aria-label={t("agents.roster.editAgent", {
                          name: agent.name,
                          defaultValue: "Edit {{name}}",
                        })}
                        className="h-8 w-8 rounded-full p-0 text-muted-foreground hover:text-foreground"
                        data-testid={`agent-edit-${agent.name}`}
                      >
                        <Pencil className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(agent);
                        }}
                        aria-label={t("agents.roster.deleteAgent", {
                          name: agent.name,
                          defaultValue: "Delete {{name}}",
                        })}
                        className="h-8 w-8 rounded-full p-0 text-destructive-text"
                        data-testid={`agent-delete-${agent.name}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    </span>
                  </div>
                </li>
              );
            })}
      </ul>
      <AgentDeleteDialog
        agent={pendingDelete}
        dependents={pendingDelete ? agentsDelegatingTo(agents, pendingDelete.name) : []}
        deleting={deleting}
        error={deleteError}
        onOpenChange={(next) => {
          if (!next) setPendingDelete(null);
        }}
        onConfirm={confirmDelete}
      />
    </section>
  );
}

/**
 * Asks once, and says what breaks.
 *
 * The only dialog on this page, because a delete is the only thing here that destroys something.
 * The dependents matter because config refuses a roster whose delegate does not exist: deleting an
 * agent that somebody delegates to comes back refused, and a confirmation that had not mentioned it
 * would send the operator into a refusal they had no way to predict. Naming them turns that into a
 * decision -- edit the peer first -- taken before the click rather than after it.
 */
function AgentDeleteDialog({
  agent,
  dependents,
  deleting,
  error,
  onOpenChange,
  onConfirm,
}: {
  agent: NamedAgentRosterEntry | null;
  dependents: string[];
  deleting: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: (agent: NamedAgentRosterEntry) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open={Boolean(agent)} onOpenChange={onOpenChange}>
      {agent
        ? (
          <DialogContent
            className="w-[min(calc(100vw-2rem),26rem)] rounded-[26px]"
            data-testid="agent-delete-confirm"
          >
            <DialogHeader>
              <DialogTitle>
                {t("agents.roster.deleteTitle", {
                  name: agent.name,
                  defaultValue: "Delete {{name}}?",
                })}
              </DialogTitle>
              <DialogDescription>
                {t("agents.roster.deleteDescription", {
                  defaultValue:
                    "The agent stops existing and stops being addressable. Past messages stay in their sessions.",
                })}
              </DialogDescription>
            </DialogHeader>
            {dependents.length > 0
              ? (
                <p
                  className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
                  data-testid="agent-delete-dependents"
                >
                  {t("agents.roster.deleteDependents", {
                    agents: dependents.join(", "),
                    defaultValue:
                      "{{agents}} delegate to this agent. Config refuses a roster whose delegate does not exist, so remove it from them first.",
                  })}
                </p>
              )
              : null}
            {error
              ? (
                <p
                  className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
                  data-testid="agent-delete-error"
                >
                  {error}
                </p>
              )
              : null}
            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={deleting}
                className="rounded-full"
              >
                {t("agents.roster.deleteCancel", { defaultValue: "Cancel" })}
              </Button>
              <Button
                type="button"
                onClick={() => void onConfirm(agent)}
                disabled={deleting}
                className="rounded-full bg-destructive text-destructive-foreground hover:bg-destructive/90"
                data-testid="agent-delete-confirm-button"
              >
                {deleting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
                {t("agents.roster.deleteConfirm", { defaultValue: "Delete" })}
              </Button>
            </DialogFooter>
          </DialogContent>
        )
        : null}
    </Dialog>
  );
}

/**
 * How many, and never which -- with **three** answers, because the list has three states.
 *
 * The number leads, the way the reference product's chips read, and the label after it is a
 * category name rather than a sentence -- `3 skills`, `1 delegates`. That is deliberate: a label
 * that had to agree with its number would need a plural rule in eight languages for a chip whose
 * whole job is to be glanced at, and `Skills 3` was the old row's way of dodging exactly that.
 *
 * **What this chip used to get wrong.** It read a count, and it was told that `0` meant *all*.
 * That was true when it was written and it is the exact conflation the rest of this change
 * removes: config, the tool filter and this chip now all distinguish *nothing declared* -- which
 * is every one of them -- from *declared, and empty* -- which is none of them. A chip that showed
 * `all` for both would report a coordinator narrowed to no grouped surface as an agent that can
 * reach every one.
 *
 * So: `null` reads `all`, `[]` reads `none`, and a list reads its length. `undefined` is a gateway
 * that does not report the list, and only there does the count fall back to the old reading --
 * that gateway's config had the old meaning too, so `all` is what it actually did.
 */
function BindingChip({
  label,
  declared,
  count,
  numeric = false,
  testId,
}: {
  label: string;
  /** `null` is nothing declared, `[]` is declared and empty, `undefined` is not reported. */
  declared: readonly string[] | null | undefined;
  /** The count the payload carries, read only when the list itself is not reported. */
  count?: number;
  /**
   * True for `delegates`, the one list with no third state: membership is the grant, so an empty
   * list really is none and `0` is the fact rather than a lie. It reads as a number always.
   */
  numeric?: boolean;
  testId: string;
}) {
  const { t } = useTranslation();
  const state = numeric
    ? "some"
    : declared === undefined
    ? ((count ?? 0) === 0 ? "all" : "some")
    : declared === null
    ? "all"
    : declared.length === 0
    ? "none"
    : "some";
  const value = state === "all"
    ? t("agents.roster.chip.all", { defaultValue: "all" })
    : state === "none"
    ? t("agents.roster.chip.none", { defaultValue: "none" })
    : String(Array.isArray(declared) ? declared.length : count ?? 0);
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] leading-4",
        state === "none"
          ? "bg-muted/50 text-muted-foreground"
          : "bg-muted text-foreground/80",
      )}
      data-testid={testId}
      title={state === "all"
        ? t("agents.roster.chipAllHelp", {
          defaultValue: "Nothing declared, which is every one of them.",
        })
        : state === "none"
        ? t("agents.roster.chipNoneHelp", {
          defaultValue: "Declared, and empty: this agent reaches none of them.",
        })
        : undefined}
    >
      <span className="tabular-nums font-semibold">{value}</span> {label}
    </span>
  );
}
