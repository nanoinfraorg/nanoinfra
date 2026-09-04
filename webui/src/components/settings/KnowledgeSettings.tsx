/**
 * Settings -> Knowledge -- nanoinfraorg/nanoinfra#243.
 *
 * Two halves. The top half is config: where documents go, which mode searches them, how often
 * the index runs, what is never indexed, and the caps. The bottom half is what the last pass
 * actually did, including what it refused -- because a document that was skipped and never
 * reported is indistinguishable from one that was indexed and never matched.
 *
 * Hybrid is greyed out with the install command when the extra is absent, rather than offered
 * and then failing at the first search. The save path refuses it too: this panel is a
 * convenience, and config is the authority.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { CircleAlert, Info, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { updateKnowledgeSettings } from "@/lib/api";
import type { KnowledgePayload, KnowledgeSettingsUpdate, SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

type Draft = KnowledgeSettingsUpdate;

const MEGABYTE = 1024 * 1024;

function draftFrom(knowledge: KnowledgePayload): Draft {
  return {
    enabled: knowledge.enabled,
    mode: knowledge.mode,
    reindexIntervalS: knowledge.reindex_interval_s,
    exclude: [...knowledge.exclude],
    maxFileBytes: knowledge.max_file_bytes,
    maxTotalBytes: knowledge.max_total_bytes,
    maxResults: knowledge.max_results,
  };
}

/** Bytes as whole megabytes, which is the unit an operator thinks in for a document cap. */
export function toMegabytes(bytes: number): number {
  return Math.max(1, Math.round(bytes / MEGABYTE));
}

export function describeSkipReason(reason: string): string {
  const reasons: Record<string, string> = {
    too_large: "larger than the per-file limit",
    total_budget: "the total index size limit was already reached",
    not_text: "not a text document",
    outside_workspace: "a symlink leaving the knowledge folder",
  };
  return reasons[reason] ?? reason;
}

export function describeInterval(seconds: number): string {
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return hours === 1 ? "every hour" : `every ${hours} hours`;
  }
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return minutes === 1 ? "every minute" : `every ${minutes} minutes`;
  }
  return `every ${seconds} seconds`;
}

