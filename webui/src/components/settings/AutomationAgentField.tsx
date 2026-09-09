/**
 * The agent an automation runs as -- nanoinfraorg/nanoinfra#257.
 *
 * A job used to run with the deployment's default agent and no way to say otherwise. Naming an
 * agent narrows it: the agent's tool groups become the ceiling for that turn, and the job's own
 * "Skills to load" picker may only stay inside the agent's list. The server refuses the widening
 * cases, so this field offers choices and never validates authority.
 *
 * **The field disappears when the deployment names no agents**, which is every deployment today.
 * An empty picker would advertise a concept an operator cannot use and cannot discover from here;
 * the agents themselves are declared in config, where authority lives.
 */
import { useEffect, useState } from "react";

import { fetchNamedAgents } from "@/lib/api";
import type { NamedAgentSummary } from "@/lib/api";

/** The value that means "the deployment's default agent" -- the same empty string the job stores. */
export const DEPLOYMENT_DEFAULT_AGENT = "";

/**
 * The configured agents, or none.
 *
 * A failed read is "no named agents" rather than an error surface: the roster is an addition to a
 * form that worked without it, so a gateway that cannot answer must leave the rest of the editor
 * usable instead of blocking a save on a field the deployment may not even use.
 */
export function useNamedAgents(token: string, base: string = ""): NamedAgentSummary[] {
  const [agents, setAgents] = useState<NamedAgentSummary[]>([]);

  useEffect(() => {
    if (!token) {
      setAgents([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchNamedAgents(token, base);
        if (!cancelled) setAgents(payload.agents ?? []);
      } catch {
        if (!cancelled) setAgents([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, base]);

  return agents;
}

/**
 * The roster as a select, with no opinion about what binding it is editing.
 *
 * Two callers: the automation field below, and the channel binding in
 * `channels/ChannelAgentField.tsx`. One control, because an operator who has bound an agent to a
 * job should recognise the one that binds an agent to a channel -- and because the empty-roster
 * rule, the *Default agent* option and the `name — description` option label are the same
 * decisions in both places.
 */
export function AgentSelectField({
  agents,
  value,
  onChange,
  label,
  defaultOptionLabel,
  help,
}: {
  agents: NamedAgentSummary[];
  value: string;
  onChange: (agent: string) => void;
  label: string;
  defaultOptionLabel: string;
  /** Shown when the selected agent has no description of its own. */
  help: string;
}) {
  if (agents.length === 0) return null;

  const selected = agents.find((agent) => agent.name === value);

  return (
    <label className="block space-y-1.5">
      <span className="text-[12px] font-medium text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={label}
        className="h-10 w-full rounded-[12px] border border-input bg-background px-3 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value={DEPLOYMENT_DEFAULT_AGENT}>{defaultOptionLabel}</option>
        {agents.map((agent) => (
          <option key={agent.name} value={agent.name}>
            {agent.description ? `${agent.name} — ${agent.description}` : agent.name}
          </option>
        ))}
      </select>
      <p className="text-[11.5px] leading-4 text-muted-foreground/80">
        {selected?.description ? selected.description : help}
      </p>
    </label>
  );
}

export function AutomationAgentField({
  agents,
  value,
  onChange,
  tx,
}: {
  agents: NamedAgentSummary[];
  value: string;
  onChange: (agent: string) => void;
  tx: (key: string, fallback: string) => string;
}) {
  return (
    <AgentSelectField
      agents={agents}
      value={value}
      onChange={onChange}
      label={tx("settings.automations.fields.agent", "Runs as")}
      defaultOptionLabel={tx("settings.automations.agentDefault", "Default agent")}
      help={tx(
        "settings.automations.agentHelp",
        "The agent sets the ceiling for this run: its tool groups, and its own instructions. The job may narrow that, never widen it.",
      )}
    />
  );
}
