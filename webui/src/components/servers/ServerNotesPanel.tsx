import { useCallback, useEffect, useState } from "react";
import { NotebookPen, Pencil, Plus, Save, User, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import {
  appendServerNote,
  fetchServerNotes,
  fetchServerNotesArchive,
  saveServerNotes,
  type ServerNoteEntry,
  type ServerNotesPayload,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

interface ServerNotesPanelProps {
  serverId: string;
}

/**
 * A box's memory, rendered where the box is configured (#229).
 *
 * Newest first, because the newest entry is the one that changes what you would do next — the file
 * itself stays chronological, so the reversal is a view and not a storage decision.
 *
 * Two write paths and they are not the same act. **Add note** appends one entry and the author is
 * stamped by the gateway from the identity it verified, never typed here; that is what makes an
 * `(operator)` mark mean something an agent cannot claim (#228). **Edit** hands over the whole
 * markdown file, because a human owns this file as much as the agent does and correcting a typo in
 * somebody's sentence is not an append.
 */
export function ServerNotesPanel({ serverId }: ServerNotesPanelProps) {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [notes, setNotes] = useState<ServerNotesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [composing, setComposing] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [archive, setArchive] = useState<ServerNoteEntry[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchServerNotes(getToken(), serverId)
      .then((payload) => {
        if (!cancelled) setNotes(payload);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [getToken, serverId]);

  const handleAppend = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = await appendServerNote(getToken(), serverId, { title, body });
      setNotes(payload);
      setTitle("");
      setBody("");
      setComposing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [getToken, serverId, title, body]);

  const handleSaveRaw = useCallback(async () => {
    if (draft === null) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await saveServerNotes(getToken(), serverId, draft);
      setNotes(payload);
      setDraft(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [draft, getToken, serverId]);

  const handleShowArchive = useCallback(async () => {
    setError(null);
    try {
      const payload = await fetchServerNotesArchive(getToken(), serverId);
      setArchive(payload.entries);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [getToken, serverId]);

  const entries = [...(notes?.entries ?? [])].reverse();

  return (
    <div className="block">
      <span className="flex items-center justify-between gap-2 text-[11px] font-medium text-foreground/85">
        <span className="flex items-center gap-1.5">
          <NotebookPen className="h-3.5 w-3.5 text-muted-foreground" />
          {t("serverNotes.title", { defaultValue: "Device notes" })}
        </span>
        <span className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => {
              setComposing((value) => !value);
              setDraft(null);
            }}
            className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/70 hover:text-foreground"
          >
            <Plus className="h-3 w-3" /> {t("serverNotes.add", { defaultValue: "Add note" })}
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(draft === null ? (notes?.text ?? "") : null);
              setComposing(false);
            }}
            className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/70 hover:text-foreground"
          >
            {draft === null ? <Pencil className="h-3 w-3" /> : <X className="h-3 w-3" />}
            {draft === null
              ? t("serverNotes.edit", { defaultValue: "Edit" })
              : t("serverNotes.cancelEdit", { defaultValue: "Cancel" })}
          </button>
        </span>
      </span>

      <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
        {t("serverNotes.hint", {
          defaultValue:
            "What a future visitor to this box needs to know. A note does not expire — one that disagrees with what you see is evidence the infrastructure changed.",
        })}
      </span>

      {error ? (
        <span className="mt-1 block text-[12px] text-destructive-text">{error}</span>
      ) : null}

      {composing ? (
        <div className="mt-2 flex flex-col gap-1.5 rounded-[12px] border border-border/45 bg-muted/25 p-2.5">
          <Input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder={t("serverNotes.titlePlaceholder", {
              defaultValue: "e.g. journald is deliberate",
            })}
            aria-label={t("serverNotes.titleLabel", { defaultValue: "Note title" })}
            className="h-9 rounded-[10px] border-border/60 bg-background/60 text-[13px]"
          />
          <textarea
            value={body}
            onChange={(event) => setBody(event.target.value)}
            placeholder={t("serverNotes.bodyPlaceholder", {
              defaultValue: "The conclusion, and what not to 'fix'.",
            })}
            aria-label={t("serverNotes.bodyLabel", { defaultValue: "Note body" })}
            rows={4}
            className="w-full resize-y rounded-[10px] border border-border/60 bg-background/60 p-2 text-[13px] text-foreground outline-none"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy || !title.trim() || !body.trim()}
              onClick={() => void handleAppend()}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Save className="h-3 w-3" />{" "}
              {t("serverNotes.append", { defaultValue: "Append" })}
            </button>
            <span className="text-[11px] text-muted-foreground">
              {t("serverNotes.authorNote", {
                defaultValue: "Signed as the operator, with your verified identity.",
              })}
            </span>
          </div>
        </div>
      ) : null}

      {draft !== null ? (
        <div className="mt-2 flex flex-col gap-1.5">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label={t("serverNotes.rawLabel", { defaultValue: "Notes markdown" })}
            rows={14}
            spellCheck={false}
            className="w-full resize-y rounded-[10px] border border-border/60 bg-muted/35 p-2 font-mono text-[12px] text-foreground outline-none"
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleSaveRaw()}
            className="flex h-8 w-fit items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Save className="h-3 w-3" />{" "}
            {t("serverNotes.saveRaw", { defaultValue: "Save notes" })}
          </button>
        </div>
      ) : (
        <div className="mt-2 flex flex-col gap-2">
          {entries.length === 0 ? (
            <span className="text-[11px] text-muted-foreground">
              {t("serverNotes.empty", {
                defaultValue: "No notes yet — the agent writes here when it learns something.",
              })}
            </span>
          ) : (
            entries.map((entry, index) => <NoteCard key={index} entry={entry} />)
          )}
        </div>
      )}

      {notes?.hasArchive && draft === null ? (
        <div className="mt-2">
          {archive === null ? (
            <button
              type="button"
              onClick={() => void handleShowArchive()}
              className="rounded-full px-2 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/70 hover:text-foreground"
            >
              {t("serverNotes.showArchive", { defaultValue: "Show rotated-out entries" })}
            </button>
          ) : (
            <div className="flex flex-col gap-2">
              <span className="text-[11px] font-medium text-foreground/85">
                {t("serverNotes.archive", { defaultValue: "Archive" })}
              </span>
              {[...archive].reverse().map((entry, index) => (
                <NoteCard key={`archive-${index}`} entry={entry} />
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function NoteCard({ entry }: { entry: ServerNoteEntry }) {
  const { t } = useTranslation();
  return (
    <div
      className={
        entry.isOperator
          ? "flex flex-col gap-1 rounded-[12px] border border-primary/40 bg-primary/5 p-2.5"
          : "flex flex-col gap-1 rounded-[12px] border border-border/45 bg-settings-surface p-2.5"
      }
    >
      <div className="flex flex-wrap items-center gap-1.5 text-[12px] font-semibold text-foreground">
        <span className="truncate">{entry.title}</span>
        {entry.isOperator ? (
          <span className="flex items-center gap-1 rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-foreground">
            <User className="h-2.5 w-2.5" />
            {t("serverNotes.operator", { defaultValue: "operator" })}
          </span>
        ) : null}
      </div>
      <div className="text-[11px] text-muted-foreground">
        {entry.when} · {entry.author}
      </div>
      <div className="whitespace-pre-wrap text-[12.5px] leading-5 text-foreground/90">
        {entry.body}
      </div>
    </div>
  );
}
