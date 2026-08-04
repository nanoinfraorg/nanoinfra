import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { COMPONENT_TYPES } from "./componentCatalog";
import { COMPONENT_ICONS } from "./icons";
import { PALETTE_DRAG_MIME } from "./DiagramCanvas";

const CATEGORIES = ["Edge", "Compute", "Data"] as const;

export function ComponentPalette({ onAdd }: { onAdd: (componentTypeId: string) => void }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  return (
    <div className="flex h-full w-[230px] shrink-0 flex-col gap-4 overflow-y-auto border-r border-border bg-settings-surface px-3 py-4">
      <div className="px-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Components
      </div>
      <div className="px-1 text-[11px] text-muted-foreground">
        Click to see providers · drag onto the canvas to add.
      </div>
      {CATEGORIES.map((category) => (
        <div key={category} className="flex flex-col gap-1">
          <div className="px-1 text-[11px] font-medium text-muted-foreground">{category}</div>
          {COMPONENT_TYPES.filter((c) => c.category === category).map((component) => {
            const Icon = COMPONENT_ICONS[component.iconKey];
            const expanded = expandedId === component.id;
            return (
              <div key={component.id} className="flex flex-col">
                <div
                  draggable
                  onDragStart={(event) => {
                    event.dataTransfer.setData(PALETTE_DRAG_MIME, component.id);
                    event.dataTransfer.effectAllowed = "move";
                  }}
                  onClick={() => setExpandedId(expanded ? null : component.id)}
                  // Double-click still adds the component directly for
                  // keyboard/touch users who can't drag-and-drop.
                  onDoubleClick={() => onAdd(component.id)}
                  className="touch-target flex h-10 w-full cursor-grab items-center gap-2.5 rounded-[11px] px-2.5 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-muted/70 active:cursor-grabbing"
                >
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] bg-muted">
                    <Icon className="h-3.5 w-3.5" />
                  </span>
                  <span className="flex-1">{component.label}</span>
                  <ChevronRight
                    className={["h-3.5 w-3.5 text-muted-foreground transition-transform", expanded ? "rotate-90" : ""].join(" ")}
                  />
                </div>
                {expanded ? (
                  <div className="ml-9 flex flex-col gap-0.5 pb-1.5 pt-0.5">
                    {component.providers.map((provider) => (
                      <div key={provider.id} className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                        <span>{provider.label}</span>
                        <code className="rounded bg-muted px-1 py-0.5 text-[10px]">{provider.kind}</code>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
