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

import { Button } from "@/components/ui/button";
import { fetchMetricsLive } from "@/lib/api";
import type { MetricsGauge } from "@/lib/types";
import { cn } from "@/lib/utils";

/** How often the panel resamples. Matches the approval watcher's own poll, which feeds one gauge. */
const POLL_MS = 3_000;

export function MetricsLive({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [gauges, setGauges] = useState<MetricsGauge[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await fetchMetricsLive(token, base);
      setGauges(payload.gauges);
      setAvailable(payload.available !== false);
      setError(null);
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

  return (
    <div className="space-y-3" data-testid="metrics-live">
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
        {(gauges ?? []).map((gauge) => {
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
              <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground/80">
                {help}
              </p>
            </div>
          );
        })}
      </div>

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

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-[46rem] text-[11px] leading-4 text-muted-foreground/80">
          {tx(
            "metrics.live.scrape",
            "The same values are served in Prometheus text format at /metrics on the gateway's own port, which binds to loopback and is off unless gateway.metricsEnabled is set. A dash means the value could not be read, which is not the same as zero.",
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
