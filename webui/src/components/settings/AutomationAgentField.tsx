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
  if (agents.length === 0) return null;

  const selected = agents.find((agent) => agent.name === value);

  return (
    <label className="block space-y-1.5">
      <span className="text-[12px] font-medium text-muted-foreground">
        {tx("settings.automations.fields.agent", "Runs as")}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={tx("settings.automations.fields.agent", "Runs as")}
        className="h-10 w-full rounded-[12px] border border-input bg-background px-3 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value={DEPLOYMENT_DEFAULT_AGENT}>
          {tx("settings.automations.agentDefault", "Default agent")}
        </option>
        {agents.map((agent) => (
          <option key={agent.name} value={agent.name}>
            {agent.description ? `${agent.name} — ${agent.description}` : agent.name}
          </option>
        ))}
      </select>
      <p className="text-[11.5px] leading-4 text-muted-foreground/80">
        {selected?.description
          ? selected.description
          : tx(
              "settings.automations.agentHelp",
              "The agent sets the ceiling for this run: its tool groups, and its own instructions. The job may narrow that, never widen it.",
            )}
      </p>
    </label>
  );
}
