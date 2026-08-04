import { useState } from "react";
import { FileText, Plus, Trash2 } from "lucide-react";

import { DiagramDeleteConfirm } from "./DiagramDeleteConfirm";
import type { DiagramSummary } from "./diagramStore";

interface DiagramListProps {
  diagrams: DiagramSummary[];
  onOpen: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export function DiagramList({ diagrams, onOpen, onNew, onDelete }: DiagramListProps) {
  const [pendingDelete, setPendingDelete] = useState<DiagramSummary | null>(null);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold text-foreground">Infra Diagrams</span>
          <span className="text-[11px] text-muted-foreground">
            {diagrams.length > 0 ? `${diagrams.length} saved` : "No diagrams yet — mock/local storage only"}
          </span>
        </div>
        <button
          type="button"
          onClick={onNew}
          className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
        >
          <Plus className="h-3.5 w-3.5" /> New Diagram
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {diagrams.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
            Nothing saved yet — start a new diagram to generate one.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {diagrams.map((d) => (
              <div
                key={d.id}
                role="button"
                tabIndex={0}
                onClick={() => onOpen(d.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") onOpen(d.id);
                }}
                className="group relative flex cursor-pointer flex-col gap-1.5 rounded-[14px] border border-border/45 bg-settings-surface p-4 text-left hover:bg-muted/70"
              >
                <div className="flex items-center gap-2 pr-6 text-[13px] font-semibold text-foreground">
                  <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{d.name}</span>
                </div>
                <span className="text-[11px] text-muted-foreground">
                  {d.nodeCount} component{d.nodeCount === 1 ? "" : "s"} ·{" "}
                  {d.targets.length > 0 ? d.targets.join(", ") : "no targets"}
                </span>
                <span className="text-[10.5px] text-muted-foreground/70">
                  Updated {new Date(d.updatedAt).toLocaleString()}
                </span>
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    setPendingDelete(d);
                  }}
                  aria-label={`Delete ${d.name}`}
                  className="absolute right-2 top-2 hidden h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive group-hover:flex"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <DiagramDeleteConfirm
        open={pendingDelete !== null}
        diagramName={pendingDelete?.name ?? ""}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) onDelete(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
    </div>
  );
}
