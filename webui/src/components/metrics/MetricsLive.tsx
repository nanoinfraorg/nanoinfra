/**
 * Live: the numbers rows cannot answer (#235, phase 2).
 *
 * `llm_calls` and `tool_calls` say what *happened*. Nothing in them says how many sockets are open
 * right now or how many suspended actions are waiting for a person, and those are the numbers an
 * operator watches rather than reviews.
 *
 * **This exists because a `/metrics` endpoint alone would have shipped gauges only a Prometheus
 * install could see** — which is most deployments, and the demo. Both read the same sampler, so a
 * scrape and this panel cannot disagree.
 *
 * `null` is rendered as "—" and never as `0`. "The executor did not answer" and "nothing is
 * pending" are different facts, and a dashboard that draws them identically lies on the one day
 * it matters.
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { MetricsCharts } from "@/components/metrics/MetricsCharts";
import { Meter } from "@/components/metrics/Meter";
import { Sparkline } from "@/components/metrics/Sparkline";
import { Button } from "@/components/ui/button";
import { fetchMetricsLive } from "@/lib/api";
import type { MetricsGauge } from "@/lib/types";
import { cn } from "@/lib/utils";

/** How often the panel resamples. Matches the approval watcher's own poll, which feeds one gauge. */
const POLL_MS = 3_000;

/**
 * How many samples the sparklines hold: three minutes at the poll above.
 *
 * **In the browser, deliberately.** Nothing stores gauge history — the nine are sampled at read,
 * which is what stops them going stale — so a server-side ring buffer would need its own timer and
 * every deployment would pay memory to record numbers for a tab most operators never open. The
 * cost of keeping it here is that a reload starts over, and the panel says so rather than
 * implying it holds yesterday.
 */
const HISTORY = 60;

/** The two that are one ratio rather than two facts, and are drawn as a meter instead. */
const CONTEXT_USED = "nanoinfra_context_tokens_used";
const CONTEXT_LIMIT = "nanoinfra_context_tokens_limit";

function compact(value: number): string {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 })
    .format(value);
}

export function MetricsLive({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [gauges, setGauges] = useState<MetricsGauge[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // One bounded series per gauge name. `null` enters the series for a sample that could not be
  // read, so the sparkline breaks its line there rather than drawing across the gap.
  const [history, setHistory] = useState<Record<string, Array<number | null>>>({});

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await fetchMetricsLive(token, base);
      setGauges(payload.gauges);
      setAvailable(payload.available !== false);
      setError(null);
      setHistory((current) => {
        const next: Record<string, Array<number | null>> = { ...current };
        for (const gauge of payload.gauges) {
          next[gauge.name] = [...(current[gauge.name] ?? []), gauge.value].slice(-HISTORY);
        }
        return next;
      });
    } catch {
      // A sample that fails leaves the last one on screen rather than blanking the panel: a
      // dropped poll is not news, and a panel that flickers to empty teaches people to ignore it.
      setError(tx("metrics.live.unreachable", "Could not read the gauges"));
    }
  }, [token, base]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  if (!available) {
    return (
      <div
        className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
        data-testid="metrics-live-unavailable"
      >
        <p className="text-[12.5px] leading-5 text-muted-foreground">
          {tx(
            "metrics.live.notGateway",
            "These numbers live in the gateway process. This WebUI is talking to one it does not run, so there is nothing here to sample.",
          )}
        </p>
      </div>
    );
  }

  const contextUsed = (gauges ?? []).find((gauge) => gauge.name === CONTEXT_USED);
  const contextLimit = (gauges ?? []).find((gauge) => gauge.name === CONTEXT_LIMIT);

  return (
    <div className="space-y-3" data-testid="metrics-live">
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
        {(gauges ?? []).filter((gauge) => gauge.name !== CONTEXT_USED && gauge.name !== CONTEXT_LIMIT).map((gauge) => {
          // A non-zero alerting gauge is somebody waiting or something backing up.
          const raised = gauge.alerting && (gauge.value ?? 0) > 0;
          // The sampler is Python and has no locale. Rather than build an i18n layer into the
          // exposition format -- where a translated `# HELP` line would be wrong for Prometheus
          // anyway -- the panel looks up the gauge by its stable name and falls back to the words
          // the server sent. A locale that has not translated a gauge shows English, not a blank.
          const label = t(`metrics.gauge.${gauge.name}.label`, { defaultValue: gauge.label });
          const help = t(`metrics.gauge.${gauge.name}.help`, { defaultValue: gauge.help });
          return (
            <div
              key={gauge.name}
              title={help}
              className={cn(
                "rounded-[16px] px-4 py-3",
                raised ? "bg-amber-500/10" : "bg-settings-surface",
              )}
              data-testid={`metrics-gauge-${gauge.name}`}
            >
              <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {label}
              </p>
              <p
                className={cn(
                  "mt-0.5 text-[22px] font-semibold leading-7 tabular-nums",
                  raised ? "text-amber-500" : "text-foreground",
                )}
              >
                {gauge.value == null ? "—" : new Intl.NumberFormat().format(gauge.value)}
              </p>
              {/* The trend half of the tile. Absent when the gauge is unreadable: an empty plot
                  would read as flat at zero, which is the one lie this panel has avoided. */}
              {gauge.value == null
                ? null
                : (
                  <Sparkline
                    values={history[gauge.name] ?? []}
                    className={raised ? "text-amber-500" : "text-primary"}
                    ariaLabel={tx(
                      "metrics.live.trend",
                      "{{label}} over the last three minutes",
                    ).replace("{{label}}", label)}
                  />
                )}
              <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground/80">
                {help}
              </p>
            </div>
          );
        })}
      </div>

      {/* The context pair as one meter: `used` and `limit` were never two facts. */}
      {contextUsed || contextLimit
        ? (
          <Meter
            label={tx("metrics.live.context", "Context window")}
            used={contextUsed?.value ?? null}
            limit={contextLimit?.value ?? null}
            format={compact}
            testId="metrics-context-meter"
          />
        )
        : null}

      {gauges == null
        ? (
          <p className="text-[11.5px] text-muted-foreground" data-testid="metrics-live-loading">
            {tx("metrics.live.loading", "Sampling…")}
          </p>
        )
        : null}

      {error
        ? (
          <p className="text-[11.5px] text-amber-500/90" data-testid="metrics-live-error">
            {error}
          </p>
        )
        : null}

      {/* The two charts a level cannot draw: a rate and a tail. */}
      <MetricsCharts token={token} base={base} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-[46rem] text-[11px] leading-4 text-muted-foreground/80">
          {tx(
            "metrics.live.scrape",
            "The trend under each number is the last three minutes, kept in this tab and lost on reload — nothing stores gauge history, because these are sampled when read. The same values are served in Prometheus text format at /metrics on the gateway's own port, which binds to loopback and is off unless gateway.metricsEnabled is set. A dash means the value could not be read, which is not the same as zero.",
          )}
        </p>
        <Button
          type="button"
          variant="ghost"
          className="h-7 shrink-0 rounded-full px-3 text-[11.5px]"
          onClick={() => void load()}
          data-testid="metrics-live-refresh"
        >
          {tx("metrics.refresh", "Refresh")}
        </Button>
      </div>
    </div>
  );
}
