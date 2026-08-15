import { useState } from "react";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { SecretDeleteConfirm } from "./SecretDeleteConfirm";
import type { SecretSummary } from "@/lib/api";

interface SecretListProps {
  secrets: SecretSummary[];
  onOpen: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export function SecretList({ secrets, onOpen, onNew, onDelete }: SecretListProps) {
  const { t } = useTranslation();
  const [pendingDelete, setPendingDelete] = useState<SecretSummary | null>(null);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold text-foreground">
            {t("sidebar.secrets", { defaultValue: "Secrets" })}
          </span>
          <span className="text-[11px] text-muted-foreground">
            {secrets.length > 0 ? `${secrets.length} saved` : "No secrets yet"}
          </span>
        </div>
        <button
          type="button"
          onClick={onNew}
          className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
        >
          <Plus className="h-3.5 w-3.5" /> New Secret
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {secrets.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
            Nothing saved yet — add a secret to use it from a server.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {secrets.map((s) => (
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
                  <KeyRound className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{s.name}</span>
                </div>
                <span className="text-[11px] text-muted-foreground">
                  {s.kind} · {s.providerId}
                </span>
                <span className="text-[10.5px] text-muted-foreground/70">
                  Updated {new Date(s.updatedAt).toLocaleString()}
                </span>
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

      <SecretDeleteConfirm
        open={pendingDelete !== null}
        secretName={pendingDelete?.name ?? ""}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) onDelete(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
    </div>
  );
}
