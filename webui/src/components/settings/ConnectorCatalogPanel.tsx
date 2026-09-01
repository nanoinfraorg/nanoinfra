/**
 * Browse and install data connectors from the catalog (#207).
 *
 * The Connectors tab listed only what was already installed, so there was no way to find one — while
 * the catalog had been publishing them, and this client had been treating every row as a skill.
 *
 * The panel's one rule: **what it grants is shown before the install button is useful.** A connector
 * is requests made with a live credential, and `hosts` is the field that decides where a token of
 * yours could go. A row that showed a name and an Install button would put the only check that
 * matters behind a click nobody makes.
 */
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, Loader2, Search, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { MarketplaceGrants, MarketplaceSkillSummary } from "@/lib/types";

export interface ConnectorCatalogRow extends MarketplaceSkillSummary {
  grants?: MarketplaceGrants;
}

export function ConnectorCatalogPanel({
  rows,
  loading,
  installing,
  error,
  lastInstalled,
  onSearch,
  onInstall,
}: {
  rows: ConnectorCatalogRow[] | null;
  loading: boolean;
  installing: string | null;
  error: string | null;
  /** What the last install said is still missing, so the row does not read as finished. */
  lastInstalled: { name: string; next_step?: string } | null;
  onSearch: (query: string) => void;
  onInstall: (row: ConnectorCatalogRow) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [query, setQuery] = useState("");

  const submit = useCallback(() => {
    const trimmed = query.trim();
    if (trimmed.length >= 2) onSearch(trimmed);
  }, [onSearch, query]);

  return (
    <section className="mt-4 rounded-[18px] border border-border/60 bg-card/40 p-3">
      <div className="flex min-w-0 items-baseline justify-between gap-3 px-1">
        <h3 className="text-[13px] font-semibold text-foreground">
          {tx("settings.connectors.catalogTitle", "Install a connector")}
        </h3>
        <p className="truncate text-[11.5px] text-muted-foreground">
          {tx(
            "settings.connectors.catalogHint",
            "Read what it grants before you install it.",
          )}
        </p>
      </div>

      <div className="mt-2 flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                submit();
              }
            }}
            placeholder={tx("settings.connectors.catalogSearch", "Search the catalog")}
            aria-label={tx("settings.connectors.catalogSearch", "Search the catalog")}
            className="h-9 rounded-full pl-9 text-[12.5px]"
          />
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={loading || query.trim().length < 2}
          onClick={submit}
          className="h-9 shrink-0 rounded-full px-3 text-[12px] font-semibold"
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            tx("settings.connectors.catalogSearchAction", "Search")
          )}
        </Button>
      </div>

      {error ? (
        <p className="mt-2 px-1 text-[11.5px] leading-4 text-destructive-text">{error}</p>
      ) : null}

      {rows !== null && rows.length === 0 && !loading ? (
        <p className="mt-3 px-1 text-[12px] text-muted-foreground">
          {tx("settings.connectors.catalogEmpty", "No connectors match that search.")}
        </p>
      ) : null}

      {rows?.map((row) => (
        <ConnectorCatalogRowView
          key={row.id}
          row={row}
          busy={installing === row.skill_id}
          disabled={installing !== null}
          lastInstalled={lastInstalled?.name === row.skill_id ? lastInstalled : null}
          onInstall={onInstall}
        />
      ))}
    </section>
  );
}

function ConnectorCatalogRowView({
  row,
  busy,
  disabled,
  lastInstalled,
  onInstall,
}: {
  row: ConnectorCatalogRow;
  busy: boolean;
  disabled: boolean;
  lastInstalled: { name: string; next_step?: string } | null;
  onInstall: (row: ConnectorCatalogRow) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const grants = row.grants;
  const operations = grants?.operations ?? [];
  const hosts = grants?.hosts ?? [];
  const scopes = grants?.scopes ?? [];

  return (
    <article className="mt-2 rounded-[14px] bg-background/55 p-3">
      <div className="flex min-w-0 items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline gap-2">
            <h4 className="truncate text-[13.5px] font-semibold leading-5 text-foreground">
              {row.name}
            </h4>
            <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground/70">
              {tx("settings.apps.connectorLabel", "Connector")}
            </span>
          </div>
          <p className="mt-0.5 text-[11.5px] leading-4 text-muted-foreground">
            {row.installs > 0
              ? t("settings.connectors.catalogInstalls", {
                  count: row.installs,
                  defaultValue: "{{count}} downloads",
                })
              : tx("settings.connectors.catalogNew", "Newly published")}
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant={row.installed ? "ghost" : "outline"}
          disabled={busy || disabled || row.installed}
          onClick={() => onInstall(row)}
          className="h-8 shrink-0 rounded-full px-3 text-[12px] font-semibold"
        >
          {busy ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Download className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          )}
          {row.installed
            ? tx("settings.connectors.catalogInstalled", "Installed")
            : tx("settings.connectors.catalogInstall", "Install")}
        </Button>
      </div>

      {/* Absent and "grants nothing" are different statements, so an unreadable manifest says so
          rather than rendering as a package that asked for nothing. */}
      {grants === undefined ? (
        <p className="mt-2 flex items-center gap-1.5 text-[11.5px] leading-4 text-amber-600 dark:text-amber-300">
          <ShieldAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {tx(
            "settings.connectors.catalogNoGrants",
            "The catalog could not read this package, so what it grants is unknown.",
          )}
        </p>
      ) : (
        <div className="mt-2 space-y-1.5">
          {operations.length ? (
            <ul className="space-y-0.5">
              {operations.map((operation) => (
                <li
                  key={operation.name}
                  className="flex min-w-0 items-baseline gap-2 text-[11.5px] leading-4"
                >
                  <code
                    className={cn(
                      "shrink-0 rounded-full px-1.5 py-px font-mono text-[10px]",
                      operation.class === "read"
                        ? "bg-muted text-muted-foreground"
                        : "bg-amber-500/15 text-amber-600 dark:text-amber-300",
                    )}
                  >
                    {operation.class}
                  </code>
                  <span className="truncate text-foreground">{operation.name}</span>
                  <span className="truncate text-muted-foreground/70">
                    {operation.method} {operation.path}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
          {hosts.length ? (
            <p className="text-[11.5px] leading-4 text-muted-foreground">
              {t("settings.connectors.catalogHosts", {
                hosts: hosts.join(", "),
                defaultValue: "A token can only reach: {{hosts}}",
              })}
            </p>
          ) : null}
          {scopes.length ? (
            <p className="text-[11.5px] leading-4 text-muted-foreground">
              {t("settings.connectors.catalogScopes", {
                scopes: scopes.join(", "),
                defaultValue: "The token would carry: {{scopes}}",
              })}
            </p>
          ) : null}
        </div>
      )}

      {/* Installed is not working: the package is on disk, and giving it a credential and adding it
          to `connectors.active` is a config decision an operator makes. */}
      {lastInstalled?.next_step ? (
        <p className="mt-2 rounded-[10px] bg-muted/60 px-2 py-1.5 text-[11.5px] leading-4 text-foreground">
          {lastInstalled.next_step}
        </p>
      ) : null}
    </article>
  );
}
