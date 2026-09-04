/**
 * One data connector on the Apps page.
 *
 * The row's job is the posture, which is the same job Settings → Identity does for a person:
 * who it acts as, and what it may do. Three of its lines are facts the deployment holds and an
 * operator cannot get anywhere else — a scope that was never granted, a class the ceiling caps,
 * and a token that stopped refreshing. A row that only said "enabled" would send somebody to a
 * log at 03:00 to find out why an automation is refused.
 *
 * Two things this row deliberately does not offer:
 *
 * - **No enable toggle.** Activation is declared in `connectors.active` and applied when the
 *   agent starts, because enabling a connector is what gives a package a token and a capability
 *   class. A toggle here would be a second authority contradicting the first, which is the rule
 *   the Agent Plugins panel already states.
 * - **No data browser.** The surface for data is the conversation. The only data shown is the
 *   test result, and it is there to prove a credential works rather than to be read.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { AtSign, Check, ChevronDown, Link2, PlayCircle, ShieldAlert, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ConnectorInfo, ConnectorOperationInfo } from "@/lib/types";

/** `read allow · mutate.remote approve (unattended: needs a grant)`, from the gate itself. */
function classSummary(
  connector: ConnectorInfo,
  labels: { unattendedGrant: string; unattendedDenied: string },
): { capability_class: string; interactive: string; unattended: string }[] {
  const seen = new Map<string, ConnectorOperationInfo>();
  for (const op of connector.operations) {
    if (op.enabled && !seen.has(op.capability_class)) seen.set(op.capability_class, op);
  }
  return Array.from(seen.entries()).map(([capability_class, op]) => ({
    capability_class,
    interactive: op.interactive.outcome,
    unattended:
      op.unattended.outcome === "allow" && op.unattended.grant_id
        ? labels.unattendedGrant
        : op.unattended.outcome === "deny"
          ? labels.unattendedDenied
          : op.unattended.outcome,
  }));
}

