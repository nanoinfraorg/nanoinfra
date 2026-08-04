import { useCallback, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type EdgeMouseHandler,
  type NodeMouseHandler,
  type ReactFlowInstance,
} from "@xyflow/react";
import { LayoutGrid } from "lucide-react";
import "@xyflow/react/dist/base.css";

import { autoLayout } from "./autoLayout";
import { DiagramNode } from "./DiagramNode";
import { defaultEdgeLabel } from "./edgeDefaults";
import type { DiagramNodeData } from "./diagramTypes";

const NODE_TYPES = { component: DiagramNode };

// Applied to every edge — both the seeded ones and any drawn by hand — so a
// new connection never falls back to React Flow's default (unstyled, which
// renders as a solid black label chip without this).
const EDGE_LABEL_STYLE = {
  labelBgPadding: [6, 3] as [number, number],
  labelBgBorderRadius: 6,
  labelStyle: { fill: "hsl(var(--foreground))", fontSize: 11, fontWeight: 500 },
  labelBgStyle: { fill: "hsl(var(--settings-surface))", stroke: "hsl(var(--border))", strokeWidth: 1 },
  style: { stroke: "hsl(var(--border))" },
};

// minZoom is a floor on readability, not a guarantee every node is visible —
// a diagram that doesn't fit at this zoom stays pannable/scrollable instead
// of shrinking further, so it never looks "tiny" just because the window is
// narrow.
const FIT_VIEW_OPTIONS = { padding: 0.15, minZoom: 0.85, maxZoom: 1.5 };

export type DiagramSelection = { kind: "node" | "edge"; id: string } | null;

export const PALETTE_DRAG_MIME = "application/x-nanoinfra-component";

interface DiagramCanvasProps {
  nodes: Node<DiagramNodeData>[];
  edges: Edge[];
  onNodesChange: (nodes: Node<DiagramNodeData>[]) => void;
  onEdgesChange: (edges: Edge[]) => void;
  onSelect: (selection: DiagramSelection) => void;
  onDropComponent: (componentTypeId: string, position: { x: number; y: number }) => void;
}

export function DiagramCanvas({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onSelect,
  onDropComponent,
}: DiagramCanvasProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const { screenToFlowPosition, fitView } = useReactFlow();

  const handleNodesChange = useCallback(
    (changes: NodeChange<Node<DiagramNodeData>>[]) => {
      // A locked node can still be selected/moved, but never removed —
      // whether by the Delete/Backspace key, a marquee delete, or any other
      // path that produces a "remove" change.
      const allowed = changes.filter((change) => {
        if (change.type !== "remove") return true;
        const target = nodes.find((n) => n.id === change.id);
        return !target?.data.locked;
      });
      onNodesChange(applyNodeChanges(allowed, nodes));
    },
    [nodes, onNodesChange],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => onEdgesChange(applyEdgeChanges(changes, edges)),
    [edges, onEdgesChange],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      const sourceType = nodes.find((n) => n.id === connection.source)?.data.componentTypeId ?? "";
      const targetType = nodes.find((n) => n.id === connection.target)?.data.componentTypeId ?? "";
      onEdgesChange(
        addEdge(
          { ...connection, label: defaultEdgeLabel(sourceType, targetType), ...EDGE_LABEL_STYLE },
          edges,
        ),
      );
    },
    [nodes, edges, onEdgesChange],
  );

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => onSelect({ kind: "node", id: node.id }),
    [onSelect],
  );

  const handleEdgeClick: EdgeMouseHandler = useCallback(
    (_event, edge) => onSelect({ kind: "edge", id: edge.id }),
    [onSelect],
  );

  const handlePaneClick = useCallback(() => onSelect(null), [onSelect]);

  const handleDragOver = useCallback((event: React.DragEvent) => {
    if (!event.dataTransfer.types.includes(PALETTE_DRAG_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      const componentTypeId = event.dataTransfer.getData(PALETTE_DRAG_MIME);
      if (!componentTypeId) return;
      event.preventDefault();
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      onDropComponent(componentTypeId, position);
    },
    [screenToFlowPosition, onDropComponent],
  );

  const handleAutoLayout = useCallback(() => {
    onNodesChange(autoLayout(nodes, edges));
    // Let the new positions commit to the DOM before re-fitting the view.
    requestAnimationFrame(() => fitView(FIT_VIEW_OPTIONS));
  }, [nodes, edges, onNodesChange, fitView]);

  const handleInit = useCallback((instance: ReactFlowInstance<Node<DiagramNodeData>, Edge>) => {
    // React Flow's declarative `fitView` prop can fire before the flex
    // layout around the canvas has settled to its final size, producing a
    // too-small initial zoom. Re-fitting one frame later fixes it.
    requestAnimationFrame(() => instance.fitView(FIT_VIEW_OPTIONS));
  }, []);

  return (
    <div ref={wrapperRef} className="h-full w-full" onDragOver={handleDragOver} onDrop={handleDrop}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={handleConnect}
        onNodeClick={handleNodeClick}
        onEdgeClick={handleEdgeClick}
        onPaneClick={handlePaneClick}
        onInit={handleInit}
        deleteKeyCode={["Backspace", "Delete"]}
        fitView
        fitViewOptions={FIT_VIEW_OPTIONS}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{ type: "smoothstep", ...EDGE_LABEL_STYLE }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} className="opacity-40" />
        <Panel position="top-left">
          <button
            type="button"
            onClick={handleAutoLayout}
            className="touch-target flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground shadow-none hover:bg-muted/70"
          >
            <LayoutGrid className="h-3.5 w-3.5" />
            Auto Layout
          </button>
        </Panel>
        <Controls
          showInteractive={false}
          className="!shadow-none [&_button]:!border-border/45 [&_button]:!bg-settings-surface"
        />
      </ReactFlow>
    </div>
  );
}
