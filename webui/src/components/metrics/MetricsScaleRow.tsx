/**
 * How big this deployment is, in five numbers — nanoinfraorg/nanoinfra#274.
 *
 * Every one of these existed before and was scattered across five settings pages, which is what
 * #235 said when it asked for the row. One request answers all five: five parallel reads to
 * render five integers is five chances at a partial row, and a row showing four numbers and a
 * spinner answers nothing.
 *
 * It sits above the window control because it is the only thing on the tab a range does not
 * scope. Putting it under one would imply it did.
 *
 * A count of `null` renders as `—` and names itself as unreadable. "This could not be read" and
 * "there are none of these" are different facts about a deployment, and the same rule the gauges
 * follow applies here for the same reason.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchMetricsScale } from "@/lib/api";
import type { MetricsScalePayload } from "@/lib/types";

/** The five, in the order a reader scans them: what runs work, then what shapes it. */
const COUNTS = [
  { key: "servers", label: "settings.rows.servers", fallback: "servers" },
  { key: "skills", label: "sidebar.skills.title", fallback: "skills" },
  { key: "agents", label: "sidebar.agents", fallback: "agents" },
  { key: "mcp_servers", label: "metrics.scale.mcp", fallback: "MCP" },
  { key: "connectors", label: "metrics.scale.connectors", fallback: "connectors" },
] as const;

export function MetricsScaleRow({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [scale, setScale] = useState<MetricsScalePayload | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchMetricsScale(token, base);
        if (!cancelled) setScale(payload);
      } catch {
        // The row is a signpost, not a measurement. A failed read leaves it absent rather than
        // putting an error band above every other number on the tab.
        if (!cancelled) setScale(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, base]);

  if (!scale) return null;

  // The payload is validated rather than trusted, and the reason is not defensiveness for its own
  // sake: this row renders *inside* the Usage tab, so a shape it did not expect used to throw and
  // take every number on that tab down with it. An older gateway answering this route without
  // `unavailable` is exactly that shape. A row that renders nothing is a bad row; a row that
  // unmounts the page around it is a bug.
  const unavailable = Array.isArray(scale.unavailable) ? scale.unavailable : [];
  const readable = COUNTS.filter(({ key }) => typeof scale[key] === "number" || scale[key] === null);
  if (readable.length === 0) return null;

  return (
    <div
      className="flex flex-wrap items-baseline gap-x-5 gap-y-1.5 rounded-[18px] bg-settings-surface px-4 py-3 sm:px-5"
      data-testid="metrics-scale"
    >
      {readable.map(({ key, label, fallback }) => {
        const value = scale[key];
        return (
          <span
            key={key}
            className="flex items-baseline gap-1.5 text-[12.5px]"
            data-testid={`metrics-scale-${key}`}
          >
            <span className="font-semibold tabular-nums text-foreground">
              {value == null ? "—" : new Intl.NumberFormat().format(value)}
            </span>
            <span className="text-muted-foreground">{tx(label, fallback)}</span>
          </span>
        );
      })}
      {unavailable.length > 0
        ? (
          <span
            className="text-[11.5px] text-amber-500/90"
            data-testid="metrics-scale-unavailable"
          >
            {tx(
              "metrics.scale.unavailable",
              "A dash is a count that could not be read, not a count of zero.",
            )}
          </span>
        )
        : null}
    </div>
  );
}
