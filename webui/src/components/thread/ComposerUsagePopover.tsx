/**
 * How much of the context window this thread is using, and what the last few rounds cost.
 *
 * Two facts on one surface, because they answer the same question at two zoom levels: the ring
 * says *how close to full* the window is right now, and the bars say *why* -- a thread that grew
 * steadily looks nothing like one where a single tool result doubled the prompt.
 *
 * Each bar is one provider call's input, split into the three buckets we bill separately. That
 * split is the whole point of drawing them: `prompt_tokens` **includes** the cached tokens (rule 1
 * of the `LLMUsage` contract), so a bar showing only its total hides the difference between a
 * 200K-token prompt that cost full rate and a 200K-token prompt that was 95% cache reads at a
 * tenth of it. See `nanoinfra/llm_usage/pricing.py:uncached_input_tokens` for the same subtraction
 * done server-side, and for why the buckets must be disjoint before they meet three rates.
 */

import { Fragment, useMemo } from "react";
import { useTranslation } from "react-i18next";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatCompactTokens, formatTurnLatency } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Where the thread stands against the window the answering model was given. */
export interface ComposerContextUsage {
  contextTokens: number;
  /** Absent when the gateway reported none -- and then there is no fraction to draw. */
  contextWindowTokens?: number;
}

/** One provider call's cost, as `TurnUsage` on the row that call produced (#208). */
export interface ComposerRoundUsage {
  id: string;
  timestamp: number;
  /** The logical input, cache reads and writes **included**. */
  inputTokens: number;
  outputTokens?: number;
  /** Cache reads. Absent means the provider reported no cache metric, not a cold cache. */
  cachedTokens?: number;
  /** Cache writes, billed above the input rate rather than below it. */
  cacheWriteTokens?: number;
  estimatedTokens?: number;
  generationMs?: number;
}

/** How many rounds the chart holds. Eight fits the panel without the bars becoming hairlines. */
const MAX_ROUNDS = 8;
/** Tallest bar, in px, inside the `h-28` (112px) plot. */
const PLOT_HEIGHT_PX = 108;

interface NormalizedRound extends ComposerRoundUsage {
  /** The three disjoint buckets, summing to exactly `inputTokens`. */
  freshTokens: number;
  cacheReadTokens: number;
  cacheWrittenTokens: number;
  /** True when the provider reported a cache metric at all. */
  cacheKnown: boolean;
}

/**
 * Make the buckets disjoint and make them sum to the total.
 *
 * A provider that reports reads plus writes above its own `prompt_tokens` is stating something
 * impossible; server-side that clamps the *fresh* bucket at zero, and here it would additionally
 * make the segments overflow the bar. So each bucket is clamped to what the previous ones left,
 * which keeps the drawing honest about the total even when the report is not.
 */
function normalizeRounds(rounds: readonly ComposerRoundUsage[]): NormalizedRound[] {
  return rounds
    .filter((round) => Number.isFinite(round.inputTokens) && round.inputTokens > 0)
    .map((round) => {
      const inputTokens = Math.max(0, Math.round(round.inputTokens));
      const cacheKnown =
        Number.isFinite(round.cachedTokens) || Number.isFinite(round.cacheWriteTokens);
      const cacheReadTokens = Math.min(
        inputTokens,
        Math.max(0, Math.round(round.cachedTokens ?? 0)),
      );
      const cacheWrittenTokens = Math.min(
        inputTokens - cacheReadTokens,
        Math.max(0, Math.round(round.cacheWriteTokens ?? 0)),
      );
      return {
        ...round,
        inputTokens,
        outputTokens: Number.isFinite(round.outputTokens)
          ? Math.max(0, Math.round(round.outputTokens ?? 0))
          : 0,
        cacheKnown,
        cacheReadTokens,
        cacheWrittenTokens,
        freshTokens: inputTokens - cacheReadTokens - cacheWrittenTokens,
      };
    });
}

