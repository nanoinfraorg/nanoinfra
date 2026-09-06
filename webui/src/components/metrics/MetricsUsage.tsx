/**
 * Usage: what this deployment spent (#235, phase 1).
 *
 * The two components that used to sit inside Settings, plus the columns the store has recorded
 * since #176 and never showed anyone. Six of `llm_calls`' twenty-one columns reached no pixel
 * before this, and `finish_reason` was collapsed to a boolean that read a **truncated** answer as
 * a success.
 *
 * The window control is here rather than in Settings because it changes what the tables below
 * mean, and the store keeps four hundred days while the page asked for thirty.
 */
import { Fragment, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { TokenUsageHeatmap } from "@/components/settings/TokenUsageHeatmap";
import { TokenUsageSummary } from "@/components/settings/TokenUsageSummary";
import { Button } from "@/components/ui/button";
import { fetchSettingsUsage } from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The windows offered. Bounded by what the store retains, which is 400 days of `llm_calls`. */
const WINDOWS = [7, 30, 90, 365] as const;

function formatTokens(value: number): string {
  return new Intl.NumberFormat().format(value);
}

/** The five sources a call can carry. `system` is the default for anything unattributed. */
function sourceLabel(source: string, tx: (key: string, fallback: string) => string): string {
  if (source === "user") return tx("settings.usage.sources.user", "Chat");
  if (source === "api") return tx("settings.usage.sources.api", "API");
  if (source === "cron") return tx("settings.usage.sources.cron", "Automations");
  if (source === "dream") return tx("settings.usage.sources.dream", "Memory");
  return tx("settings.usage.sources.system", "System");
}

/** One secondary measurement, under the model row it belongs to. */
function DetailRow({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string | null;
}) {
  return (
    <div className="flex min-w-0 gap-2 text-[11.5px]">
      <dt className="w-[150px] shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-foreground">
        <span className="tabular-nums">{value}</span>
        {hint ? <span className="ml-1.5 text-muted-foreground/80">{hint}</span> : null}
      </dd>
    </div>
  );
}

/** USD with enough places to be checkable: a month can be cents or hundreds. */
function formatUsd(value: number): string {
  const digits = value > 0 && value < 1 ? 4 : 2;
  return `$${value.toFixed(digits)}`;
}

export function MetricsUsage({
  settings,
  token,
  base = "",
  onSettings,
}: {
  settings: SettingsPayload | null;
  token: string;
  base?: string;
  onSettings?: (payload: SettingsPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const usage = settings?.usage;
  const [window_, setWindow] = useState<number>(usage?.window_days ?? 30);
  const [nonce, setNonce] = useState(0);
  const [loading, setLoading] = useState(false);
  // Which model's secondary measurements are open. Fourteen columns do not fit on a laptop, and
  // the four below are checks on the ten above rather than numbers read every day.
  const [openModel, setOpenModel] = useState<string | null>(null);

  // The payload the page already holds carries a window; a change refetches rather than
  // recomputing locally, because the breakdowns are grouped in SQL and cannot be re-sliced here.
  useEffect(() => {
    if (!token || !settings) return;
    if (nonce === 0 && window_ === (usage?.window_days ?? 30)) return;
    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const fresh = await fetchSettingsUsage(token, window_, base);
        if (!cancelled && fresh) onSettings?.({ ...settings, usage: fresh });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [window_, nonce, token, base]);

  const rows = usage?.providers_30d ?? [];
  const sources = usage?.sources_window ?? [];
  const failures = usage?.failures ?? [];
  const cost = usage?.cost_usd_window;
  const priced = usage?.priced_models ?? 0;

  return (
    <div className="space-y-4" data-testid="metrics-usage">
      {/* The window first, because every number under it is scoped by it. */}
      <div className="flex flex-wrap items-center gap-2" data-testid="metrics-window">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {tx("metrics.window.label", "Window")}
        </span>
        {WINDOWS.map((days) => (
          <button
            key={days}
            type="button"
            aria-pressed={window_ === days}
            onClick={() => setWindow(days)}
            className={cn(
              "rounded-full px-2.5 py-1 text-[11.5px] leading-4 transition-colors",
              window_ === days
                ? "bg-primary/15 text-foreground"
                : "bg-muted/60 text-muted-foreground hover:text-foreground",
            )}
            data-testid={`metrics-window-${days}`}
          >
            {tx("metrics.window.days", "{{count}}d", { count: days })}
          </button>
        ))}
        {loading
          ? (
            <span className="text-[11px] text-muted-foreground" data-testid="metrics-window-loading">
              {tx("metrics.window.loading", "Loading…")}
            </span>
          )
          : null}
      </div>

      {/* Cost, which is the question the page could not answer at all before. */}
      <div
        className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
        data-testid="metrics-cost"
      >
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {tx("metrics.cost.label", "Spend, this window")}
        </p>
        {cost == null
          ? (
            <>
              <p className="mt-0.5 text-[20px] font-semibold text-foreground">
                {tx("metrics.cost.unpriced", "No prices configured")}
              </p>
              <p className="mt-1 max-w-[46rem] text-[11.5px] leading-4 text-muted-foreground">
                {tx(
                  "metrics.cost.unpricedHelp",
                  "Set the rates on the model itself, under Settings → Models → Pricing — or once for a whole provider, which is the answer for a local one. Four rates, because a cached read costs a fraction of a fresh prompt token and one rate over the total over-bills a working cache by more than half.",
                )}
              </p>
            </>
          )
          : (
            <>
              <p className="mt-0.5 text-[20px] font-semibold tabular-nums text-foreground">
                {formatUsd(cost)}
              </p>
              <p className="mt-1 text-[11.5px] leading-4 text-muted-foreground">
                {tx("metrics.cost.priced", "{{priced}} of {{total}} models priced", {
                  priced,
                  total: rows.length,
                })}
              </p>
            </>
          )}
      </div>

      {/* What started the turns. Recorded per call since the store existed and readable only
          inside one heatmap cell's tooltip, so "what does automation cost me" had no answer. */}
      {sources.length > 0
        ? (
          <div
            className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
            data-testid="metrics-sources"
          >
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tx("metrics.sources.title", "By source, last {{count}} days", {
                count: usage?.window_days ?? 30,
              })}
            </p>
            <ul className="flex flex-wrap gap-x-6 gap-y-1.5">
              {sources.map((row) => (
                <li
                  key={row.source}
                  className="flex items-baseline gap-1.5 text-[11.5px]"
                  data-testid={`metrics-source-${row.source}`}
                >
                  <span className="text-muted-foreground">{sourceLabel(row.source, tx)}</span>
                  <span className="font-semibold tabular-nums text-foreground">
                    {formatTokens(row.total_tokens)}
                  </span>
                  <span className="text-muted-foreground/80">
                    {tx("metrics.sources.calls", "{{count}} calls", { count: row.requests })}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )
        : null}

      <TokenUsageSummary usage={usage} />

      {/* Per model, with the columns Settings never showed. */}
      {rows.length > 0
        ? (
          <div
            className="overflow-x-auto rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
            data-testid="metrics-models"
          >
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tx("metrics.models.title", "By model, last {{count}} days", {
                count: usage?.window_days ?? 30,
              })}
            </p>
            <table className="w-full text-left text-[11.5px]">
              <thead className="text-muted-foreground">
                <tr>
                  <th className="pb-1 pr-3 font-medium">{tx("metrics.models.model", "Model")}</th>
                  <th className="pb-1 pr-3 text-right font-medium">{tx("metrics.models.in", "In")}</th>
                  <th className="pb-1 pr-3 text-right font-medium">{tx("metrics.models.out", "Out")}</th>
                  <th className="pb-1 pr-3 text-right font-medium">
                    {tx("metrics.models.cacheRead", "Cache read")}
                  </th>
                  <th className="pb-1 pr-3 text-right font-medium">
                    {tx("metrics.models.cacheWrite", "Cache write")}
                  </th>
                  <th className="pb-1 pr-3 text-right font-medium">{tx("metrics.models.calls", "Calls")}</th>
                  <th className="pb-1 pr-3 text-right font-medium">
                    {tx("metrics.models.truncated", "Truncated")}
                  </th>
                  <th className="pb-1 pr-3 text-right font-medium">{tx("metrics.models.ttft", "TTFT")}</th>
                  <th className="pb-1 pr-3 text-right font-medium">
                    {tx("metrics.models.wall", "Wall clock")}
                  </th>
                  <th className="pb-1 text-right font-medium">{tx("metrics.models.cost", "Cost")}</th>
                </tr>
              </thead>
              <tbody className="text-foreground/90">
                {rows.map((row) => {
                  const streamed = row.streamed_requests ?? 0;
                  const ttft = streamed > 0 ? row.ttft_ms / streamed : null;
                  const wall = row.requests > 0 ? (row.duration_ms ?? 0) / row.requests : null;
                  const key = `${row.provider}/${row.model}`;
                  const reported = row.provider_requests ?? 0;
                  const estimated = row.estimated_requests ?? 0;
                  const generation = row.generation_ms ?? 0;
                  // Output tokens per second of generation, which wall clock cannot give: it
                  // includes the wait before the first token.
                  const throughput = generation > 0
                    ? (row.completion_tokens / (generation / 1000))
                    : null;
                  return (
                    <Fragment key={key}>
                    <tr
                      className="cursor-pointer border-t border-border/30 hover:bg-muted/20"
                      onClick={() => setOpenModel(openModel === key ? null : key)}
                      data-testid={`metrics-model-${row.model}`}
                    >
                      <td className="py-1.5 pr-3">
                        <code className="text-foreground">{row.model}</code>{" "}
                        <span className="text-muted-foreground">{row.provider}</span>
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatTokens(row.prompt_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatTokens(row.completion_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatTokens(row.cached_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatTokens(row.cache_write_tokens ?? 0)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatTokens(row.requests)}
                        {row.failed_requests > 0
                          ? (
                            <span className="text-amber-500/90">
                              {" "}/ {row.failed_requests}
                            </span>
                          )
                          : null}
                      </td>
                      {/* Not a failure and not a success: the answer arrived cut off. */}
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {(row.truncated_requests ?? 0) > 0
                          ? (
                            <span className="text-amber-500/90">{row.truncated_requests}</span>
                          )
                          : "—"}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {ttft == null ? "—" : `${(ttft / 1000).toFixed(1)}s`}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {wall == null ? "—" : `${(wall / 1000).toFixed(1)}s`}
                      </td>
                      <td className="py-1.5 text-right tabular-nums">
                        {row.cost_usd == null
                          ? <span className="text-muted-foreground">—</span>
                          : (
                            <span
                              // A cost inherited from the provider's default is a weaker claim
                              // than one typed for this model. Marked rather than footnoted,
                              // because the reader is comparing a column against an invoice.
                              title={row.cost_source === "provider"
                                ? tx(
                                  "metrics.models.costFromProvider",
                                  "From the provider's default rates, not this model's own",
                                )
                                : undefined}
                              className={cn(
                                row.cost_source === "provider" && "text-muted-foreground",
                              )}
                            >
                              {formatUsd(row.cost_usd)}
                              {row.cost_source === "provider" ? "*" : ""}
                            </span>
                          )}
                      </td>
                    </tr>
                    {openModel === key
                      ? (
                        <tr className="border-t border-border/20 bg-muted/15">
                          <td colSpan={10} className="px-1 py-2">
                            <dl
                              className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2"
                              data-testid={`metrics-model-detail-${row.model}`}
                            >
                              {/* The four measurements the ten columns above are checked against.
                                  Each was stored and displayed nowhere. */}
                              <DetailRow
                                label={tx("metrics.models.reported", "Reported / estimated")}
                                value={tx(
                                  "metrics.models.reportedValue",
                                  "{{reported}} of {{total}} calls reported by the provider · {{tokens}} tokens tokenized locally",
                                  {
                                    reported: reported,
                                    total: row.requests,
                                    tokens: formatTokens(row.estimated_tokens),
                                  },
                                )}
                                hint={estimated > 0
                                  ? tx(
                                    "metrics.models.reportedHint",
                                    "An estimate is nanoinfra's own tokenizer, not a bill.",
                                  )
                                  : null}
                              />
                              <DetailRow
                                label={tx("metrics.models.throughput", "Throughput")}
                                value={throughput == null
                                  ? "—"
                                  : tx("metrics.models.throughputValue", "{{rate}} output tok/s", {
                                    rate: throughput.toFixed(1),
                                  })}
                                hint={tx(
                                  "metrics.models.throughputHint",
                                  "Output tokens over generation time, which wall clock is not.",
                                )}
                              />
                              <DetailRow
                                label={tx("metrics.models.measured", "Measured vs reported out")}
                                value={`${formatTokens(row.measured_completion_tokens)} / ${
                                  formatTokens(row.completion_tokens)
                                }`}
                                hint={tx(
                                  "metrics.models.measuredHint",
                                  "The gap is what the token calibration learns from.",
                                )}
                              />
                              <DetailRow
                                label={tx("metrics.models.streamed", "Streamed / not")}
                                value={`${formatTokens(row.streamed_requests ?? 0)} / ${
                                  formatTokens(Math.max(0, row.requests - (row.streamed_requests ?? 0)))
                                }`}
                                hint={tx(
                                  "metrics.models.streamedHint",
                                  "TTFT is averaged over {{count}} timed calls; it means nothing for a call that did not stream.",
                                  { count: row.timed_requests },
                                )}
                              />
                            </dl>
                          </td>
                        </tr>
                      )
                      : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
        : null}

      {/* Why the failures failed, which "16 failed (4%)" could never say. */}
      {failures.length > 0
        ? (
          <div
            className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
            data-testid="metrics-failures"
          >
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tx("metrics.failures.title", "Why calls failed")}
            </p>
            <ul className="space-y-1">
              {failures.map((row) => (
                <li
                  key={`${row.provider}:${row.error_kind}:${row.status_code}`}
                  className="flex flex-wrap items-baseline gap-x-2 text-[11.5px]"
                  data-testid={`metrics-failure-${row.error_kind}`}
                >
                  <span className="tabular-nums font-semibold text-foreground">
                    {row.requests}
                  </span>
                  <code className="text-foreground/90">{row.error_kind || "unknown"}</code>
                  {row.status_code > 0
                    ? <span className="text-muted-foreground">HTTP {row.status_code}</span>
                    : null}
                  <span className="text-muted-foreground">{row.provider}</span>
                </li>
              ))}
            </ul>
          </div>
        )
        : null}

      <TokenUsageHeatmap usage={usage} timeZone={settings?.agent.timezone} />

      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          className="h-7 rounded-full px-3 text-[11.5px]"
          onClick={() => setNonce((n) => n + 1)}
          data-testid="metrics-refresh"
        >
          {tx("metrics.refresh", "Refresh")}
        </Button>
      </div>
    </div>
  );
}
