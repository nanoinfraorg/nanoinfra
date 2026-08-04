import { Lock } from "lucide-react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";

import { GROUP_COMPONENT_ID, findProvider } from "./componentCatalog";
import type { DiagramNodeData } from "./diagramTypes";

export function GroupNode({ data, selected }: NodeProps & { data: DiagramNodeData }) {
  const provider = findProvider(GROUP_COMPONENT_ID, data.providerId);
  const isConfigured = provider && provider.id !== "generic";
  // Never surface "secret" fields here — this legend is a glanceable summary
  // on the canvas, not a place to ever echo back sensitive values. Capped to
  // 2 fields so it stays a short caption instead of a full config dump that
  // sprawls the box's whole width on a single line.
  const legendFields = isConfigured
    ? provider.fields.filter((field) => field.kind !== "secret" && data.config[field.key])
    : [];
  const legend = legendFields.slice(0, 2).map((field) => `${field.label}: ${data.config[field.key]}`);
  const legendTitle = legendFields.map((field) => `${field.label}: ${data.config[field.key]}`).join(" · ");

  return (
    <div
      className={[
        "flex h-full w-full flex-col rounded-[14px] border-2 border-dashed bg-muted/10",
        selected ? "border-foreground/50" : "border-border/45",
      ].join(" ")}
    >
      <NodeResizer
        isVisible={selected && !data.locked}
        minWidth={220}
        minHeight={160}
        handleClassName="!h-2.5 !w-2.5 !rounded-full !border-none !bg-border"
        lineClassName="!border-border"
      />
      <Handle type="target" position={Position.Top} className="!h-2 !w-2 !border-none !bg-border" />
      <div className="flex flex-col gap-0.5 px-3 py-2">
        <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-foreground">
          <span className="truncate">{data.label}</span>
          {data.locked ? <Lock className="h-3 w-3 shrink-0 text-muted-foreground" aria-label="Locked" /> : null}
        </div>
        {isConfigured ? (
          <span className="truncate text-[10.5px] font-normal text-muted-foreground">{provider.label}</span>
        ) : null}
        {legend.length > 0 ? (
          <span className="truncate text-[10px] text-muted-foreground/80" title={legendTitle}>
            {legend.join(" · ")}
            {legendFields.length > legend.length ? " · …" : ""}
          </span>
        ) : null}
      </div>
      <Handle type="source" position={Position.Bottom} className="!h-2 !w-2 !border-none !bg-border" />
    </div>
  );
}