export function ComposerUsagePopover({
  context,
  rounds,
  compact = false,
}: {
  context: ComposerContextUsage | null;
  rounds: readonly ComposerRoundUsage[];
  /** The hero composer runs one step smaller, like every other control in that row. */
  compact?: boolean;
}) {
  const { t, i18n } = useTranslation("common");
  const normalizedRounds = useMemo(
    () => normalizeRounds(rounds).slice(-MAX_ROUNDS),
    [rounds],
  );

  const hasContext =
    !!context
    && Number.isFinite(context.contextTokens)
    && context.contextTokens >= 0
    && Number.isFinite(context.contextWindowTokens)
    && (context.contextWindowTokens ?? 0) > 0;

  // Nothing measured yet is not a surface worth opening. A fresh thread shows no control at all
  // rather than an empty panel behind an enabled button.
  if (!hasContext && normalizedRounds.length === 0) return null;

  const contextPercentage = hasContext
    ? Math.min(100, Math.round((context!.contextTokens / context!.contextWindowTokens!) * 100))
    : null;
  const meterPercentage = contextPercentage ?? 0;
  // Thresholds, not a ramp: the only decision this number drives is whether to compact, and that
  // decision has steps. 75% is "soon", 90% is "now".
  const status = meterPercentage >= 90 ? "critical" : meterPercentage >= 75 ? "caution" : "normal";

  const detailsLabel = t("thread.composer.context.detailsLabel");
  const contextDescription =
    contextPercentage === null
      ? detailsLabel
      : t("thread.composer.context.tooltip", { percent: contextPercentage });
  const triggerLabel =
    contextPercentage === null ? detailsLabel : `${contextDescription}. ${detailsLabel}`;

  const ringCircumference = 2 * Math.PI * 6;
  const ringLength = (ringCircumference * meterPercentage) / 100;
  const maxInputTokens = Math.max(1, ...normalizedRounds.map((round) => round.inputTokens));

  const numberFormatter = new Intl.NumberFormat(i18n.language);
  const percentageFormatter = new Intl.NumberFormat(i18n.language, {
    style: "percent",
    maximumFractionDigits: 0,
  });
  const roundDateFormatter = new Intl.DateTimeFormat(i18n.language, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <DropdownMenu>
      <TooltipProvider delayDuration={300} skipDelayDuration={80}>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                data-testid="composer-context-usage"
                aria-label={triggerLabel}
                className={cn(
                  "thread-composer-action touch-target inline-flex shrink-0 items-center justify-center rounded-full",
                  "text-muted-foreground/80 transition-colors hover:text-foreground/85",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  compact ? "h-8 w-8" : "h-9 w-9",
                )}
              >
                {contextPercentage === null ? (
                  // No window reported, so no fraction to draw. A small histogram glyph says the
                  // panel still has the rounds, rather than implying a reading of zero.
                  <svg viewBox="0 0 16 16" aria-hidden="true" className="h-[15px] w-[15px]">
                    <path
                      d="M3 12V9m5 3V5m5 7V2"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                ) : (
                  <svg
                    viewBox="0 0 16 16"
                    aria-hidden="true"
                    className={cn(
                      "h-[15px] w-[15px] shrink-0 -rotate-90",
                      status === "critical" && "text-destructive-text",
                      status === "caution" && "text-amber-600 dark:text-amber-400",
                      status === "normal" && "text-muted-foreground/80",
                    )}
                  >
                    <circle
                      cx="8"
                      cy="8"
                      r="6"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      className="opacity-20"
                    />
                    <circle
                      cx="8"
                      cy="8"
                      r="6"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeDasharray={`${ringLength} ${ringCircumference}`}
                      data-testid="composer-context-meter"
                    />
                  </svg>
                )}
              </button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent
            side="top"
            align="center"
            sideOffset={8}
            className="w-fit max-w-[calc(100vw-2rem)] rounded-full border border-border/70 px-3 py-1.5 text-[13px] font-medium shadow-[0_8px_24px_rgba(15,23,42,0.13)] dark:border-white/10"
          >
            <span className="whitespace-nowrap tabular-nums">{contextDescription}</span>
          </TooltipContent>
        </Tooltip>

        <DropdownMenuContent
          side="top"
          align="end"
          sideOffset={10}
          aria-label={t("thread.composer.context.panelTitle")}
          className={cn(
            "w-[min(22rem,calc(100vw-1.5rem))] min-w-0 max-h-none overflow-visible p-0",
            "rounded-[14px] border-border/70 bg-popover backdrop-blur-none",
            "shadow-[0_10px_34px_rgba(15,23,42,0.14)] dark:shadow-[0_12px_34px_rgba(0,0,0,0.5)]",
          )}
        >
          <div className="px-4 pb-4 pt-3.5">
            {contextPercentage !== null ? (
              <>
                <div className="flex items-baseline justify-between gap-3">
                  <div className="flex min-w-0 items-baseline gap-2">
                    <span className="shrink-0 text-[12.5px] font-medium text-foreground">
                      {t("thread.composer.context.contextTitle")}
                    </span>
                    <span className="truncate text-[11.5px] tabular-nums text-muted-foreground">
                      {formatCompactTokens(context!.contextTokens)}
                      {" / "}
                      {formatCompactTokens(context!.contextWindowTokens!)}
                    </span>
                  </div>
                  <span className="shrink-0 text-[11.5px] tabular-nums text-muted-foreground">
                    {contextPercentage}%
                  </span>
                </div>
                <div
                  role="progressbar"
                  aria-label={contextDescription}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={contextPercentage}
                  className="mt-2 h-1 overflow-hidden rounded-full bg-foreground/10 dark:bg-white/[0.14]"
                >
                  <div
                    className={cn(
                      "h-full rounded-full",
                      status === "critical" && "bg-destructive",
                      status === "caution" && "bg-amber-500",
                      status === "normal" && "bg-foreground/45",
                    )}
                    style={{ width: `${contextPercentage}%` }}
                  />
                </div>
              </>
            ) : null}

            {normalizedRounds.length > 0 ? (
              <>
                <div
                  className={cn(
                    "flex items-baseline justify-between gap-3",
                    contextPercentage === null ? "mt-0" : "mt-5",
                  )}
                >
                  <span className="text-[12.5px] font-medium text-foreground">
                    {t("thread.composer.context.recentRounds")}
                  </span>
                  <span className="text-[11.5px] text-muted-foreground">
                    {t("thread.composer.context.inputTrend")}
                  </span>
                </div>
                <div
                  role="group"
                  aria-label={t("thread.composer.context.inputTrend")}
                  className="mt-2 flex h-28 items-end gap-1.5 border-b border-border/60"
                >
                  {normalizedRounds.map((round, index) => {
                    // Relative, not absolute: the tallest round is always full height, so the
                    // chart reads as a shape. An absolute scale would flatten every thread that
                    // never approaches its window into an unreadable strip.
                    const barHeight = (round.inputTokens / maxInputTokens) * PLOT_HEIGHT_PX;
                    const timestampLabel = roundDateFormatter.format(round.timestamp);
                    const segments = [
                      {
                        key: "written",
                        tokens: round.cacheWrittenTokens,
                        className: "kv-cache-written",
                      },
                      { key: "fresh", tokens: round.freshTokens, className: "kv-cache-not-reused" },
                      { key: "read", tokens: round.cacheReadTokens, className: "kv-cache-reused" },
                    ].filter((segment) => segment.tokens > 0);
                    const detailRows = [
                      {
                        key: "input",
                        label: t("thread.composer.context.input"),
                        value: numberFormatter.format(round.inputTokens),
                      },
                      round.cacheWrittenTokens > 0
                        ? {
                            key: "cache-written",
                            label: t("thread.composer.context.cacheWritten"),
                            value: numberFormatter.format(round.cacheWrittenTokens),
                          }
                        : null,
                      round.cacheKnown
                        ? {
                            key: "cache-hit-rate",
                            label: t("thread.composer.context.cacheHitRate"),
                            value: percentageFormatter.format(
                              round.cacheReadTokens / round.inputTokens,
                            ),
                          }
                        : null,
                      {
                        key: "output",
                        label: t("thread.composer.context.output"),
                        value: numberFormatter.format(round.outputTokens ?? 0),
                      },
                      typeof round.generationMs === "number"
                        ? {
                            key: "duration",
                            label: t("thread.composer.context.duration"),
                            value: formatTurnLatency(round.generationMs, i18n.language),
                          }
                        : null,
                    ].filter((row): row is NonNullable<typeof row> => !!row);
                    const detailNote =
                      (round.estimatedTokens ?? 0) > 0
                        ? t("thread.composer.context.estimated")
                        : null;
                    // The same content the tooltip shows, flattened -- so the chart is readable
                    // without sight and without a hover a keyboard cannot produce.
                    const detailLabel = [
                      timestampLabel,
                      ...detailRows.map((row) => `${row.label} ${row.value}`),
                      detailNote,
                    ]
                      .filter((part): part is string => !!part)
                      .join(". ");

                    return (
                      <span
                        key={round.id}
                        className={cn(
                          "flex h-full min-w-0 flex-1 items-end justify-center rounded-sm",
                          "opacity-70 transition-opacity hover:opacity-100",
                          index === normalizedRounds.length - 1 && "opacity-100",
                        )}
                      >
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span
                              role="img"
                              tabIndex={0}
                              aria-label={detailLabel}
                              data-testid="round-usage-bar"
                              className={cn(
                                "flex w-full max-w-7 flex-col overflow-hidden rounded-t-[3px]",
                                "bg-foreground/[0.07] dark:bg-white/[0.09]",
                                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                              )}
                              style={{ height: `${barHeight}px` }}
                            >
                              {segments.map((segment) => (
                                <span
                                  key={segment.key}
                                  data-segment={segment.key}
                                  className={cn("block w-full", segment.className)}
                                  style={{
                                    height: `${(segment.tokens / round.inputTokens) * 100}%`,
                                  }}
                                />
                              ))}
                            </span>
                          </TooltipTrigger>
                          <TooltipContent
                            side="top"
                            align="center"
                            className="max-w-72 px-3 py-2 text-[11.5px]"
                          >
                            <span className="block font-medium text-foreground">
                              {timestampLabel}
                            </span>
                            <span className="mt-1 grid grid-cols-[max-content_max-content] gap-x-3 gap-y-0.5">
                              {detailRows.map((row) => (
                                <Fragment key={row.key}>
                                  <span className="text-muted-foreground">{row.label}</span>
                                  <span className="text-end tabular-nums text-foreground">
                                    {row.value}
                                  </span>
                                </Fragment>
                              ))}
                            </span>
                            {detailNote ? (
                              <span className="mt-1 block text-muted-foreground">{detailNote}</span>
                            ) : null}
                          </TooltipContent>
                        </Tooltip>
                      </span>
                    );
                  })}
                </div>
                {/* A legend, always: three textures are three things, and a reader who never
                    hovers a bar would otherwise have to guess which is which. */}
                <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                  <LegendSwatch className="kv-cache-not-reused" label={t("thread.composer.context.legendFresh")} />
                  <LegendSwatch className="kv-cache-written" label={t("thread.composer.context.legendWritten")} />
                  <LegendSwatch className="kv-cache-reused" label={t("thread.composer.context.legendReused")} />
                </div>
              </>
            ) : null}
          </div>
        </DropdownMenuContent>
      </TooltipProvider>
    </DropdownMenu>
  );
}

function LegendSwatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        aria-hidden="true"
        className={cn("inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]", className)}
      />
      {label}
    </span>
  );
}
