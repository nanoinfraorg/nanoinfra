import { Lock } from "lucide-react";
import { NodeResizer, type NodeProps } from "@xyflow/react";

import { ConnectionHandles } from "./ConnectionHandles";
import { useComponentCatalog } from "./useComponentCatalog";
import { buildFieldLegend } from "./nodeLegend";
import type { DiagramNodeData } from "./diagramTypes";

export function GroupNode({ data, selected }: NodeProps & { data: DiagramNodeData }) {
  const { findProvider } = useComponentCatalog();
  // Use the node's own componentTypeId, not a hardcoded sentinel — every
  // group node's data.componentTypeId already points at whichever fetched
  // catalog type has isGroup: true.
  const provider = findProvider(data.componentTypeId, data.providerId);
  const isConfigured = provider && provider.id !== "generic";
  const { legend, legendTitle, hasMore } = buildFieldLegend(isConfigured ? provider : undefined, data.config);

  return (
    <div
      className={[
        "flex h-full w-full flex-col rounded-[14px] border-2 border-dashed bg-muted/10",
        selected ? "border-foreground/50" : "border-border",
      ].join(" ")}
    >
      <NodeResizer
        isVisible={selected && !data.locked}
        minWidth={220}
        minHeight={160}
        handleClassName="!h-2.5 !w-2.5 !rounded-full !border-none !bg-border"
        lineClassName="!border-border"
      />
      <ConnectionHandles />
      <div className="flex flex-col gap-0.5 px-3 py-2">
        <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-foreground">
          <span className="truncate">{data.label}</span>
          {data.locked ? <Lock className="h-3 w-3 shrink-0 text-muted-foreground" aria-label="Locked" /> : null}
        </div>
        {isConfigured ? (
          <span className="truncate text-[10.5px] font-normal text-muted-foreground">{provider.label}</span>
        ) : null}
        {legend.length > 0 ? (
          <span className="whitespace-normal break-words text-[10px] leading-snug text-muted-foreground/80" title={legendTitle}>
            {legend.join(" · ")}
            {hasMore ? " · …" : ""}
          </span>
        ) : null}
      </div>
    </div>
  );
}