export function KnowledgeSettings({
  token,
  settings,
  onSaved,
}: {
  token: string;
  settings: SettingsPayload;
  onSaved: (payload: SettingsPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const knowledge = settings.knowledge;
  const savedJson = useMemo(() => JSON.stringify(knowledge ?? null), [knowledge]);
  const [draft, setDraft] = useState<Draft | null>(() =>
    knowledge ? draftFrom(knowledge) : null,
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A fresh payload replaces the draft. An older gateway sends no block at all, and then this
  // panel stays away rather than rendering controls that save nothing.
  useEffect(() => {
    const parsed = JSON.parse(savedJson) as KnowledgePayload | null;
    setDraft(parsed ? draftFrom(parsed) : null);
    setError(null);
  }, [savedJson]);

  if (!knowledge || !draft) return null;

  const savedDraft = draftFrom(knowledge);
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedDraft);

  const change = (patch: Partial<Draft>) => {
    setDraft((previous) => (previous ? { ...previous, ...patch } : previous));
    setSaved(false);
    setError(null);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = await updateKnowledgeSettings(token, draft);
      setSaved(true);
      onSaved(payload);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const statusMessage = error
    ? error
    : saved
      ? tx(
        "settings.knowledge.status.saved",
        "Saved. The gateway indexes and searches with these after a restart.",
      )
      : dirty
        ? tx("settings.knowledge.status.unsaved", "Unsaved changes.")
        : undefined;

  const lastRun = knowledge.last_run;

  return (
    <div className="space-y-7" data-testid="knowledge-settings">
      <section>
        <KnowledgeTitle>{tx("settings.knowledge.title", "Knowledge")}</KnowledgeTitle>
        <KnowledgeGroup>
          <KnowledgeRow
            label={tx("settings.knowledge.enabled", "Search the knowledge base")}
            help={tx(
              "settings.knowledge.enabledHelp",
              "The agent reaches documents through the knowledge_search tool. Nothing is added to any prompt, so a knowledge base costs nothing on a turn that does not ask for it.",
            )}
          >
            <ToggleButton
              checked={draft.enabled}
              ariaLabel={tx("settings.knowledge.enabled", "Search the knowledge base")}
              label={
                draft.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")
              }
              onChange={(enabled) => change({ enabled })}
            />
          </KnowledgeRow>
          <KnowledgeRow
            label={tx("settings.knowledge.folder", "Documents folder")}
            help={tx(
              "settings.knowledge.folderHelp",
              "Folders and subfolders, whatever you drop there. The index lives beside them in .index, so a restored workspace restores its index too.",
            )}
          >
            <code
              className="max-w-full truncate rounded-full bg-muted px-3 py-1 text-[12px] text-muted-foreground"
              data-testid="knowledge-path"
            >
              {knowledge.path}
            </code>
          </KnowledgeRow>
          <KnowledgeRow
            label={tx("settings.knowledge.mode", "Mode")}
            help={tx(
              "settings.knowledge.modeHelp",
              "Lexical is BM25F over the words in the document. It will not match \"pod won't start\" against a document that says \"CrashLoopBackOff\".",
            )}
          >
            <div className="flex items-center gap-2">
              <select
                className="h-8 rounded-full border border-input bg-background px-3 text-[13px]"
                aria-label={tx("settings.knowledge.mode", "Mode")}
                data-testid="knowledge-mode"
                value={draft.mode}
                onChange={(event) => change({ mode: event.target.value as Draft["mode"] })}
              >
                <option value="lexical">
                  {tx("settings.knowledge.modeLexical", "Lexical (BM25F)")}
                </option>
                <option value="hybrid" disabled={!knowledge.hybrid_available}>
                  {tx("settings.knowledge.modeHybrid", "Hybrid (BM25F + vectors)")}
                </option>
              </select>
            </div>
          </KnowledgeRow>
          {knowledge.hybrid_available ? null : (
            <KnowledgeNote testId="knowledge-hybrid-hint">
              {tx(
                "settings.knowledge.hybridMissing",
                "Hybrid needs an extra that is not installed. Install it and restart:",
              )}{" "}
              <code className="rounded bg-muted px-1.5 py-0.5">
                {knowledge.hybrid_install_hint ?? "pip install 'semlix[semantic]'"}
              </code>
            </KnowledgeNote>
          )}
          <KnowledgeRow
            label={tx("settings.knowledge.schedule", "Reindex")}
            help={tx(
              "settings.knowledge.scheduleHelp",
              "The full pass: it collects deletions and catches what nobody searched for. A document you just saved is indexed by the search itself, so this can stay quiet.",
            )}
          >
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={60}
                value={draft.reindexIntervalS}
                aria-label={tx("settings.knowledge.schedule", "Reindex")}
                data-testid="knowledge-interval"
                onChange={(event) => {
                  const parsed = Number(event.target.value);
                  if (!Number.isFinite(parsed)) return;
                  change({ reindexIntervalS: Math.trunc(parsed) });
                }}
                className="h-8 w-24 max-w-full rounded-full text-[13px]"
              />
              <span className="text-[12px] text-muted-foreground">
                {tx("settings.knowledge.seconds", "seconds")} · {describeInterval(draft.reindexIntervalS)}
              </span>
            </div>
          </KnowledgeRow>
        </KnowledgeGroup>
      </section>

      <section>
        <KnowledgeTitle>{tx("settings.knowledge.limits", "Limits")}</KnowledgeTitle>
        <KnowledgeGroup>
          <KnowledgeRow
            label={tx("settings.knowledge.maxFile", "Largest document")}
            help={tx(
              "settings.knowledge.maxFileHelp",
              "One 2 GB log file must not become the knowledge base. A file over the limit is skipped and named below, never silently dropped.",
            )}
          >
            <MegabyteInput
              label={tx("settings.knowledge.maxFile", "Largest document")}
              testId="knowledge-max-file"
              bytes={draft.maxFileBytes}
              onChange={(maxFileBytes) => change({ maxFileBytes })}
              unit={tx("settings.knowledge.megabytes", "MB")}
            />
          </KnowledgeRow>
          <KnowledgeRow
            label={tx("settings.knowledge.maxTotal", "Total indexed")}
            help={tx(
              "settings.knowledge.maxTotalHelp",
              "Once this is reached the pass stops adding documents and reports each one it dropped.",
            )}
          >
            <MegabyteInput
              label={tx("settings.knowledge.maxTotal", "Total indexed")}
              testId="knowledge-max-total"
              bytes={draft.maxTotalBytes}
              onChange={(maxTotalBytes) => change({ maxTotalBytes })}
              unit={tx("settings.knowledge.megabytes", "MB")}
            />
          </KnowledgeRow>
          <KnowledgeRow
            label={tx("settings.knowledge.maxResults", "Fragments per search")}
            help={tx(
              "settings.knowledge.maxResultsHelp",
              "Small on purpose: a citation the model has to read is worth more than ten it skims.",
            )}
          >
            <Input
              type="number"
              min={1}
              max={25}
              value={draft.maxResults}
              aria-label={tx("settings.knowledge.maxResults", "Fragments per search")}
              data-testid="knowledge-max-results"
              onChange={(event) => {
                const parsed = Number(event.target.value);
                if (!Number.isFinite(parsed)) return;
                change({ maxResults: Math.trunc(parsed) });
              }}
              className="h-8 w-20 max-w-full rounded-full text-[13px]"
            />
          </KnowledgeRow>
        </KnowledgeGroup>
      </section>

      <section>
        <KnowledgeTitle>{tx("settings.knowledge.excludeTitle", "Never indexed")}</KnowledgeTitle>
        <KnowledgeGroup>
          <div className="px-4 py-3.5 sm:px-5">
            <textarea
              className="min-h-[7rem] w-full rounded-[14px] border border-input bg-background px-3 py-2 font-mono text-[12.5px] leading-6"
              aria-label={tx("settings.knowledge.excludeTitle", "Never indexed")}
              data-testid="knowledge-exclude"
              value={draft.exclude.join("\n")}
              onChange={(event) =>
                change({
                  exclude: event.target.value
                    .split("\n")
                    .map((line) => line.trim())
                    .filter((line) => line.length > 0),
                })
              }
            />
          </div>
          <KnowledgeNote>
            {tx(
              "settings.knowledge.excludeHelp",
              "One glob per line. The secret exclusions are pre-filled and removable only deliberately: your tree holds secrets nanoinfra cannot guess. A symlink leaving this folder is refused whatever this list says.",
            )}
          </KnowledgeNote>
        </KnowledgeGroup>
      </section>

      <section>
        <KnowledgeTitle>{tx("settings.knowledge.lastRunTitle", "Last run")}</KnowledgeTitle>
        <KnowledgeGroup>
          <KnowledgeRow
            label={tx("settings.knowledge.indexed", "Indexed")}
            help={
              knowledge.exists
                ? tx("settings.knowledge.indexedHelp", "Documents and the fragments they hold.")
                : tx(
                  "settings.knowledge.noFolder",
                  "The folder does not exist yet. Create it and drop a document in.",
                )
            }
          >
            <span className="text-[13px] text-muted-foreground" data-testid="knowledge-counts">
              {knowledge.documents} {tx("settings.knowledge.documents", "documents")} ·{" "}
              {knowledge.fragments} {tx("settings.knowledge.fragments", "fragments")} ·{" "}
              {toMegabytes(knowledge.indexed_bytes || 1)} {tx("settings.knowledge.megabytes", "MB")}
            </span>
          </KnowledgeRow>
          {lastRun ? (
            <KnowledgeRow
              label={tx("settings.knowledge.lastPass", "What it did")}
              help={new Date(lastRun.finished_at_ms).toLocaleString()}
            >
              <span className="text-[13px] text-muted-foreground" data-testid="knowledge-last-run">
                {lastRun.added} {tx("settings.knowledge.added", "added")} · {lastRun.updated}{" "}
                {tx("settings.knowledge.updated", "updated")} · {lastRun.removed}{" "}
                {tx("settings.knowledge.removed", "removed")} · {lastRun.skipped}{" "}
                {tx("settings.knowledge.skipped", "skipped")} · {lastRun.errors}{" "}
                {tx("settings.knowledge.failed", "failed")} · {lastRun.duration_ms}ms
              </span>
            </KnowledgeRow>
          ) : (
            <KnowledgeNote testId="knowledge-never-ran">
              {tx(
                "settings.knowledge.neverRan",
                "No indexing pass has run yet.",
              )}
            </KnowledgeNote>
          )}
          {knowledge.skipped.map((skip) => (
            <KnowledgeNote key={skip.path} tone="warning" testId="knowledge-skip">
              {skip.path}: {describeSkipReason(skip.reason)}
              {skip.detail ? ` (${skip.detail})` : ""}
            </KnowledgeNote>
          ))}
          {knowledge.errors.map((message) => (
            <KnowledgeNote key={message} tone="warning" testId="knowledge-error">
              {message}
            </KnowledgeNote>
          ))}
          {knowledge.indexed_mode !== knowledge.mode ? (
            <KnowledgeNote testId="knowledge-mode-drift">
              {tx(
                "settings.knowledge.modeDrift",
                "The stored index was built in another mode. The next pass rebuilds it.",
              )}
            </KnowledgeNote>
          ) : null}
        </KnowledgeGroup>
      </section>

      <KnowledgeGroup>
        <div className="flex min-h-[58px] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div
            className={cn(
              "min-w-0 text-[13px] leading-5",
              error ? "text-destructive-text" : "text-muted-foreground",
            )}
            data-testid="knowledge-status"
          >
            {statusMessage}
          </div>
          <div className="flex w-full shrink-0 flex-wrap justify-end gap-2 sm:w-auto">
            <Button
              size="sm"
              variant="ghost"
              className="rounded-full"
              disabled={!dirty || saving}
              onClick={() => {
                setDraft(savedDraft);
                setError(null);
                setSaved(false);
              }}
            >
              {tx("settings.knowledge.actions.discard", "Discard")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              disabled={!dirty || saving}
              onClick={() => void save()}
              data-testid="knowledge-save"
            >
              {saving ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {tx("settings.knowledge.actions.save", "Save")}
            </Button>
          </div>
        </div>
      </KnowledgeGroup>
    </div>
  );
}

function MegabyteInput({
  bytes,
  onChange,
  label,
  unit,
  testId,
}: {
  bytes: number;
  onChange: (bytes: number) => void;
  label: string;
  unit: string;
  testId: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <Input
        type="number"
        min={1}
        value={toMegabytes(bytes)}
        aria-label={label}
        data-testid={testId}
        onChange={(event) => {
          const parsed = Number(event.target.value);
          if (!Number.isFinite(parsed) || parsed < 1) return;
          onChange(Math.trunc(parsed) * MEGABYTE);
        }}
        className="h-8 w-24 max-w-full rounded-full text-[13px]"
      />
      <span className="text-[12px] text-muted-foreground">{unit}</span>
    </div>
  );
}

function KnowledgeTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
      {children}
    </h2>
  );
}

function KnowledgeGroup({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-[22px] bg-settings-surface">
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

function KnowledgeRow({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="text-[14px] font-medium leading-5 text-foreground">{label}</div>
        {help ? (
          <div className="mt-0.5 max-w-[30rem] text-[12px] leading-5 text-muted-foreground">
            {help}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}

function KnowledgeNote({
  children,
  tone = "info",
  testId,
}: {
  children: ReactNode;
  tone?: "info" | "warning";
  testId?: string;
}) {
  const Icon = tone === "warning" ? CircleAlert : Info;
  return (
    <div
      className={cn(
        "flex gap-2 px-4 py-3 text-[12px] leading-5 sm:px-5",
        tone === "warning" ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground",
      )}
      data-testid={testId}
      data-tone={tone}
    >
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}
