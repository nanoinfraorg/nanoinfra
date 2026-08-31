import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

type TokenUsagePayload = NonNullable<SettingsPayload["usage"]>;

/**
 * The numbers, as text.
 *
 * The heatmap shows a year of shape and not one figure, so a deployment with
 * eighteen days of history rendered as a grid of grey dots that answered
 * nothing. This is what the store actually knows, written out: what the last
 * thirty days cost, how much of that the provider measured rather than us
 * guessing, how many calls failed, and which model is the expensive one.
 *
 * Two of these had no surface at all before. `failed_requests_30d` and
 * `providers_30d` were in the payload and read by nothing, which is the half of
 * the call store that got built and never plugged in: one row per *attempt* is
 * what makes "how many calls failed and retried" answerable, and it was
 * answerable only by curl.
 */
export function TokenUsageSummary({
  usage,
  className,
}: {
  usage?: TokenUsagePayload;
  className?: string;
}) {
  const { t, i18n } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const numbers = useMemo(() => new Intl.NumberFormat(i18n.language), [i18n.language]);
  const rows = useMemo(() => (usage?.providers_30d ?? []).slice(0, 6), [usage?.providers_30d]);

  if (!usage) return null;

  const total30 = usage.total_tokens_30d ?? 0;
  const requests30 = usage.requests_30d ?? 0;
  const failed30 = usage.failed_requests_30d ?? 0;
  // Summed from the day rows rather than read from a total: the payload carries
  // the partition per day and per source, and a second top-level field for it
  // would be one more thing that can disagree with them.
  const reported30 = (usage.days ?? []).reduce(
    (sum, day) => sum + (day.provider_tokens ?? 0),
    0,
  );
  const estimated30 = (usage.days ?? []).reduce(
    (sum, day) => sum + (day.estimated_tokens ?? 0),
    0,
  );
  const measuredShare = reported30 + estimated30 > 0
    ? Math.round((reported30 / (reported30 + estimated30)) * 100)
    : null;
  const today = (usage.days ?? []).at(-1);

  return (
    <dl
      className={cn(
        "grid gap-x-6 gap-y-2 text-[12.5px] leading-5 sm:grid-cols-2",
        className,
      )}
    >
      <Line
        label={tx("settings.usage.last30", "Last 30 days")}
        value={`${numbers.format(total30)} ${tx("settings.usage.tokens", "tokens")} · ${numbers.format(requests30)} ${tx("settings.usage.calls", "calls")}`}
        detail={
          failed30 > 0
            ? tx("settings.usage.failedCalls", "{{count}} failed ({{percent}}%)", {
                count: numbers.format(failed30),
                percent: requests30 > 0 ? Math.round((failed30 / requests30) * 100) : 0,
              })
            : tx("settings.usage.noFailures", "none failed")
        }
      />
      {today ? (
        <Line
          label={tx("settings.usage.today", "Today")}
          value={`${numbers.format(today.total_tokens)} ${tx("settings.usage.tokens", "tokens")} · ${numbers.format(today.requests)} ${tx("settings.usage.calls", "calls")}`}
        />
      ) : null}
      {measuredShare !== null ? (
        <Line
          label={tx("settings.usage.measured", "Measured")}
          value={tx("settings.usage.measuredValue", "{{percent}}% reported by the provider", {
            percent: measuredShare,
          })}
          // The rest is our own tokenizer, and saying so is the point: a cost
          // figure whose origin is unstated invites arithmetic it cannot carry.
          detail={
            measuredShare < 100
              ? tx("settings.usage.estimatedRest", "{{tokens}} estimated locally", {
                  tokens: numbers.format(estimated30),
                })
              : undefined
          }
        />
      ) : null}
      <Line
        label={tx("settings.usage.peak", "Peak day")}
        value={`${numbers.format(usage.peak_day_tokens ?? 0)} ${tx("settings.usage.tokens", "tokens")}`}
        detail={tx("settings.usage.streak", "{{days}} day streak, longest {{longest}}", {
          days: usage.current_streak_days ?? 0,
          longest: usage.longest_streak_days ?? 0,
        })}
      />
      {rows.length ? (
        <div className="sm:col-span-2">
          <dt className="mb-1 text-[11.5px] font-semibold uppercase tracking-wide text-muted-foreground/80">
            {tx("settings.usage.byModel", "By model, last 30 days")}
          </dt>
          <dd className="space-y-1">
            {rows.map((row) => (
              <div
                key={`${row.provider}/${row.model}`}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 tabular-nums"
              >
                <code className="text-foreground">{row.model}</code>
                <span className="text-muted-foreground/80">{row.provider}</span>
                <span className="text-foreground">
                  {numbers.format(row.total_tokens)} {tx("settings.usage.tokens", "tokens")}
                </span>
                <span className="text-muted-foreground">
                  {numbers.format(row.requests)} {tx("settings.usage.calls", "calls")}
                </span>
                {row.failed_requests > 0 ? (
                  <span className="text-amber-600 dark:text-amber-300">
                    {tx("settings.usage.failedShort", "{{count}} failed", {
                      count: numbers.format(row.failed_requests),
                    })}
                  </span>
                ) : null}
                {/* Time to first token, averaged over the calls that were timed
                    rather than over all of them -- a call nobody timed would
                    otherwise drag the figure toward zero. */}
                {row.timed_requests > 0 ? (
                  <span className="text-muted-foreground/80">
                    {(row.ttft_ms / row.timed_requests / 1000).toFixed(1)}s{" "}
                    {tx("settings.usage.ttft", "to first token")}
                  </span>
                ) : null}
              </div>
            ))}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

function Line({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums text-foreground">{value}</dd>
      {detail ? <span className="text-[11.5px] text-muted-foreground/80">{detail}</span> : null}
    </div>
  );
}
