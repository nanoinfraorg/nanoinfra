/**
 * What this deployment offers an agent to bind to -- skills, connectors, MCP servers.
 *
 * Extracted from `AgentDetail` when the default agent's page gained the same pickers (#266). One
 * hook rather than two copies of the same three reads, because the interesting rules are the
 * quiet ones and a second copy is where they get lost: only *active* connectors and only
 * *installed* MCP servers are bindings an agent can actually be given, and a failure of any one
 * read has to leave that catalogue empty rather than take the other two down with it.
 */
import { useEffect, useState } from "react";

import { fetchConnectors, fetchMcpPresets, fetchSkills } from "@/lib/api";

/** One catalogue's rows: the name config stores, and a line for the tooltip. */
export interface AgentCatalogues {
  skills: Array<{ name: string; description?: string }>;
  connectors: Array<{ name: string; description?: string }>;
  mcpServers: Array<{ name: string; description?: string }>;
}

export const NO_AGENT_CATALOGUES: AgentCatalogues = {
  skills: [],
  connectors: [],
  mcpServers: [],
};

export function useAgentCatalogues(token: string, base = ""): AgentCatalogues {
  const [catalogues, setCatalogues] = useState<AgentCatalogues>(NO_AGENT_CATALOGUES);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    void (async () => {
      // Three independent reads, and a failure of any one of them is an empty catalogue rather
      // than a closed editor: an unreachable connector list must not stop somebody naming an
      // agent. `allSettled` is what keeps one 404 from taking the other two with it.
      const [skills, connectors, mcp] = await Promise.allSettled([
        fetchSkills(token, base),
        fetchConnectors(token, base),
        fetchMcpPresets(token, base),
      ]);
      if (cancelled) return;
      setCatalogues({
        // `?? []` on each: these payloads are parsed, not validated, and a gateway answering a
        // different shape must leave the catalogue empty rather than throw out of this effect.
        skills: skills.status === "fulfilled"
          ? (skills.value.skills ?? []).map((skill) => ({
            name: skill.name,
            description: skill.description,
          }))
          : [],
        // What config *activates*, not what is merely installed: a connector this deployment has
        // not turned on is not a binding an agent can be given.
        connectors: connectors.status === "fulfilled"
          ? (connectors.value.connectors ?? [])
            .filter((connector) => connector.state === "active")
            .map((connector) => ({
              name: connector.name,
              description: connector.display_name || connector.description,
            }))
          : [],
        mcpServers: mcp.status === "fulfilled"
          ? (mcp.value.presets ?? [])
            .filter((preset) => preset.installed)
            .map((preset) => ({
              name: preset.name,
              description: preset.display_name || preset.description,
            }))
          : [],
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [token, base]);

  return catalogues;
}
