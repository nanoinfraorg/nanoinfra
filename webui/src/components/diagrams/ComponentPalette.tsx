import { useState } from "react";
import { ChevronRight, Group as GroupIcon } from "lucide-react";

import { COMPONENT_TYPES, GROUP_COMPONENT_ID } from "./componentCatalog";
import { COMPONENT_ICONS } from "./icons";
import { PALETTE_DRAG_MIME } from "./DiagramCanvas";

const CATEGORIES = ["Edge", "Compute", "Applications", "Data"] as const;

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

      <div className="flex flex-col gap-1">
        <div className="px-1 text-[11px] font-medium text-muted-foreground">Layout</div>
        <div
          draggable
          onDragStart={(event) => {
            event.dataTransfer.setData(PALETTE_DRAG_MIME, GROUP_COMPONENT_ID);
            event.dataTransfer.effectAllowed = "move";
          }}
          onDoubleClick={() => onAdd(GROUP_COMPONENT_ID)}
          className="touch-target flex h-10 w-full cursor-grab items-center gap-2.5 rounded-[11px] px-2.5 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-muted/70 active:cursor-grabbing"
          title="A nameable container — drag other components inside it, and connect it to other things as a single unit (e.g. a Kubernetes Cluster or Scaling Group)."
        >
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] bg-muted">
            <GroupIcon className="h-3.5 w-3.5" />
          </span>
          <span className="flex-1">Group</span>
        </div>
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
                  <div className="ml-9 flex flex-col gap-1 pb-2 pt-0.5">
                    {component.providers.map((provider) => (
                      <span key={provider.id} className="text-[12px] leading-tight text-muted-foreground">
                        {provider.label}
                      </span>
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
