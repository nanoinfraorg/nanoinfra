/**
 * Calls: `tool_calls`, read for the first time (#232, phase 3).
 *
 * The table has been written since 2.0.0 — fourteen columns, one row per tool call, a pruner and
 * a purge log — and it has never had a reader. This is it.
 *
 * **The arguments are not here, and that is the design.** A row carries the *address* of a call —
 * `session_key`, `turn_id`, `seq` — and the transcript carries what was said. A table that
 * duplicated the arguments would be a second transcript with a different retention and its own
 * leak surface, so the detail view links to the conversation instead of reprinting it.
 *
 * Paging is a keyset cursor (`next_before_id`), not an offset: calls arrive while somebody is
 * reading, and an offset silently skips or repeats rows.
 *
 * The retention line at the bottom exists because an empty page and a purged page look identical
 * otherwise. `last_purge` is written by the pruner and, until this view, was read by nobody.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchMetricsCalls } from "@/lib/api";
import type { MetricsCallsPayload, ToolCallRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 100;

interface Filters {
  tool: string;
  outcome: string;
  decision: string;
  /** Set by clicking a row's turn, not typed: it is an opaque id nobody remembers. */
  session: string;
  turn: string;
}

const EMPTY_FILTERS: Filters = {
  tool: "",
  outcome: "",
  decision: "",
  session: "",
  turn: "",
};

function timeLabel(tsMs: number): string {
  const moment = new Date(tsMs);
  return Number.isNaN(moment.getTime()) ? "—" : moment.toLocaleString();
}

function dayLabel(tsMs: number): string {
  const moment = new Date(tsMs);
  return Number.isNaN(moment.getTime()) ? "—" : moment.toLocaleDateString();
}

