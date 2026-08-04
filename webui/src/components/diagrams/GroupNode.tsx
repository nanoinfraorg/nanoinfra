import { Lock } from "lucide-react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";

import type { DiagramNodeData } from "./diagramTypes";

export function GroupNode({ data, selected }: NodeProps & { data: DiagramNodeData }) {
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
      <Handle type="target" position={Position.Top} className="!h-2 !w-2 !border-none !bg-border" />
      <div className="flex items-center gap-1.5 px-3 py-2 text-[12.5px] font-medium text-foreground">
        <span className="truncate">{data.label}</span>
        {data.locked ? <Lock className="h-3 w-3 shrink-0 text-muted-foreground" aria-label="Locked" /> : null}
      </div>
      <Handle type="source" position={Position.Bottom} className="!h-2 !w-2 !border-none !bg-border" />
    </div>
  );
}
