/**
 * The shape of a number's recent past — nanoinfraorg/nanoinfra#274.
 *
 * The trend half of a stat tile: a sparkline is *shape*, not measurement, so it carries no axis,
 * no grid and no labels. The tile above it holds the number, and that division is the reason this
 * can be 40 px tall and still say something.
 *
 * Two points is the minimum: one sample is a dot, and a dot drawn as a line would claim a trend
 * from a single reading. Fewer than two renders nothing at all.
 *
 * A flat series is drawn flat rather than normalised to fill the box. A gauge that has read `0`
 * for three minutes should look like nothing happening — stretching that to a full-height wiggle
 * is the most common way a sparkline lies.
 */
import { useId } from "react";

import { cn } from "@/lib/utils";

const WIDTH = 120;
const HEIGHT = 28;
/** 2 px, per the mark spec. Thin enough to recede, thick enough to read at this size. */
const STROKE = 2;

export function Sparkline({
  values,
  className,
  ariaLabel,
}: {
  /** Oldest first. `null` marks a sample that could not be read and breaks the line there. */
  values: Array<number | null>;
  className?: string;
  ariaLabel?: string;
}) {
  const gradientId = useId();
  const readable = values.filter((value): value is number => value != null);
  if (readable.length < 2) return null;

  const high = Math.max(...readable);
  const low = Math.min(...readable);
  // A flat series sits on the baseline instead of being stretched to fill the box.
  const span = high - low;
  const y = (value: number) =>
    span === 0 ? HEIGHT - STROKE : HEIGHT - STROKE - ((value - low) / span) * (HEIGHT - STROKE * 2);
  const x = (index: number) => (index / Math.max(1, values.length - 1)) * WIDTH;

  // One path per unbroken run, so a gap in the samples is a gap in the line rather than a
  // straight segment across the missing time.
  const runs: string[] = [];
  let current: string[] = [];
  values.forEach((value, index) => {
    if (value == null) {
      if (current.length > 1) runs.push(current.join(" "));
      current = [];
      return;
    }
    current.push(`${current.length === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (current.length > 1) runs.push(current.join(" "));
  if (runs.length === 0) return null;

  const last = readable[readable.length - 1];
  const lastIndex = values.length - 1 - [...values].reverse().findIndex((value) => value != null);
  const area = `${runs[runs.length - 1]} L${x(lastIndex).toFixed(1)},${HEIGHT} L${x(0).toFixed(1)},${HEIGHT} Z`;

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      className={cn("h-7 w-full", className)}
      role={ariaLabel ? "img" : "presentation"}
      aria-label={ariaLabel}
      aria-hidden={ariaLabel ? undefined : true}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.18" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* The fill is decoration under a single series and stays faint enough not to read as a
          second mark. */}
      <path d={area} fill={`url(#${gradientId})`} stroke="none" />
      {runs.map((run) => (
        <path
          key={run}
          d={run}
          fill="none"
          stroke="currentColor"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {/* The current reading, marked. It is the only point on the line worth pointing at. */}
      <circle cx={x(lastIndex)} cy={y(last)} r={2.5} fill="currentColor" />
    </svg>
  );
}
