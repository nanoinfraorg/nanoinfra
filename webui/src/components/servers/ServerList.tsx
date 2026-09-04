import { useState } from "react";
import { NotebookPen, Plus, Server as ServerIcon, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ServerDeleteConfirm } from "./ServerDeleteConfirm";
import type { ServerSummary } from "@/lib/api";

interface ServerListProps {
  servers: ServerSummary[];
  onOpen: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export function ServerList({ servers, onOpen, onNew, onDelete }: ServerListProps) {
  const { t } = useTranslation();
  const [pendingDelete, setPendingDelete] = useState<ServerSummary | null>(null);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold text-foreground">
            {t("sidebar.servers", { defaultValue: "Servers" })}
          </span>
          <span className="text-[11px] text-muted-foreground">
            {servers.length > 0 ? `${servers.length} saved` : "No servers yet"}
          </span>
        </div>
        <button
          type="button"
          onClick={onNew}
          className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
        >
          <Plus className="h-3.5 w-3.5" /> New Server
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {servers.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
            Nothing saved yet — add a server to build out your inventory.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {servers.map((s) => (
              <div
                key={s.id}
                role="button"
                tabIndex={0}
                onClick={() => onOpen(s.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") onOpen(s.id);
                }}
                className="group relative flex cursor-pointer flex-col gap-1.5 rounded-[14px] border border-border/45 bg-settings-surface p-4 text-left hover:bg-muted/70"
              >
                <div className="flex items-center gap-2 pr-6 text-[13px] font-semibold text-foreground">
                  <ServerIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{s.name}</span>
                </div>
                <span className="text-[11px] text-muted-foreground">
                  {s.providerId} · {s.tags.length > 0 ? s.tags.join(", ") : "no tags"}
                </span>
                <span className="text-[10.5px] text-muted-foreground/70">
                  Updated {new Date(s.updatedAt).toLocaleString()}
                </span>
                {/* The scalar the record carries rather than the prose (#225): the gallery says a
                    box has memory and when it was last touched, and reads not a word of it. */}
                {s.notesUpdatedAt ? (
                  <span className="flex items-center gap-1 text-[10.5px] text-muted-foreground/70">
                    <NotebookPen className="h-3 w-3 shrink-0" />
                    {t("serverNotes.lastNote", {
                      defaultValue: "Notes {{when}}",
                      when: new Date(s.notesUpdatedAt).toLocaleDateString(),
                    })}
                  </span>
                ) : null}
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    setPendingDelete(s);
                  }}
                  aria-label={`Delete ${s.name}`}
                  className="absolute right-2 top-2 hidden h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive-text group-hover:flex"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <ServerDeleteConfirm
        open={pendingDelete !== null}
        serverName={pendingDelete?.name ?? ""}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) onDelete(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
    </div>
  );
}