/** Milliseconds read badly past a few seconds, and tool calls routinely run for minutes. */
function durationLabel(ms: number): string {
  if (ms < 1_000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

/** An outcome is one of three facts, and a denial is not a failure. */
function outcomeTone(outcome: string): string {
  if (outcome === "error") return "text-red-500";
  if (outcome === "denied") return "text-amber-500";
  return "text-muted-foreground";
}

export function MetricsCalls({
  token,
  base = "",
  onOpenSession,
  knownSessions,
}: {
  token: string;
  base?: string;
  /** Opens the transcript a row points at. Absent when the host cannot navigate. */
  onOpenSession?: (sessionKey: string) => void;
  /** The sessions the host can actually open. A row for a session that is gone gets no button. */
  knownSessions?: readonly string[];
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, vars?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(vars ?? {}) });

  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState<MetricsCallsPayload | null>(null);
  const [rows, setRows] = useState<ToolCallRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);

  const openable = useMemo(() => new Set(knownSessions ?? []), [knownSessions]);

  const load = useCallback(
    async (next: Filters, before: number | null) => {
      setLoading(true);
      try {
        const result = await fetchMetricsCalls(
          token,
          {
            limit: PAGE_SIZE,
            before,
            tool: next.tool,
            outcome: next.outcome,
            decision: next.decision,
            session: next.session,
            turn: next.turn,
          },
          base,
        );
        setFailed(false);
        setPage(result);
        // `before` distinguishes the two calls: a first page replaces, a cursor page appends.
        setRows((current) => (before == null ? result.calls : [...current, ...result.calls]));
      } catch {
        setFailed(true);
        if (before == null) {
          setPage(null);
          setRows([]);
        }
      } finally {
        setLoading(false);
      }
    },
    [token, base],
  );

  useEffect(() => {
    void load(filters, null);
  }, [filters, load]);

  const update = (field: keyof Filters, value: string) => {
    setOpenId(null);
    setFilters((current) => ({ ...current, [field]: value }));
  };

  const open = rows.find((row) => row.id === openId) ?? null;
  const turnFiltered = filters.session !== "" || filters.turn !== "";

  return (
    <section className="space-y-3" data-testid="metrics-calls">
      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect
          label={tx("metrics.calls.filter.tool", "Tool")}
          value={filters.tool}
          choices={page?.tools ?? []}
          anyLabel={tx("metrics.calls.filter.any", "any")}
          onChange={(value) => update("tool", value)}
        />
        <FilterSelect
          label={tx("metrics.calls.filter.outcome", "Outcome")}
          value={filters.outcome}
          choices={page?.outcomes ?? []}
          anyLabel={tx("metrics.calls.filter.any", "any")}
          onChange={(value) => update("outcome", value)}
        />
        <FilterSelect
          label={tx("metrics.calls.filter.decision", "Gate")}
          value={filters.decision}
          choices={page?.gate_decisions ?? []}
          anyLabel={tx("metrics.calls.filter.any", "any")}
          onChange={(value) => update("decision", value)}
        />
        {turnFiltered
          ? (
            <button
              type="button"
              onClick={() => setFilters((current) => ({ ...current, session: "", turn: "" }))}
              className="h-8 rounded-full bg-accent px-3 text-[12px] text-foreground transition-colors hover:bg-accent/80"
              data-testid="metrics-calls-clear-turn"
            >
              {tx("metrics.calls.filter.clearTurn", "Showing one turn — show all")}
            </button>
          )
          : null}
        {loading ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : null}
      </div>

      {failed
        ? (
          <p
            className="rounded-[12px] bg-amber-500/10 px-3 py-2.5 text-[12.5px] text-foreground"
            data-testid="metrics-calls-error"
          >
            {tx(
              "metrics.calls.unreachable",
              "Could not read the tool call log. The calls are still recorded, so treat this as a missing view and not as an empty log.",
            )}
          </p>
        )
        : null}

      {page && rows.length === 0 && !failed
        ? (
          <p
            className="rounded-[12px] border border-dashed border-border px-3 py-2.5 text-[12.5px] text-muted-foreground"
            data-testid="metrics-calls-empty"
          >
            {tx(
              "metrics.calls.empty",
              "No call matches these filters. Rows older than the retention window are purged, so an empty page is not always an idle one.",
            )}
          </p>
        )
        : null}

      {rows.length > 0
        ? (
          <div className="overflow-hidden rounded-[14px] border border-border">
            <table className="w-full text-left text-[12.5px]">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="w-6 px-2 py-2" />
                  <th className="px-3 py-2 font-medium">
                    {tx("metrics.calls.column.time", "Time")}
                  </th>
                  <th className="px-3 py-2 font-medium">
                    {tx("metrics.calls.column.tool", "Tool")}
                  </th>
                  <th className="px-3 py-2 font-medium">
                    {tx("metrics.calls.column.source", "Source")}
                  </th>
                  <th className="px-3 py-2 font-medium">
                    {tx("metrics.calls.column.outcome", "Outcome")}
                  </th>
                  <th className="px-3 py-2 font-medium">
                    {tx("metrics.calls.column.gate", "Gate")}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {tx("metrics.calls.column.duration", "Took")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    data-testid={`metrics-call-${row.id}`}
                    onClick={() => setOpenId(openId === row.id ? null : row.id)}
                    className={cn(
                      "cursor-pointer border-t border-border/60 hover:bg-muted/30",
                      openId === row.id && "bg-muted/40",
                    )}
                  >
                    <td className="px-2 py-2 text-muted-foreground">
                      {openId === row.id
                        ? <ChevronDown className="h-3.5 w-3.5" />
                        : <ChevronRight className="h-3.5 w-3.5" />}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-muted-foreground">
                      {timeLabel(row.ts_ms)}
                    </td>
                    <td className="px-3 py-2 font-medium text-foreground">{row.tool}</td>
                    <td className="px-3 py-2 text-muted-foreground">{row.source}</td>
                    <td className={cn("px-3 py-2", outcomeTone(row.outcome))}>
                      {row.outcome}
                      {row.error_kind ? ` · ${row.error_kind}` : ""}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{row.gate_decision ?? "—"}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {durationLabel(row.duration_ms)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
        : null}

      {open
        ? (
          <CallDetail
            row={open}
            canOpenSession={open.session_key != null && openable.has(open.session_key)}
            onOpenSession={onOpenSession}
            onFilterTurn={() => {
              setOpenId(null);
              setFilters((current) => ({
                ...current,
                session: open.session_key ?? "",
                turn: open.turn_id ?? "",
              }));
            }}
          />
        )
        : null}

      {page?.has_more && page.next_before_id != null
        ? (
          <Button
            type="button"
            variant="ghost"
            className="h-8 rounded-full px-3 text-[12px]"
            disabled={loading}
            onClick={() => void load(filters, page.next_before_id)}
            data-testid="metrics-calls-more"
          >
            {tx("metrics.calls.loadMore", "Load more")}
          </Button>
        )
        : null}

      {page
        ? (
          <p className="text-[11px] leading-4 text-muted-foreground/80" data-testid="metrics-calls-retention">
            {tx(
              "metrics.calls.retention",
              "Calls are kept for {{days}} days and then purged.",
              { days: page.retention_days },
            )}{" "}
            {page.last_purge
              ? tx(
                "metrics.calls.lastPurge",
                "The last purge ran on {{when}} and dropped {{rows}} rows older than {{cutoff}}.",
                {
                  when: timeLabel(page.last_purge.ts_ms),
                  rows: page.last_purge.rows_purged,
                  cutoff: dayLabel(page.last_purge.cutoff_ms),
                },
              )
              : tx("metrics.calls.noPurge", "Nothing has been purged yet.")}
          </p>
        )
        : null}
    </section>
  );
}

function FilterSelect({
  label,
  value,
  choices,
  anyLabel,
  onChange,
}: {
  label: string;
  value: string;
  choices: string[];
  anyLabel: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-[10px] border border-input bg-background px-2 text-[12.5px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value="">{anyLabel}</option>
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    </label>
  );
}

function CallDetail({
  row,
  canOpenSession,
  onOpenSession,
  onFilterTurn,
}: {
  row: ToolCallRow;
  canOpenSession: boolean;
  onOpenSession?: (sessionKey: string) => void;
  onFilterTurn: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const fields: [string, string][] = [
    [tx("metrics.calls.field.time", "Time"), timeLabel(row.ts_ms)],
    [tx("metrics.calls.field.tool", "Tool"), row.tool],
    [tx("metrics.calls.field.source", "Source"), row.source],
    [tx("metrics.calls.field.outcome", "Outcome"), row.outcome],
    [tx("metrics.calls.field.errorKind", "Error kind"), row.error_kind ?? "—"],
    [tx("metrics.calls.field.duration", "Took"), durationLabel(row.duration_ms)],
    [tx("metrics.calls.field.class", "Capability class"), row.capability_class ?? "—"],
    [tx("metrics.calls.field.gate", "Gate decision"), row.gate_decision ?? "—"],
    [tx("metrics.calls.field.gateReason", "Gate reason"), row.gate_reason ?? "—"],
    // Whose call it was. The gate's authenticated answerer when there was one, and otherwise the
    // identity the turn arrived under -- which is a channel's claim, not authentication.
    [tx("metrics.calls.field.actor", "Actor"), row.actor ?? "—"],
    [tx("metrics.calls.field.session", "Session"), row.session_key ?? "—"],
    [tx("metrics.calls.field.turn", "Turn"), row.turn_id ?? "—"],
    [tx("metrics.calls.field.seq", "Sequence"), row.seq == null ? "—" : String(row.seq)],
  ];

  return (
    <div
      data-testid="metrics-call-detail"
      className="space-y-2 rounded-[14px] border border-border bg-muted/20 p-3"
    >
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
        {fields.map(([label, value]) => (
          <div key={label} className="flex min-w-0 gap-2 text-[12.5px]">
            <dt className="w-[126px] shrink-0 text-muted-foreground">{label}</dt>
            <dd className="min-w-0 break-words text-foreground">{value}</dd>
          </div>
        ))}
      </dl>

      <p className="text-[11.5px] leading-4 text-muted-foreground">
        {tx(
          "metrics.calls.noArguments",
          "What the call was given is not recorded here. This row is the address of the call; the conversation holds what was said.",
        )}
      </p>

      <div className="flex flex-wrap gap-2">
        {row.turn_id
          ? (
            <Button
              type="button"
              variant="ghost"
              className="h-7 rounded-full px-3 text-[11.5px]"
              onClick={onFilterTurn}
              data-testid="metrics-call-filter-turn"
            >
              {tx("metrics.calls.sameTurn", "Every call in this turn")}
            </Button>
          )
          : null}
        {canOpenSession && onOpenSession && row.session_key
          ? (
            <Button
              type="button"
              variant="ghost"
              className="h-7 rounded-full px-3 text-[11.5px]"
              onClick={() => onOpenSession(row.session_key as string)}
              data-testid="metrics-call-open-session"
            >
              {tx("metrics.calls.openTranscript", "Open the conversation")}
            </Button>
          )
          : null}
      </div>
    </div>
  );
}
