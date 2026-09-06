/**
 * A ratio against a limit — nanoinfraorg/nanoinfra#274.
 *
 * `context_tokens_used` and `context_tokens_limit` were never two facts; they were one ratio
 * rendered as two tiles, leaving the reader to divide 428,000 by 1,048,576. This does the
 * arithmetic.
 *
 * The form is a meter and not a two-slice pie, per the form heuristic. The fill carries severity
 * — accent, then warning, then critical — and the **percentage is always a visible label**,
 * because a status colour never carries meaning alone. The unfilled track is a lighter step of the
 * same ramp so the state reads across the whole bar rather than only where it stops.
 *
 * An absent or zero limit renders as unknown. A ratio with no denominator is not 0%, and drawing
 * it as an empty bar would claim a measurement nobody took.
 */
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** Where the fill changes what it is saying. Both are labelled, never colour-only. */
const WARNING_AT = 0.75;
const CRITICAL_AT = 0.9;

export function Meter({
  label,
  used,
  limit,
  format,
  testId,
}: {
  label: string;
  used: number | null;
  limit: number | null;
  format: (value: number) => string;
  testId?: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const unknown = used == null || limit == null || limit <= 0;
  const share = unknown ? 0 : Math.min(1, used / limit);
  const severity = share >= CRITICAL_AT ? "critical" : share >= WARNING_AT ? "warning" : "normal";

  return (
    <div
      className="rounded-[16px] bg-settings-surface px-4 py-3"
      data-testid={testId}
      data-severity={unknown ? "unknown" : severity}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </p>
        <p className="text-[11.5px] text-muted-foreground">
          {unknown
            ? tx("metrics.meter.unknown", "No limit reported")
            : `${(share * 100).toFixed(0)}%`}
        </p>
      </div>
      <p className="mt-0.5 text-[18px] font-semibold leading-6 text-foreground">
        {unknown || used == null || limit == null
          ? "—"
          : tx("metrics.meter.ofLimit", "{{used}} of {{limit}}")
            .replace("{{used}}", format(used))
            .replace("{{limit}}", format(limit))}
      </p>
      <div
        className="mt-2 h-2 w-full overflow-hidden rounded-full bg-primary/15"
        role="meter"
        aria-valuenow={unknown ? undefined : Math.round(share * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-500 ease-out motion-reduce:transition-none",
            severity === "critical"
              ? "bg-red-500"
              : severity === "warning"
                ? "bg-amber-500"
                : "bg-primary",
          )}
          style={{ width: unknown ? "0%" : `${Math.max(share * 100, share > 0 ? 2 : 0)}%` }}
        />
      </div>
    </div>
  );
}
