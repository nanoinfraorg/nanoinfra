/**
 * Approvals: whether the gate is working, rather than merely running — #274.
 *
 * The tab #235 asked for and the only one that answers a question about *people*. An approval
 * queue with a median of four hours is a queue nobody reads, and an approver who refuses nothing
 * is either working in a deployment that gates only safe things or rubber-stamping. Neither shows
 * up in the audit viewer's page of rows.
 *
 * Three numbers here are deliberately **not** what the audit log's decision names suggest, and
 * each was measured against a live log rather than inferred:
 *
 * - the log's `approve` is the **ask**, not the answer
 * - the answer is a later `allow` that names the path a person answered on
 * - `denied` holds a person's refusal *and* a policy refusal nobody was asked about, and this
 *   panel keeps them apart because merging them overstated the approver's denials 16-fold on the
 *   one deployment it was checked against
 *
 * A window range is deliberately absent from the top: this tab carries its own, because a
 * decision is not a token and sharing Usage's control would put two retentions under one range.
 */
import { useCallback, useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchMetricsApprovals } from "@/lib/api";
import type { MetricsApprovalsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const WINDOWS = [7, 30, 90, 365] as const;

/** Seconds read badly past a minute, and an approval queue is measured in minutes. */
function duration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3_600) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${Math.round(seconds % 60)}s`;
  }
  return `${(seconds / 3_600).toFixed(1)}h`;
}

export function MetricsApprovals({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const [window_, setWindow] = useState<number>(30);
  const [payload, setPayload] = useState<MetricsApprovalsPayload | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    try {
      setPayload(await fetchMetricsApprovals(token, window_, base));
      setUnavailable(false);
    } catch {
      // A gateway with no gate runtime answers 503. That is not a window in which nobody
      // approved anything, and rendering it as four zeros would be the worst possible lie on
      // this particular page.
      setUnavailable(true);
      setPayload(null);
    } finally {
      setLoading(false);
    }
  }, [token, window_, base]);

  useEffect(() => {
    void load();
  }, [load]);

  if (unavailable) {
    return (
      <div
        className="rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5"
        data-testid="metrics-approvals-unavailable"
      >
        <p className="text-[12.5px] leading-5 text-muted-foreground">
          {tx(
            "metrics.approvals.unavailable",
            "This gateway cannot reach the gate audit log. The gate may still be enforcing every decision, so read this as a missing view and not as a window in which nobody approved anything.",
          )}
        </p>
      </div>
    );
  }

  const tiles: Array<{ key: string; label: string; value: string; raised?: boolean }> = [
    {
      key: "asked",
      label: tx("metrics.approvals.asked", "Held for a person"),
      value: payload ? String(payload.asked) : "—",
    },
    {
      key: "answered",
      label: tx("metrics.approvals.answered", "Answered"),
      value: payload ? String(payload.answered) : "—",
    },
    {
      key: "refused",
      label: tx("metrics.approvals.refused", "Refused by a person"),
      value: payload ? String(payload.refused) : "—",
    },
    {
      key: "expired",
      label: tx("metrics.approvals.expired", "Expired"),
      value: payload ? String(payload.expired) : "—",
      raised: (payload?.expired ?? 0) > 0,
    },
    {
      key: "median",
      label: tx("metrics.approvals.median", "Median answer"),
      value: duration(payload?.median_seconds_to_answer ?? null),
    },
  ];

  return (
    <section className="space-y-3" data-testid="metrics-approvals">
      <div className="flex flex-wrap items-center gap-2">
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
            data-testid={`metrics-approvals-window-${days}`}
          >
            {tx("metrics.window.days", "{{count}}d", { count: days })}
          </button>
        ))}
        {loading
          ? <span className="text-[11px] text-muted-foreground">{tx("metrics.window.loading", "Loading…")}</span>
          : null}
      </div>

      <div className="grid gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
        {tiles.map((tile) => (
          <div
            key={tile.key}
            className={cn(
              "rounded-[16px] px-4 py-3",
              tile.raised ? "bg-amber-500/10" : "bg-settings-surface",
            )}
            data-testid={`metrics-approvals-${tile.key}`}
          >
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {tile.label}
            </p>
            <p
              className={cn(
                "mt-0.5 text-[22px] font-semibold leading-7 tabular-nums",
                tile.raised ? "text-amber-500" : "text-foreground",
              )}
            >
              {tile.value}
            </p>
          </div>
        ))}
      </div>

      {payload
        ? (
          <div className="space-y-1.5 rounded-[18px] bg-settings-surface px-4 py-3.5 sm:px-5">
            {payload.expired > 0
              ? (
                <p
                  className="flex items-start gap-2 text-[12px] leading-5 text-amber-600 dark:text-amber-500"
                  data-testid="metrics-approvals-expired-note"
                >
                  <TriangleAlert className="mt-[2px] h-3.5 w-3.5 shrink-0" aria-hidden />
                  {tx(
                    "metrics.approvals.expiredNote",
                    "{{count}} expired. An expired approval is an action that never ran and a person who never saw the ask.",
                    { count: payload.expired },
                  )}
                </p>
              )
              : null}

            {payload.unanswered > 0
              ? (
                <p
                  className="text-[12px] leading-5 text-amber-600 dark:text-amber-500"
                  data-testid="metrics-approvals-unanswered-note"
                >
                  {tx(
                    "metrics.approvals.unansweredNote",
                    "{{count}} were held and neither answered nor expired — an approval that fell through.",
                    { count: payload.unanswered },
                  )}
                </p>
              )
              : null}

            <p className="text-[12px] leading-5 text-muted-foreground">
              {payload.refusal_share == null
                ? tx(
                  "metrics.approvals.noAnswers",
                  "Nobody answered an approval in this window, so there is no refusal rate to read.",
                )
                : tx(
                  "metrics.approvals.refusalShare",
                  "{{percent}}% of answers were refusals. An approver who refuses nothing is either gating only safe things or not reading the ask.",
                  { percent: (payload.refusal_share * 100).toFixed(0) },
                )}
            </p>

            {payload.policy_refusals > 0
              ? (
                <p
                  className="text-[12px] leading-5 text-muted-foreground"
                  data-testid="metrics-approvals-policy-note"
                >
                  {tx(
                    "metrics.approvals.policyRefusals",
                    "{{count}} more actions were refused by policy without anybody being asked. They are not counted above, because nobody saw them.",
                    { count: payload.policy_refusals },
                  )}
                </p>
              )
              : null}

            {payload.same_path_answers > 0
              ? (
                <p
                  className="flex items-start gap-2 text-[12px] leading-5 text-amber-600 dark:text-amber-500"
                  data-testid="metrics-approvals-same-path-note"
                >
                  <TriangleAlert className="mt-[2px] h-3.5 w-3.5 shrink-0" aria-hidden />
                  {tx(
                    "metrics.approvals.samePath",
                    "{{count}} answers arrived on the same channel that asked. One compromised account then holds both halves.",
                    { count: payload.same_path_answers },
                  )}
                </p>
              )
              : null}

            <p className="text-[11px] leading-4 text-muted-foreground/80">
              {tx(
                "metrics.approvals.anchored",
                "Counted by when the gate held the action, not when it was answered — so an approval raised inside this window and answered after it still counts here. Fastest {{fastest}}, slowest {{slowest}}.",
                {
                  fastest: duration(payload.fastest_seconds),
                  slowest: duration(payload.slowest_seconds),
                },
              )}
            </p>
          </div>
        )
        : null}

      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          className="h-7 rounded-full px-3 text-[11.5px]"
          onClick={() => void load()}
          data-testid="metrics-approvals-refresh"
        >
          {tx("metrics.refresh", "Refresh")}
        </Button>
      </div>
    </section>
  );
}
