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
  type OnNodeDrag,
  type ReactFlowInstance,
} from "@xyflow/react";
import { LayoutGrid } from "lucide-react";
import "@xyflow/react/dist/base.css";

import { autoLayout } from "./autoLayout";
import { DiagramNode } from "./DiagramNode";
import { GroupNode } from "./GroupNode";
import { defaultEdgeLabel } from "./edgeDefaults";
import type { DiagramNodeData } from "./diagramTypes";

// Named "groupBox", not "group" — React Flow's base.css ships default
// border/color styling keyed off a literal node.type of "group"
// (.react-flow__node-group), which would silently fight our own styling.
const NODE_TYPES = { component: DiagramNode, groupBox: GroupNode };

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

const GROUP_DEFAULT_WIDTH = 320;
const GROUP_DEFAULT_HEIGHT = 220;

function absolutePositionOf(
  node: Node<DiagramNodeData>,
  byId: Map<string, Node<DiagramNodeData>>,
): { x: number; y: number } {
  let x = node.position.x;
  let y = node.position.y;
  let current = node;
  while (current.parentId) {
    const parent = byId.get(current.parentId);
    if (!parent) break;
    x += parent.position.x;
    y += parent.position.y;
    current = parent;
  }
  return { x, y };
}

function groupDimensions(node: Node<DiagramNodeData>): { width: number; height: number } {
  const styleWidth = typeof node.style?.width === "number" ? node.style.width : undefined;
  const styleHeight = typeof node.style?.height === "number" ? node.style.height : undefined;
  return {
    width: node.measured?.width ?? styleWidth ?? GROUP_DEFAULT_WIDTH,
    height: node.measured?.height ?? styleHeight ?? GROUP_DEFAULT_HEIGHT,
  };
}

// Finds the innermost group whose absolute bounds contain the point — used
// when a component is dropped from the palette (not dragged from elsewhere
// on the canvas), since it doesn't exist as a node yet and so can't be
// checked via getIntersectingNodes.
function findContainingGroup(
  point: { x: number; y: number },
  nodes: Node<DiagramNodeData>[],
  byId: Map<string, Node<DiagramNodeData>>,
): Node<DiagramNodeData> | undefined {
  let best: { node: Node<DiagramNodeData>; depth: number } | undefined;
  for (const node of nodes) {
    if (node.type !== "groupBox") continue;
    const abs = absolutePositionOf(node, byId);
    const { width, height } = groupDimensions(node);
    if (point.x < abs.x || point.x > abs.x + width || point.y < abs.y || point.y > abs.y + height) continue;
    let depth = 0;
    let current = node;
    while (current.parentId) {
      const parent = byId.get(current.parentId);
      if (!parent) break;
      depth += 1;
      current = parent;
    }
    if (!best || depth > best.depth) best = { node, depth };
  }
  return best?.node;
}

interface DiagramCanvasProps {
  nodes: Node<DiagramNodeData>[];
  edges: Edge[];
  onNodesChange: (nodes: Node<DiagramNodeData>[]) => void;
  onEdgesChange: (edges: Edge[]) => void;
  onSelect: (selection: DiagramSelection) => void;
  onDropComponent: (
    componentTypeId: string,
    position: { x: number; y: number },
    parentId?: string,
  ) => void;
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
  const { screenToFlowPosition, fitView, getIntersectingNodes } = useReactFlow<Node<DiagramNodeData>, Edge>();

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

  // Dropping a component — or a group — onto (or out of) another group
  // reparents it, walking the full ancestor chain so this is correct at any
  // nesting depth (a single level up is not enough once groups can contain
  // groups).
  const handleNodeDragStop: OnNodeDrag<Node<DiagramNodeData>> = useCallback(
    (_event, draggedNode) => {
      const byId = new Map(nodes.map((n) => [n.id, n]));

      const descendantIds = new Set<string>();
      const collectDescendants = (id: string) => {
        for (const n of nodes) {
          if (n.parentId === id && !descendantIds.has(n.id)) {
            descendantIds.add(n.id);
            collectDescendants(n.id);
          }
        }
      };
      collectDescendants(draggedNode.id);

      const absolutePosition = absolutePositionOf(draggedNode, byId);

      // A group can't be dropped into itself or into one of its own
      // descendants — that would create a cycle.
      const newParent = getIntersectingNodes(draggedNode).find(
        (n) => n.type === "groupBox" && n.id !== draggedNode.id && !descendantIds.has(n.id),
      );

      if (newParent && newParent.id !== draggedNode.parentId) {
        const newParentAbsolute = absolutePositionOf(newParent, byId);
        onNodesChange(
          nodes.map((n) =>
            n.id === draggedNode.id
              ? {
                  ...n,
                  parentId: newParent.id,
                  extent: "parent" as const,
                  position: {
                    x: absolutePosition.x - newParentAbsolute.x,
                    y: absolutePosition.y - newParentAbsolute.y,
                  },
                }
              : n,
          ),
        );
      } else if (!newParent && draggedNode.parentId) {
        onNodesChange(
          nodes.map((n) =>
            n.id === draggedNode.id
              ? { ...n, parentId: undefined, extent: undefined, position: absolutePosition }
              : n,
          ),
        );
      }
    },
    [nodes, onNodesChange, getIntersectingNodes],
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
      const absolutePosition = screenToFlowPosition({ x: event.clientX, y: event.clientY });

      const byId = new Map(nodes.map((n) => [n.id, n]));
      const parent = findContainingGroup(absolutePosition, nodes, byId);

      if (!parent) {
        onDropComponent(componentTypeId, absolutePosition);
        return;
      }
      const parentAbsolute = absolutePositionOf(parent, byId);
      onDropComponent(
        componentTypeId,
        { x: absolutePosition.x - parentAbsolute.x, y: absolutePosition.y - parentAbsolute.y },
        parent.id,
      );
    },
    [nodes, screenToFlowPosition, onDropComponent],
  );

  const handleAutoLayout = useCallback(() => {
    onNodesChange(autoLayout(nodes, edges));
    // A manually-picked side handle (left/right) is a fix for one specific,
    // hand-placed geometry — dagre's TB layout assumes the default top/
    // bottom flow, so a handle override left over from before auto-layout
    // ran can end up routing straight through whatever dagre placed nearby.
    // Auto Layout owns the whole geometry now, so it resets the handles too.
    if (edges.some((e) => e.sourceHandle || e.targetHandle)) {
      onEdgesChange(edges.map((e) => ({ ...e, sourceHandle: undefined, targetHandle: undefined })));
    }
    // Let the new positions commit to the DOM before re-fitting the view.
    requestAnimationFrame(() => fitView(FIT_VIEW_OPTIONS));
  }, [nodes, edges, onNodesChange, onEdgesChange, fitView]);

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
        onNodeDragStop={handleNodeDragStop}
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
