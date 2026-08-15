/**
 * Gate audit log viewer -- nanoinfraorg/nanoinfra#29.
 *
 * The viewer reads. It renders no control that deletes, prunes, or edits a record, because #16
 * makes the log append-only and a delete control would make that false. Retention belongs to the
 * policy panel, which drops whole expired segments.
 *
 * Two fields carry more weight than the layout. `samePath` marks a decision whose request and
 * approval arrived on one channel, and the row shows that mark even when the gate allowed the
 * action, because a later policy may relax the rule. `commandDigest` is what the log holds by
 * default, and the full text appears only under the opt-in with a warning beside it.
 *
 * The table shows the first resolved host and the host count rather than the label an operator
 * typed. #24 records the resolved addresses on purpose, so the viewer shows what ran.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { CircleAlert, Loader2, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fetchWithTimeout } from "@/lib/http";
import { cn } from "@/lib/utils";

const AUDIT_READ_PATH = "/api/webui/gates/audit";
const TIMEOUT_MS = 20_000;

/** Key spelling matches the route payload, so no layer translates a field name. */
export interface AuditRecord {
  ts: string | null;
  sessionId: string | null;
  executionContext: string | null;
  originPath: string | null;
  approvalPath: string | null;
  samePath: boolean;
  actor: string | null;
  capabilityClass: string | null;
  scope: string | null;
  hosts: string[];
  hostCount: number | null;
  commandDigest: string | null;
  commandText: string | null;
  holdsCommandText: boolean;
  decision: string | null;
  reason: string | null;
  grantId: string | null;
  tokenNonce: string | null;
  exitCode: number | null;
  durationMs: number | null;
  tool: string | null;
}

export interface AuditPage {
  records: AuditRecord[];
  total: number;
  limit: number;
  offset: number;
  recordsCommandText: boolean;
  choices: {
    decision: string[];
    capabilityClass: string[];
    executionContext: string[];
  };
}

interface Filters {
  decision: string;
  capabilityClass: string;
  executionContext: string;
  days: string;
}

const RANGE_DAYS = ["1", "7", "30", "any"];

const EMPTY_FILTERS: Filters = {
  decision: "",
  capabilityClass: "",
  executionContext: "",
  days: "7",
};

function sinceFor(days: string): string | null {
  if (days === "any") return null;
  const count = Number(days);
  if (!Number.isFinite(count) || count <= 0) return null;
  const moment = new Date(Date.now() - count * 24 * 60 * 60 * 1000);
  return moment.toISOString();
}

function queryFor(filters: Filters): string {
  const params = new URLSearchParams();
  if (filters.decision) params.set("decision", filters.decision);
  if (filters.capabilityClass) params.set("capabilityClass", filters.capabilityClass);
  if (filters.executionContext) params.set("executionContext", filters.executionContext);
  const since = sinceFor(filters.days);
  if (since) params.set("since", since);
  const query = params.toString();
  return query ? `${AUDIT_READ_PATH}?${query}` : AUDIT_READ_PATH;
}

function timeLabel(ts: string | null): string {
  if (!ts) return "--";
  const moment = new Date(ts);
  if (Number.isNaN(moment.getTime())) return ts;
  return moment.toLocaleString();
}

function targetLabel(record: AuditRecord): string {
  if (record.hosts.length > 0) return record.hosts[0];
  return "--";
}

