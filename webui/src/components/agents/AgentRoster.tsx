/**
 * The agents this deployment names -- nanoinfraorg/nanoinfra#253.
 *
 * Three decisions are visible in this file and each one is a place the obvious arrangement is
 * wrong:
 *
 * - **Counts, never the bindings.** A row says an agent carries three tool groups and one
 *   delegate; it does not say which. Those are the agent's *authority*, decided in a config file a
 *   human reviews, and a roster that enumerated them would be publishing the authorization model
 *   to any browser that could open a settings page. The same reason the delegate tool's own
 *   description carries counts: an agent may cover hundreds of hosts.
 * - **No "New Agent" button.** There is no write path, and a button that opens a form the gateway
 *   will not accept is a worse answer than no button. An agent is declared where authority lives.
 * - **Nothing at all when no agent is named.** Which is every deployment today: the panel above
 *   this one keeps being exactly what it was, and the navigation does not grow a destination for a
 *   concept the deployment does not use.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { AgentPromptPanel } from "@/components/agents/AgentPromptPanel";
import type { NamedAgentRosterEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

export function AgentRoster({
  agents,
  token,
  base = "",
}: {
  agents: NamedAgentRosterEntry[];
  token: string;
  base?: string;
}) {
  const { t } = useTranslation();
  const [openAgent, setOpenAgent] = useState<string | null>(null);

  if (agents.length === 0) return null;

  return (
    <section data-testid="agent-roster">
      <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
        {t("agents.roster.title", { defaultValue: "Named agents" })}
      </h2>
      <p className="mb-2 px-1 text-[12px] leading-5 text-muted-foreground">
        {t("agents.roster.help", {
          defaultValue:
            "Each agent answers with its own model and its own tools. Agents are declared in config, so this page reads them and does not change them.",
        })}
      </p>
      <ul className="overflow-hidden rounded-[22px] bg-settings-surface">
        {agents.map((agent) => {
          const open = openAgent === agent.name;
          return (
            <li
              key={agent.name}
              className="border-b border-border/45 last:border-b-0"
              data-testid={`agent-row-${agent.name}`}
            >
              <button
                type="button"
                aria-expanded={open}
                onClick={() => setOpenAgent(open ? null : agent.name)}
                className="flex w-full flex-col gap-1 px-4 py-3.5 text-left transition-colors hover:bg-accent/40 sm:px-5"
              >
                <span className="flex min-w-0 flex-wrap items-baseline gap-2">
                  <span className="text-[14px] font-medium leading-5 text-foreground">
                    {agent.name}
                  </span>
                  <span className="text-[12px] text-muted-foreground">{agent.model_preset}</span>
                </span>
                {agent.description
                  ? (
                    <span className="max-w-[36rem] text-[12px] leading-5 text-muted-foreground">
                      {agent.description}
                    </span>
                  )
                  : null}
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5">
                  <BindingCount
                    label={t("agents.roster.toolGroups", { defaultValue: "Tool groups" })}
                    value={agent.tool_group_count}
                    testId={`agent-count-tool-groups-${agent.name}`}
                  />
                  <BindingCount
                    label={t("agents.roster.skills", { defaultValue: "Skills" })}
                    value={agent.skill_count}
                    testId={`agent-count-skills-${agent.name}`}
                  />
                  <BindingCount
                    label={t("agents.roster.delegates", { defaultValue: "Delegates" })}
                    value={agent.delegate_count}
                    testId={`agent-count-delegates-${agent.name}`}
                  />
                  {agent.has_addendum
                    ? (
                      <span
                        className="rounded-full bg-secondary px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-secondary-foreground"
                        data-testid={`agent-has-addendum-${agent.name}`}
                      >
                        {t("agents.roster.addendum", { defaultValue: "Addendum" })}
                      </span>
                    )
                    : null}
                </span>
              </button>
              {open
                ? (
                  <div className="border-t border-border/45 px-4 py-4 sm:px-5">
                    {/*
                      * A tab list of one, on purpose. Prompt is the tab this issue builds; Model,
                      * Abilities, Delegates, Automations and Pending are their own work, and they
                      * arrive beside this one rather than by rearranging it.
                      */}
                    <div
                      role="tablist"
                      aria-label={t("agents.detail.tabs", { defaultValue: "Agent settings" })}
                      className="mb-3 flex gap-1"
                    >
                      <span
                        role="tab"
                        aria-selected
                        className="rounded-full bg-secondary px-3 py-1 text-[12px] font-medium text-secondary-foreground"
                      >
                        {t("agents.detail.prompt", { defaultValue: "Prompt" })}
                      </span>
                    </div>
                    <AgentPromptPanel agent={agent.name} token={token} base={base} />
                  </div>
                )
                : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * How many, and never which.
 *
 * The label comes first and the number after it, so no string has to agree with a plural in eight
 * languages -- "Tool groups 1" is not wrong the way "1 tool groups" is.
 */
function BindingCount({
  label,
  value,
  testId,
}: {
  label: string;
  value: number;
  testId: string;
}) {
  return (
    <span className="text-[11.5px] text-muted-foreground" data-testid={testId}>
      {label}{" "}
      <span
        className={cn(
          "tabular-nums font-semibold",
          value > 0 ? "text-foreground/80" : "text-muted-foreground",
        )}
      >
        {value}
      </span>
    </span>
  );
}
