import { Lock } from "lucide-react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

import { COMPONENT_ICONS } from "./icons";
import { findComponentType, findProvider } from "./componentCatalog";
import { buildFieldLegend } from "./nodeLegend";
import type { DiagramNodeData } from "./diagramTypes";

export function DiagramNode({ data, selected }: NodeProps & { data: DiagramNodeData }) {
  const type = findComponentType(data.componentTypeId);
  const Icon = type ? COMPONENT_ICONS[type.iconKey] : undefined;
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
      {Icon ? (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-muted text-foreground">
          <Icon className="h-4 w-4" />
        </div>
      ) : null}
      <div className="flex min-w-0 flex-col">
        <span className="text-[13px] font-medium text-foreground">{data.label}</span>
        <span className="text-[11px] text-muted-foreground">{type?.label ?? data.componentTypeId}</span>
        {provider ? (
          <span className="truncate text-[10.5px] text-muted-foreground/80">{provider.label}</span>
        ) : null}
        {legend.length > 0 ? (
          <span className="truncate text-[10px] text-muted-foreground/70" title={legendTitle}>
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
