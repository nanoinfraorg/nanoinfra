import { Lock } from "lucide-react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

import { getComponentIcon } from "./icons";
import { useComponentCatalog } from "./useComponentCatalog";
import { buildFieldLegend } from "./nodeLegend";
import type { DiagramNodeData } from "./diagramTypes";

export function DiagramNode({ data, selected }: NodeProps & { data: DiagramNodeData }) {
  const { findComponentType, findProvider } = useComponentCatalog();
  const type = findComponentType(data.componentTypeId);
  const Icon = getComponentIcon(type?.iconKey ?? "");
  const provider = findProvider(data.componentTypeId, data.providerId);
  const { legend, legendTitle, hasMore } = buildFieldLegend(provider, data.config);

  return (
    <div
      className={[
        "flex min-w-[180px] items-center gap-2.5 rounded-[14px] border bg-settings-surface px-3.5 py-2.5 text-left shadow-none",
        selected ? "border-foreground/60" : "border-border/45",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Top} className="!h-2 !w-2 !border-none !bg-border" />
      {/* Side handles are opt-in (via an edge's sourceHandle/targetHandle) for
          connections between nodes that sit side-by-side rather than stacked —
          without one, a mostly-horizontal edge still has to leave from the
          bottom and enter from the top, forcing a detour through whatever
          sits between the two default handles. */}
      <Handle
        type="target"
        position={Position.Left}
        id="left"
        className="!h-2 !w-2 !border-none !bg-border"
      />
      <Handle
        type="source"
        position={Position.Right}
        id="right"
        className="!h-2 !w-2 !border-none !bg-border"
      />
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-muted text-foreground">
        <Icon className="h-4 w-4" />
      </div>
      <div className="flex min-w-0 max-w-[220px] flex-col">
        <span className="truncate text-[13px] font-medium text-foreground">{data.label}</span>
        <span className="truncate text-[11px] text-muted-foreground">{type?.label ?? data.componentTypeId}</span>
        {provider ? (
          <span className="truncate text-[10.5px] text-muted-foreground/80">{provider.label}</span>
        ) : null}
        {legend.length > 0 ? (
          <span className="whitespace-normal break-words text-[10px] leading-snug text-muted-foreground/70" title={legendTitle}>
            {legend.join(" · ")}
            {hasMore ? " · …" : ""}
          </span>
        ) : null}
      </div>
      {data.locked ? (
        <Lock className="ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label="Locked" />
      ) : null}
      <Handle type="source" position={Position.Bottom} className="!h-2 !w-2 !border-none !bg-border" />
    </div>
  );
}
