import { Lock } from "lucide-react";
import type { NodeProps } from "@xyflow/react";

import { categoryBorderColor } from "./categoryColors";
import { ConnectionHandles } from "./ConnectionHandles";
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
  // Selection must stay unambiguous regardless of category, so the accent
  // only applies at rest -- selected keeps the existing neutral highlight.
  const accentColor = !selected ? categoryBorderColor(type?.category) : undefined;

  return (
    <div
      className={[
        "flex min-w-[180px] items-center gap-2.5 rounded-[14px] border bg-settings-surface px-3.5 py-2.5 text-left shadow-none",
        selected ? "border-foreground/60" : "border-border/45",
      ].join(" ")}
      style={accentColor ? { borderColor: accentColor } : undefined}
    >
      <ConnectionHandles />
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
    </div>
  );
}