export function GatesAuditLog({ token }: { token: string }) {
  const { t } = useTranslation();
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState<AuditPage | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const load = useCallback(
    async (next: Filters) => {
      setLoading(true);
      try {
        const res = await fetchWithTimeout(
          queryFor(next),
          {
            headers: { Authorization: `Bearer ${token}` },
            credentials: "same-origin",
          },
          TIMEOUT_MS,
        );
        if (!res.ok) {
          // A gateway with no gate runtime answers 503. That is not an empty log, and the
          // viewer must not render it as one.
          setUnreachable(true);
          setPage(null);
          return;
        }
        setUnreachable(false);
        setPage((await res.json()) as AuditPage);
      } catch {
        setUnreachable(true);
        setPage(null);
      } finally {
        setLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    void load(filters);
  }, [filters, load]);

  const choices = useMemo(
    () =>
      page?.choices ?? {
        decision: [],
        capabilityClass: [],
        executionContext: [],
      },
    [page],
  );

  const update = (field: keyof Filters, value: string) => {
    setOpenIndex(null);
    setFilters((current) => ({ ...current, [field]: value }));
  };

  return (
    <section className="space-y-3" data-testid="gates-audit-log">
      <header className="space-y-1">
        <h3 className="text-[15px] font-medium text-foreground">
          {t("settings.gates.audit.title", "Gate decisions")}
        </h3>
        <p className="text-[13px] text-muted-foreground">
          {t(
            "settings.gates.audit.subtitle",
            "One record per gate decision, newest first. The log appends, so this view reads only.",
          )}
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect
          label={t("settings.gates.audit.filter.decision", "Decision")}
          value={filters.decision}
          choices={choices.decision}
          anyLabel={t("settings.gates.audit.filter.any", "any")}
          onChange={(value) => update("decision", value)}
        />
        <FilterSelect
          label={t("settings.gates.audit.filter.class", "Class")}
          value={filters.capabilityClass}
          choices={choices.capabilityClass}
          anyLabel={t("settings.gates.audit.filter.any", "any")}
          onChange={(value) => update("capabilityClass", value)}
        />
        <FilterSelect
          label={t("settings.gates.audit.filter.context", "Context")}
          value={filters.executionContext}
          choices={choices.executionContext}
          anyLabel={t("settings.gates.audit.filter.any", "any")}
          onChange={(value) => update("executionContext", value)}
        />
        <FilterSelect
          label={t("settings.gates.audit.filter.range", "Range")}
          value={filters.days}
          choices={RANGE_DAYS}
          anyLabel={null}
          toLabel={(value) =>
            value === "any"
              ? t("settings.gates.audit.range.any", "every record")
              : t("settings.gates.audit.range.days", "last {{count}} days", {
                  count: Number(value),
                })
          }
          onChange={(value) => update("days", value)}
        />
        {loading ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : null}
      </div>

      {unreachable ? (
        <p className="flex items-start gap-2 rounded-[10px] border border-amber-500/40 bg-amber-500/5 p-3 text-[13px] text-foreground">
          <CircleAlert className="mt-[2px] h-4 w-4 shrink-0 text-amber-500" />
          {t(
            "settings.gates.audit.unreachable",
            "This gateway cannot reach the audit log. The gate may still enforce every decision, so treat this as a missing view and not as an empty log.",
          )}
        </p>
      ) : null}

      {page && page.records.length === 0 && !unreachable ? (
        <p className="rounded-[10px] border border-dashed border-border p-3 text-[13px] text-muted-foreground">
          {t(
            "settings.gates.audit.empty",
            "No decision matches these filters. The gate still records every decision it makes, so a wider range may show one.",
          )}
        </p>
      ) : null}

      {page && page.records.length > 0 ? (
        <div className="overflow-hidden rounded-[12px] border border-border">
          <table className="w-full text-left text-[13px]">
            <thead className="bg-muted/40 text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.time", "Time")}
                </th>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.decision", "Decision")}
                </th>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.class", "Class")}
                </th>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.context", "Context")}
                </th>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.target", "Target")}
                </th>
                <th className="px-3 py-2 font-medium">
                  {t("settings.gates.audit.column.hosts", "Hosts")}
                </th>
              </tr>
            </thead>
            <tbody>
              {page.records.map((entry, index) => (
                <tr
                  key={`${entry.ts ?? index}-${index}`}
                  data-testid={`audit-row-${index}`}
                  onClick={() => setOpenIndex(openIndex === index ? null : index)}
                  className={cn(
                    "cursor-pointer border-t border-border/60 hover:bg-muted/30",
                    openIndex === index && "bg-muted/40",
                  )}
                >
                  <td className="px-3 py-2 tabular-nums text-muted-foreground">
                    {timeLabel(entry.ts)}
                  </td>
                  <td className="px-3 py-2">
                    <span className="flex items-center gap-1.5">
                      <span className="font-medium text-foreground">{entry.decision ?? "--"}</span>
                      {entry.samePath ? (
                        <TriangleAlert
                          data-testid="audit-same-path"
                          className="h-3.5 w-3.5 text-amber-500"
                          aria-label={t(
                            "settings.gates.audit.samePath",
                            "The request and the approval arrived on one channel.",
                          )}
                        />
                      ) : null}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{entry.capabilityClass ?? "--"}</td>
                  <td className="px-3 py-2 text-muted-foreground">{entry.executionContext ?? "--"}</td>
                  <td className="px-3 py-2 text-muted-foreground">{targetLabel(entry)}</td>
                  <td className="px-3 py-2 tabular-nums text-muted-foreground">
                    {entry.hostCount ?? entry.hosts.length}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {page && openIndex !== null && page.records[openIndex] ? (
        <RecordDetail record={page.records[openIndex]} />
      ) : null}

      {page && page.total > page.records.length ? (
        <p className="text-[12px] text-muted-foreground">
          {t(
            "settings.gates.audit.truncated",
            "Showing {{shown}} of {{total}} matching records. Narrow the filters to see the rest.",
            { shown: page.records.length, total: page.total },
          )}
        </p>
      ) : null}
    </section>
  );
}

function FilterSelect({
  label,
  value,
  choices,
  anyLabel,
  toLabel,
  onChange,
}: {
  label: string;
  value: string;
  choices: string[];
  anyLabel: string | null;
  toLabel?: (value: string) => string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-[10px] border border-input bg-background px-2 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
      >
        {anyLabel !== null ? <option value="">{anyLabel}</option> : null}
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {toLabel ? toLabel(choice) : choice}
          </option>
        ))}
      </select>
    </label>
  );
}

function RecordDetail({ record }: { record: AuditRecord }) {
  const { t } = useTranslation();
  const rows: [string, string][] = [
    [t("settings.gates.audit.field.ts", "Time"), timeLabel(record.ts)],
    [t("settings.gates.audit.field.decision", "Decision"), record.decision ?? "--"],
    [t("settings.gates.audit.field.reason", "Reason"), record.reason ?? "--"],
    [t("settings.gates.audit.field.class", "Capability class"), record.capabilityClass ?? "--"],
    [t("settings.gates.audit.field.context", "Execution context"), record.executionContext ?? "--"],
    [t("settings.gates.audit.field.session", "Session"), record.sessionId ?? "--"],
    [t("settings.gates.audit.field.tool", "Tool"), record.tool ?? "--"],
    [t("settings.gates.audit.field.actor", "Actor"), record.actor ?? "--"],
    [t("settings.gates.audit.field.origin", "Origin path"), record.originPath ?? "--"],
    [t("settings.gates.audit.field.approval", "Approval path"), record.approvalPath ?? "--"],
    [t("settings.gates.audit.field.scope", "Scope"), record.scope ?? "--"],
    [t("settings.gates.audit.field.grant", "Grant"), record.grantId ?? "--"],
    [
      t("settings.gates.audit.field.hosts", "Resolved targets"),
      record.hosts.length > 0 ? record.hosts.join(", ") : "--",
    ],
    [t("settings.gates.audit.field.digest", "Command digest"), record.commandDigest ?? "--"],
    [t("settings.gates.audit.field.nonce", "Token nonce"), record.tokenNonce ?? "--"],
    [
      t("settings.gates.audit.field.exit", "Exit code"),
      record.exitCode === null ? "--" : String(record.exitCode),
    ],
    [
      t("settings.gates.audit.field.duration", "Duration"),
      record.durationMs === null ? "--" : `${record.durationMs} ms`,
    ],
  ];
  return (
    <div
      data-testid="audit-detail"
      className="space-y-2 rounded-[12px] border border-border bg-muted/20 p-3"
    >
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label} className="flex min-w-0 gap-2 text-[13px]">
            <dt className="w-[130px] shrink-0 text-muted-foreground">{label}</dt>
            <dd className="min-w-0 break-words text-foreground">{value}</dd>
          </div>
        ))}
      </dl>

      {record.samePath ? (
        <p className="flex items-start gap-2 text-[12px] text-amber-600 dark:text-amber-500">
          <TriangleAlert className="mt-[2px] h-3.5 w-3.5 shrink-0" />
          {t(
            "settings.gates.audit.samePathNote",
            "The request and the approval arrived on one channel. One compromised account then holds both halves.",
          )}
        </p>
      ) : null}

      {record.commandText ? (
        <div className="space-y-1">
          <p className="flex items-start gap-2 text-[12px] text-amber-600 dark:text-amber-500">
            <TriangleAlert className="mt-[2px] h-3.5 w-3.5 shrink-0" />
            {t(
              "settings.gates.audit.textWarning",
              "Full command text is on, so this record may hold a secret.",
            )}
          </p>
          <pre
            data-testid="audit-command-text"
            className="overflow-x-auto rounded-[8px] border border-border bg-background p-2 text-[12px] text-foreground"
          >
            {record.commandText}
          </pre>
        </div>
      ) : null}
    </div>
  );
}