export function ConnectorAppsCatalogRow({
  connector,
  busy,
  testResult,
  onTest,
  onAttachChange,
  onConnect,
  className,
}: {
  connector: ConnectorInfo;
  busy: boolean;
  testResult: { ok: boolean; message: string } | null;
  onTest: (name: string) => void;
  /** Set when this row may change when the connector's operations reach the prompt (#204). */
  onAttachChange?: (name: string, attach: "always" | "mention" | "search") => void;
  onConnect?: (
    name: string,
    values: { clientId: string; clientSecret: string; account: string },
  ) => void;
  /** Set by the catalog so the row can span both columns of its grid. */
  className?: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [open, setOpen] = useState(false);
  const [connectOpen, setConnectOpen] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [account, setAccount] = useState("");

  const active = connector.state === "active";
  const classes = classSummary(connector, {
    unattendedGrant: tx("settings.connectors.viaGrant", "allowed by a grant"),
    unattendedDenied: tx("settings.connectors.needsGrant", "needs a grant"),
  });
  const missingScopes = connector.scopes.filter((scope) => !scope.granted);
  const refreshed = relativeTime(connector.refreshed_at);
  const tested = relativeTime(connector.tested_at);

  const identityLine = active
    ? [
        connector.acts_as
          ? t("settings.connectors.actsAs", {
              identity: connector.acts_as,
              defaultValue: "acts as {{identity}}",
            })
          : tx("settings.connectors.actsAsUnknown", "acts as: run Test to find out"),
        refreshed
          ? t("settings.connectors.refreshed", {
              when: refreshed,
              defaultValue: "refreshed {{when}}",
            })
          : tx("settings.connectors.neverRefreshed", "no token minted yet"),
      ].join(" · ")
    : connector.state === "not_activated"
      ? connector.problem || tx("settings.connectors.notActivated", "did not activate")
      : t("settings.connectors.inactiveHint", {
          key: "connectors.active",
          defaultValue: "not activated — add it to {{key}} in config",
        });

  return (
    <article className={cn("rounded-[14px] transition-colors hover:bg-muted/45", className)}>
      <div className="group flex min-w-0 items-start gap-3 px-3 py-3">
        <span
          className={cn(
            "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px]",
            active ? "bg-emerald-500/12 text-emerald-600 dark:text-emerald-300" : "bg-muted text-muted-foreground",
          )}
          aria-hidden
        >
          {active ? <ShieldCheck className="h-4 w-4" /> : <ShieldAlert className="h-4 w-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
            {/* `min-w-0` on the name and no `shrink-0` on the badge: with those the other way
               round, a narrow container kept the badge at full width and truncated the name to a
               single character. A name is the one thing a row cannot lose. */}
            <h3 className="min-w-0 truncate text-[14px] font-semibold leading-5 text-foreground">
              {connector.display_name}
            </h3>
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
              {tx("settings.apps.connectorLabel", "Connector")}
            </span>
          </div>
          <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">
            {identityLine}
          </p>
          {active && classes.length ? (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {/* One chip per capability class, so a read and a write are two things a reader can
                 tell apart at a glance rather than two sentences that wrapped into each other. */}
              {classes.map((row) => (
                <span
                  key={row.capability_class}
                  className="inline-flex items-center gap-1.5 rounded-full border border-border/70 px-2 py-0.5 text-[11px] leading-4 text-muted-foreground"
                >
                  <code className="font-semibold text-foreground">{row.capability_class}</code>
                  <span>{row.interactive}</span>
                  <span className="text-muted-foreground/70">
                    · {tx("settings.connectors.unattended", "unattended")} {row.unattended}
                  </span>
                </span>
              ))}
            </div>
          ) : null}
          {missingScopes.length && active ? (
            <p className="mt-1 text-[11.5px] leading-4 text-amber-600 dark:text-amber-300">
              {t("settings.connectors.missingScopes", {
                scopes: missingScopes.map((scope) => scope.short).join(", "),
                defaultValue: "not granted: {{scopes}} — operations that need them are unavailable",
              })}
            </p>
          ) : null}
          {connector.last_error ? (
            <p className="mt-1 line-clamp-2 text-[11.5px] leading-4 text-destructive-text">
              {connector.last_error}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {onConnect ? (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => setConnectOpen((value) => !value)}
              className="h-8 rounded-full px-3 text-[12px] font-semibold"
            >
              <Link2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {connector.acts_as
                ? tx("settings.connectors.reconnect", "Re-authorise")
                : tx("settings.connectors.connect", "Connect")}
            </Button>
          ) : null}
          {/* The deferral setting, the same one an MCP server has (#204, proposals/tool-search.md):
              active, one word away, and its operations in no prompt that did not ask. Three states,
              so the button cycles always -> mention -> search and shows the one in force: `mention`
              waits for the user to say @connector, `search` for the model to call tool_search. */}
          {onAttachChange && active ? (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() =>
                onAttachChange(
                  connector.name,
                  connector.attach === "always"
                    ? "mention"
                    : connector.attach === "mention"
                      ? "search"
                      : "always",
                )
              }
              className="h-8 rounded-full px-3 text-[12px] font-semibold"
            >
              <AtSign className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {connector.attach === "mention"
                ? tx("settings.connectors.attachOnMention", "Load on mention")
                : connector.attach === "search"
                  ? tx("settings.connectors.attachOnSearch", "Load on search")
                  : tx("settings.connectors.attachAlways", "Load every turn")}
            </Button>
          ) : null}
          {connector.testable ? (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => onTest(connector.name)}
              className="h-8 rounded-full px-3 text-[12px] font-semibold"
            >
              <PlayCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {busy ? tx("settings.connectors.testing", "Testing…") : tx("settings.mcp.test", "Test")}
            </Button>
          ) : null}
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="h-8 rounded-full px-2 text-muted-foreground"
          >
            <ChevronDown
              className={cn("h-4 w-4 transition-transform", open && "rotate-180")}
              aria-hidden
            />
          </Button>
        </div>
      </div>

      {connectOpen && onConnect ? (
        <form
          className="mx-3 mb-3 space-y-2 rounded-[14px] bg-background/55 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            onConnect(connector.name, { clientId, clientSecret, account });
          }}
        >
          <p className="text-[12px] leading-5 text-muted-foreground">
            {tx(
              "settings.connectors.connectHint",
              "Create an OAuth client for this deployment, then consent as the account this connector should act as. The browser comes back here and nothing is copied by hand.",
            )}
          </p>
          <Input
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            placeholder={tx("settings.connectors.clientId", "OAuth client id")}
            className="h-9 rounded-full bg-background/80 text-[12.5px]"
          />
          <Input
            type="password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            placeholder={tx("settings.connectors.clientSecret", "OAuth client secret")}
            className="h-9 rounded-full bg-background/80 text-[12.5px]"
          />
          <Input
            value={account}
            onChange={(event) => setAccount(event.target.value)}
            placeholder={tx("settings.connectors.account", "Account to consent as (optional)")}
            className="h-9 rounded-full bg-background/80 text-[12.5px]"
          />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setConnectOpen(false)}
              className="h-8 rounded-full px-3 text-[12px] font-semibold text-muted-foreground"
            >
              {tx("settings.actions.cancel", "Cancel")}
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy || !clientId.trim() || !clientSecret.trim()}
              className="h-8 rounded-full px-3 text-[12px] font-semibold"
            >
              {tx("settings.connectors.consent", "Open the consent screen")}
            </Button>
          </div>
        </form>
      ) : null}

      {testResult ? (
        <p
          className={cn(
            "mx-3 mb-3 rounded-[12px] px-3 py-2 text-[12px] leading-5",
            testResult.ok
              ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "bg-destructive/10 text-destructive-text",
          )}
        >
          {testResult.message}
        </p>
      ) : null}

      {open ? (
        <div className="mx-3 mb-3 space-y-4 rounded-[14px] bg-background/55 p-3">
          <div>
            <div className="text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
              {tx("settings.connectors.operations", "Operations")}
            </div>
            <div className="mt-2 space-y-1.5">
              {connector.operations.map((op) => (
                <div
                  key={op.name}
                  className={cn(
                    "flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[12px] leading-5",
                    !op.enabled && "opacity-55",
                  )}
                >
                  <code className="font-semibold text-foreground">{op.tool}</code>
                  <span className="rounded-full bg-muted px-1.5 text-[10.5px] font-semibold text-muted-foreground">
                    {op.capability_class}
                  </span>
                  <span className="text-muted-foreground">
                    {op.method} · {tx("settings.connectors.interactive", "interactive")}:{" "}
                    {op.interactive.outcome} · {tx("settings.connectors.unattended", "unattended")}:{" "}
                    {op.unattended.outcome}
                  </span>
                  {!op.enabled ? (
                    <span className="text-muted-foreground">
                      {tx("settings.connectors.operationDisabled", "not enabled here")}
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
              {tx("settings.connectors.scopes", "Scopes")}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] leading-5">
              {connector.scopes.map((scope) => (
                <span key={`${scope.capability_class}:${scope.scope}`} className="text-muted-foreground">
                  {scope.granted ? (
                    <Check className="mr-1 inline h-3 w-3 text-emerald-600 dark:text-emerald-300" aria-hidden />
                  ) : (
                    <span className="mr-1 text-amber-600 dark:text-amber-300" aria-hidden>
                      ✗
                    </span>
                  )}
                  <code className="text-foreground">{scope.short}</code>{" "}
                  <span className="text-muted-foreground/80">({scope.capability_class})</span>
                </span>
              ))}
            </div>
          </div>

          {connector.setup_fields.length ? (
            <div>
              <div className="text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                {tx("settings.connectors.settings", "Settings")}
              </div>
              <div className="mt-2 space-y-1 text-[12px] leading-5">
                {connector.setup_fields.map((field) => {
                  const value = connector.defaults[field.name] ?? "";
                  const shown = field.secret
                    ? tx("settings.connectors.heldInStore", "held in the secret store")
                    : value || tx("settings.connectors.unset", "not set");
                  return (
                    <div key={field.name} className="flex flex-wrap items-baseline gap-2">
                      <code className="text-foreground">{field.name}</code>
                      <span className="text-muted-foreground">{shown}</span>
                      {field.required ? (
                        <span className="text-[10.5px] uppercase tracking-wide text-muted-foreground/80">
                          {tx("settings.connectors.required", "required")}
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
              {connector.official_url ? (
                <a
                  href={connector.official_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 inline-block text-[12px] font-medium text-primary hover:underline"
                >
                  {tx("settings.connectors.officialUrl", "Where these come from")}
                </a>
              ) : null}
            </div>
          ) : null}

          <div>
            <div className="text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground">
              {tx("settings.connectors.reauthorise", "Re-authorise")}
            </div>
            <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
              {tx(
                "settings.connectors.reauthoriseHint",
                "Consent happens at a browser, as a person, so it is a command rather than a button here.",
              )}
            </p>
            <code className="mt-2 block overflow-x-auto rounded-[10px] bg-muted/70 px-2.5 py-2 text-[11.5px] text-foreground">
              {connector.authorize_command}
            </code>
          </div>

          {connector.tested_at ? (
            <p className="text-[11.5px] leading-4 text-muted-foreground">
              {t("settings.connectors.lastTest", {
                when: tested,
                summary: connector.test_summary,
                defaultValue: "last test {{when}}: {{summary}}",
              })}
            </p>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export default ConnectorAppsCatalogRow;
