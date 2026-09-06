/**
 * The two charts a level cannot draw — nanoinfraorg/nanoinfra#274.
 *
 * `Calls per minute` is a rate derived from successive reads of a cumulative counter, which is
 * what `rate()` does and is sound only because those counters never go down. `Response latency`
 * is the histogram's buckets, which is the only thing here that can answer "how slow is the tail".
 *
 * Both live under the gauges on the Live tab, and both share its poll — one read of one route, no
 * second timer.
 *
 * **Colours were computed, not chosen.** `validate_palette.js` against this WebUI's own chart
 * surfaces (`#f7f7f6` light, `#383838` dark) on categorical slots 1 and 2: CVD separation ΔE 9.2
 * deutan on light, 9.4 on dark, both PASS. The light surface returned a contrast WARN for slot 2
 * (2.99:1), and that warning is not dismissable — it obligates visible labels, which is why both
 * series are direct-labelled and every bar prints its count.
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchMetricsCounters } from "@/lib/api";
import type { MetricsCountersPayload } from "@/lib/types";

/** Three minutes at the Live tab's poll, the same window the sparklines hold. */
const HISTORY = 60;
const POLL_MS = 3_000;

const SERIES = [
  { key: "nanoinfra_llm_calls_total", label: "metrics.charts.llmCalls", fallback: "LLM calls" },
  { key: "nanoinfra_tool_calls_total", label: "metrics.charts.toolCalls", fallback: "Tool calls" },
] as const;

/** Slot 1 and slot 2 of the validated categorical theme, per mode. */
const SERIES_CLASS = ["text-[#2a78d6] dark:text-[#3987e5]", "text-[#eb6834] dark:text-[#d95926]"];

const WIDTH = 320;
const HEIGHT = 72;

/**
 * The payload is validated rather than trusted.
 *
 * These charts render *inside* the Live tab, so a shape they did not expect takes every gauge
 * above them down with it — which is exactly what `MetricsScaleRow` did to the Usage tab before it
 * was hardened, and the lesson did not travel here on the first pass. An older gateway that does
 * not serve this route at all is precisely that shape.
 */
function isPayload(value: unknown): value is MetricsCountersPayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<MetricsCountersPayload>;
  return Array.isArray(candidate.counters) && Array.isArray(candidate.histograms);
}

function total(payload: MetricsCountersPayload, name: string): number {
  return payload.counters
    .filter((row) => row.name === name)
    .reduce((sum, row) => sum + (typeof row.value === "number" ? row.value : 0), 0);
}

