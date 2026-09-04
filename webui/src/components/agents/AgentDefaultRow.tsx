/**
 * The agent this page never mentioned: `agents.defaults`, the one that answers when nobody picks.
 *
 * The composer offers it by name -- `Default agent`, in `AgentBadge` and in the automation editor's
 * picker -- and the Agents page listed only `agents.named`. So the one agent that actually answers
 * a message today was the only one with no row anywhere, and the question that followed was the
 * obvious one: *"¿Cuál se supone es el Default agent?"*
 *
 * **It was an expandable row for one hour, and the reason is worth keeping.** There was no route
 * that wrote `agents.defaults`, so a page with tabs and a `Save` would have been controls whose
 * every save came back refused -- the exact thing this rework spent its time removing. There is one
 * now (`GET /api/settings/agents/defaults`, #265) and `AgentDefaults` gained every field a named
 * agent had and it lacked (#266), so the row is a card like the others and opens
 * `AgentDefaultDetail`.
 *
 * **Its chips have three answers, and that is the fix.** `all` is *nothing declared*, `none` is
 * *declared and empty*, a number is a number. They were two answers with `all` covering both, and
 * on this row of all rows that mattered most: the default agent is the one that answers when
 * nobody picks, so an agent narrowed to no MCP server reading `all mcp` would be the page lying
 * about the one turn that happens most.
 *
 * **Delegates included.** `AgentDefaults` had no `delegates` field, so this row used to say the
 * default agent *can have none*. It has one now, for a reason its owner put plainly: the default
 * agent is the one you talk to, so if it cannot hand a database question to `db` then the only way
 * to reach `db` is to stop talking to the agent you were talking to.
 */
import { useTranslation } from "react-i18next";
import { Pencil } from "lucide-react";

import { AgentPromptBadge } from "@/components/agents/AgentPromptBadge";
import { deploymentModelLine } from "@/components/agents/agentValues";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

export function AgentDefaultRow({
  agent,
  toolGroupCount,
  onOpen,
}: {
  /** `agents.defaults` as the settings payload reports it. */
  agent: SettingsPayload["agent"];
  /** How many tool groups exist, which is how many this agent may use when it names none. */
  toolGroupCount?: number;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const modelLine = deploymentModelLine(
    agent.model,
    agent.resolved_provider || agent.provider,
    tx("agents.roster.noModel", "No model configured"),
  );
  /*
   * The badge follows what config holds, not what the row can guess. A gateway that does not
   * report the addendum reports nothing about it either way, and `Default prompt` would be a
   * claim rather than a reading -- so an unreporting gateway gets the honest `undefined` path
   * through `??`, which is `false`, and the detail page is where that gap is named.
   */
  const own = Boolean(agent.addendum?.trim())
    || Object.keys(agent.prompt_sections ?? {}).length > 0;

  return (
    <li
      className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
      data-testid="agent-default-row"
    >
      <div className="flex items-start justify-between gap-3">
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left"
          data-testid="agent-default-open"
        >
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            {/*
              * The composer's own words, from the key the automation editor's picker already
              * uses: two names for one agent is how this confusion started.
              */}
            <span className="truncate font-mono text-[14px] font-semibold leading-5 text-foreground">
              {tx("settings.automations.agentDefault", "Default agent")}
            </span>
            <span
              className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-secondary-foreground"
              data-testid="agent-default-badge"
            >
              {tx("agents.roster.defaultBadge", "This deployment's own")}
            </span>
            <AgentPromptBadge own={own} testId="agent-default-prompt-source" />
          </span>
          <span
            className="mt-0.5 block text-[11.5px] leading-4 text-muted-foreground"
            data-testid="agent-default-model-line"
          >
            {modelLine}
          </span>
          <span className="mt-1.5 block max-w-[36rem] text-[12px] leading-5 text-muted-foreground">
            {tx(
              "agents.roster.defaultHelp",
              "What answers when no agent is chosen, and what every named agent inherits from.",
            )}
          </span>
          <span className="mt-2 flex flex-wrap items-center gap-1.5">
            <DeclaredChip
              label={tx("agents.roster.chip.toolGroups", "tool groups")}
              declared={agent.tool_groups}
              total={toolGroupCount}
              testId="agent-default-count-tool-groups"
            />
            <DeclaredChip
              label={tx("agents.roster.chip.skills", "skills")}
              declared={agent.skills}
              testId="agent-default-count-skills"
            />
            <DeclaredChip
              label={tx("agents.roster.chip.mcp", "mcp")}
              declared={agent.mcp_servers}
              testId="agent-default-count-mcp"
            />
            <DeclaredChip
              label={tx("agents.roster.chip.connectors", "connectors")}
              declared={agent.connectors}
              testId="agent-default-count-connectors"
            />
            {/*
              * A number, not `all` or `none`: membership is the grant, so there is no third state
              * here and `0 delegates` is simply true.
              */}
            <span
              className="rounded-full bg-muted px-2 py-0.5 text-[11px] leading-4 text-foreground/80"
              data-testid="agent-default-count-delegates"
            >
              <span className="tabular-nums font-semibold">
                {(agent.delegates ?? []).length}
              </span>{" "}
              {tx("agents.roster.chip.delegates", "delegates")}
            </span>
          </span>
        </button>
        {/*
          * A pencil, and deliberately no trash. `agents.defaults` is not a config entry that can
          * be added or removed, so a delete icon would be a button whose only outcome is a
          * refusal -- while everything else about it is now genuinely editable. *It is one more
          * agent; it just cannot be deleted.*
          */}
        <span className="flex shrink-0 items-center">
          <Button
            type="button"
            variant="ghost"
            onClick={onOpen}
            aria-label={tx("agents.roster.editDefault", "Edit the default agent")}
            className="h-8 w-8 rounded-full p-0 text-muted-foreground hover:text-foreground"
            data-testid="agent-default-edit"
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </span>
      </div>
    </li>
  );
}

/**
 * One narrowing list as a chip, with the three answers config actually has.
 *
 * - `null` -- nothing declared -- reads `all`, and `all 4` where the deployment's own total is
 *   known, because *all* is a claim about this deployment and a number makes it checkable.
 * - `[]` -- declared, and empty -- reads `none`. This is the answer the chip could not give
 *   before, and the one an operator narrowing a context budget is actually looking for.
 * - A list reads its length.
 *
 * `undefined` is a gateway that does not report the key; it reads `all` without a number, which
 * is what that gateway's config meant.
 */
function DeclaredChip({
  label,
  declared,
  total,
  testId,
}: {
  label: string;
  declared: readonly string[] | null | undefined;
  /** How many the deployment has, so `all` can be stated as a number. */
  total?: number;
  testId: string;
}) {
  const { t } = useTranslation();
  const none = Array.isArray(declared) && declared.length === 0;
  const some = Array.isArray(declared) && declared.length > 0;
  const all = t("agents.roster.chip.all", { defaultValue: "all" });
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] leading-4",
        none ? "bg-muted/50 text-muted-foreground" : "bg-muted text-foreground/80",
      )}
      data-testid={testId}
      title={none
        ? t("agents.roster.chipNoneHelp", {
          defaultValue: "Declared, and empty: this agent reaches none of them.",
        })
        : some
        ? undefined
        : t("agents.roster.chipAllHelp", {
          defaultValue: "Nothing declared, which is every one of them.",
        })}
    >
      <span className="tabular-nums font-semibold">
        {some
          ? declared.length
          : none
          ? t("agents.roster.chip.none", { defaultValue: "none" })
          : total === undefined
          ? all
          : `${all} ${total}`}
      </span>{" "}
      {label}
    </span>
  );
}
