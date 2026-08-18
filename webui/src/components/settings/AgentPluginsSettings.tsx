import { useCallback, useEffect, useState } from "react";
import { CircleAlert, Loader2, Puzzle, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchAgentPlugins } from "@/lib/api";
import type { AgentPluginInfo, AgentPluginsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/**
 * Read-only view of installed Agent Plugins.
 *
 * There is deliberately no toggle. Activation is declared in `tools.agentPlugins` and reconciled
 * by the executor (nanoinfraorg/nanoinfra#141), because enabling a package that ships an
 * `mcp.json` grants a new stdio process. A control here would be a second authority contradicting
 * the config, so the panel reports state and names where the state comes from.
 */
export function AgentPluginsSettings() {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [payload, setPayload] = useState<AgentPluginsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (showLoading: boolean) => {
      if (showLoading) setLoading(true);
      return fetchAgentPlugins(getToken())
        .then((next) => {
          setPayload(next);
          setError(null);
        })
        .catch((cause: unknown) => {
          setError(cause instanceof Error ? cause.message : String(cause));
        })
        .finally(() => setLoading(false));
    },
    [getToken],
  );

  useEffect(() => {
    let cancelled = false;
    void fetchAgentPlugins(getToken())
      .then((next) => {
        if (!cancelled) {
          setPayload(next);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        {t("agentPlugins.loading", "Reading installed Agent Plugins…")}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-md border border-destructive/40 p-3 text-sm">
        <span className="flex items-center gap-2 text-destructive-text">
          <CircleAlert className="size-4" />
          {error}
        </span>
        <Button variant="outline" size="sm" onClick={() => void load(true)}>
          <RefreshCw className="size-4" />
          {t("common.retry", "Retry")}
        </Button>
      </div>
    );
  }

  const plugins = payload?.plugins ?? [];
  const unknown = payload?.unknown ?? [];

  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between gap-3">
        <h3 className="flex items-center gap-2 text-sm font-medium">
          <Puzzle className="size-4" />
          {t("agentPlugins.title", "Agent Plugins")}
        </h3>
        <Button variant="ghost" size="sm" onClick={() => void load(false)}>
          <RefreshCw className="size-4" />
          {t("common.refresh", "Refresh")}
        </Button>
      </header>

      {payload?.error ? (
        <p className="flex items-center gap-2 text-sm text-destructive-text">
          <CircleAlert className="size-4" />
          {payload.error}
        </p>
      ) : null}

      {plugins.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t(
            "agentPlugins.empty",
            "No Agent Plugins are installed. Packages live under the workspace's plugins/ directory.",
          )}
        </p>
      ) : (
        <ul className="space-y-2">
          {plugins.map((plugin) => (
            <AgentPluginRow key={plugin.name} plugin={plugin} t={t} />
          ))}
        </ul>
      )}

      {unknown.length > 0 ? (
        <p className="flex items-start gap-2 text-sm text-amber-600 dark:text-amber-500">
          <CircleAlert className="mt-0.5 size-4 shrink-0" />
          {t("agentPlugins.unknown", {
            defaultValue: "{{authority}} names no installed package: {{names}}",
            authority: payload?.authority ?? "tools.agentPlugins",
            names: unknown.join(", "),
          })}
        </p>
      ) : null}

      <footer className="border-t pt-2 text-xs text-muted-foreground">
        {t("agentPlugins.authority", {
          defaultValue:
            "Activation is declared in {{authority}} and applied by the executor. It is not editable here.",
          authority: payload?.authority ?? "tools.agentPlugins",
        })}
      </footer>
    </section>
  );
}

const STATE_STYLES: Record<AgentPluginInfo["state"], string> = {
  active: "text-emerald-600 dark:text-emerald-500",
  modified: "text-amber-600 dark:text-amber-500",
  inactive: "text-muted-foreground",
};

function AgentPluginRow({
  plugin,
  t,
}: {
  plugin: AgentPluginInfo;
  t: ReturnType<typeof useTranslation>["t"];
}) {
  const stateLabel = {
    active: t("agentPlugins.state.active", "active"),
    modified: t("agentPlugins.state.modified", "modified"),
    inactive: t("agentPlugins.state.inactive", "inactive"),
  }[plugin.state];

  return (
    <li className="rounded-md border p-3 text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-medium">
          {plugin.display_name}
          <span className="ml-2 font-normal text-muted-foreground">{plugin.name}</span>
        </span>
        <span className="flex items-baseline gap-2">
          {plugin.version ? (
            <span className="text-xs text-muted-foreground">v{plugin.version}</span>
          ) : null}
          <span className={cn("text-xs", STATE_STYLES[plugin.state])}>{stateLabel}</span>
        </span>
      </div>

      {plugin.description ? (
        <p className="mt-1 text-muted-foreground">{plugin.description}</p>
      ) : null}

      <dl className="mt-2 space-y-1 text-xs">
        {plugin.skills.length > 0 ? (
          <div className="flex gap-2">
            <dt className="w-14 shrink-0 text-muted-foreground">
              {t("agentPlugins.skills", "skills")}
            </dt>
            <dd>{plugin.skills.join(", ")}</dd>
          </div>
        ) : null}
        {plugin.mcp_servers.length > 0 ? (
          <div className="flex gap-2">
            <dt className="w-14 shrink-0 text-muted-foreground">
              {t("agentPlugins.mcp", "mcp")}
            </dt>
            {/* Naming the launch route matters: enabling this package starts a process, and the
                operator should see that it lands in the confined host rather than the agent. */}
            <dd>
              {plugin.mcp_servers.join(", ")}
              <span className="ml-1 text-muted-foreground">
                {t("agentPlugins.mcpRoute", "(stdio → mcp-host)")}
              </span>
            </dd>
          </div>
        ) : null}
      </dl>

      {plugin.state === "modified" ? (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-500">
          {t(
            "agentPlugins.modifiedHint",
            "Its content changed since it was activated, so it deactivated itself. Restart the gateway to review and re-bind it.",
          )}
        </p>
      ) : null}
      {plugin.state === "inactive" ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {t("agentPlugins.inactiveHint", "Add it to tools.agentPlugins in config.json to activate it.")}
        </p>
      ) : null}
    </li>
  );
}