export function MetricsCharts({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const [payload, setPayload] = useState<MetricsCountersPayload | null>(null);
  // Cumulative totals over time, one series per counter. The chart plots the difference between
  // consecutive samples, which is the rate; the totals themselves would be a line that only ever
  // climbs and says nothing about now.
  const [totals, setTotals] = useState<Record<string, number[]>>({});

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const fresh = await fetchMetricsCounters(token, base);
      if (!isPayload(fresh)) return;
      setPayload(fresh);
      setTotals((current) => {
        const next: Record<string, number[]> = { ...current };
        for (const { key } of SERIES) {
          next[key] = [...(current[key] ?? []), total(fresh, key)].slice(-HISTORY);
        }
        return next;
      });
    } catch {
      // The charts are the optional half of this tab. A failed read leaves the gauges above
      // untouched rather than putting an error band over them.
    }
  }, [token, base]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  if (!payload) return null;

  const rates = SERIES.map(({ key }) => {
    const samples = totals[key] ?? [];
    // One difference per adjacent pair, scaled to per-minute from the poll interval.
    return samples
      .slice(1)
      .map((value, index) => Math.max(0, value - samples[index]) * (60_000 / POLL_MS));
  });
  const peak = Math.max(1, ...rates.flat());
  const hasRate = rates.some((series) => series.length >= 2);

  const histogram = payload.histograms.find(
    (row) => row.name === "nanoinfra_llm_duration_ms" && Array.isArray(row.buckets),
  );

  if (!hasRate && !histogram) return null;

  return (
    <div className="space-y-2.5" data-testid="metrics-charts">
      {hasRate
        ? (
          <div
            className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
            data-testid="metrics-rate-chart"
          >
            <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tx("metrics.charts.rate", "Calls per minute")}
            </p>
            <svg
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
              preserveAspectRatio="none"
              className="h-[72px] w-full"
              role="img"
              aria-label={tx("metrics.charts.rateAria", "Calls per minute over the last three minutes")}
            >
              {/* One recessive baseline. A grid would out-weigh two thin lines at this height. */}
              <line
                x1="0" y1={HEIGHT - 1} x2={WIDTH} y2={HEIGHT - 1}
                stroke="currentColor" strokeWidth="1" className="text-border"
              />
              {rates.map((series, index) =>
                series.length < 2 ? null : (
                  <path
                    key={SERIES[index].key}
                    d={series
                      .map((value, i) => {
                        const x = (i / Math.max(1, series.length - 1)) * WIDTH;
                        const y = HEIGHT - 2 - (value / peak) * (HEIGHT - 8);
                        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
                      })
                      .join(" ")}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                    className={SERIES_CLASS[index]}
                  />
                )
              )}
            </svg>
            {/* Direct labels, mandatory: the light surface's contrast check WARNed on slot 2, and
                a WARN obligates a visible label rather than colour alone. */}
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              {SERIES.map(({ key, label, fallback }, index) => (
                <span
                  key={key}
                  className="flex items-center gap-1.5 text-[11.5px] text-muted-foreground"
                  data-testid={`metrics-rate-legend-${index}`}
                >
                  <span
                    aria-hidden
                    className={`h-2 w-2 rounded-full bg-current ${SERIES_CLASS[index]}`}
                  />
                  {tx(label, fallback)}
                  <span className="tabular-nums text-foreground">
                    {(rates[index].at(-1) ?? 0).toFixed(0)}
                  </span>
                </span>
              ))}
            </div>
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground/80">
              {tx(
                "metrics.charts.rateNote",
                "Derived from the difference between successive reads of a cumulative counter — the same way a scrape would. Kept in this tab, so a reload starts over.",
              )}
            </p>
          </div>
        )
        : null}

      {histogram
        ? (
          <div
            className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
            data-testid="metrics-latency-histogram"
          >
            <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tx("metrics.charts.latency", "Response latency")}
              <span className="ml-2 normal-case tracking-normal text-muted-foreground/80">
                {tx("metrics.charts.latencyCalls", "{{count}} calls", { count: histogram.count })}
              </span>
            </p>
            <ul className="space-y-1">
              {histogram.buckets.map((bucket, index) => {
                // Each bucket holds its own count, so the widest bar sets the scale. A cumulative
                // reading would make every bar wider than the last and say nothing.
                const widest = Math.max(1, ...histogram.buckets.map((b) => b.count));
                return (
                  <li
                    key={bucket.le ?? "inf"}
                    className="flex items-center gap-2 text-[11.5px]"
                    data-testid={`metrics-latency-bucket-${index}`}
                  >
                    <span className="w-14 shrink-0 tabular-nums text-right text-muted-foreground">
                      {bucket.le == null
                        ? tx("metrics.charts.over", "over")
                        : bucket.le >= 1000
                          ? `≤${bucket.le / 1000}s`
                          : `≤${bucket.le}ms`}
                    </span>
                    <span className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-primary/10">
                      <span
                        className="block h-full rounded-full bg-primary"
                        style={{ width: `${(bucket.count / widest) * 100}%` }}
                      />
                    </span>
                    {/* Every count printed. An empty bucket keeps its row: a latency band with no
                        calls is information. */}
                    <span className="w-10 shrink-0 tabular-nums text-foreground">
                      {bucket.count}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        )
        : null}
    </div>
  );
}
