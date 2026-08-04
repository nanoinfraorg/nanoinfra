import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

import type { DiagramNodeData } from "./diagramTypes";

const NODE_WIDTH = 200;
const NODE_HEIGHT = 60;
const GROUP_DEFAULT_WIDTH = 320;
const GROUP_DEFAULT_HEIGHT = 220;

function nodeDimensions(node: Node<DiagramNodeData>): { width: number; height: number } {
  if (node.type !== "groupBox") return { width: NODE_WIDTH, height: NODE_HEIGHT };
  const styleWidth = typeof node.style?.width === "number" ? node.style.width : undefined;
  const styleHeight = typeof node.style?.height === "number" ? node.style.height : undefined;
  return {
    width: node.measured?.width ?? styleWidth ?? GROUP_DEFAULT_WIDTH,
    height: node.measured?.height ?? styleHeight ?? GROUP_DEFAULT_HEIGHT,
  };
}

/**
 * Recomputes node positions with dagre so edges (and their labels) don't
 * overlap — manual/seed positions have no collision-avoidance, so crossing
 * edges end up with their label pills stacked on top of each other.
 *
 * Only top-level nodes (no parentId) are re-laid-out. A node nested inside a
 * group has a position relative to that group, not the canvas — running
 * dagre on it directly would treat that relative position as absolute and
 * scatter it. In practice, nested nodes are rarely connected to each other
 * by edges, so dagre (with nothing to separate them) stacks them all at the
 * same spot, making all but one look like they vanished.
 */
export function autoLayout(nodes: Node<DiagramNodeData>[], edges: Edge[]): Node<DiagramNodeData>[] {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 70, marginx: 40, marginy: 40 });
  graph.setDefaultEdgeLabel(() => ({}));

  const topLevelIds = new Set(nodes.filter((n) => !n.parentId).map((n) => n.id));

  for (const node of nodes) {
    if (!topLevelIds.has(node.id)) continue;
    graph.setNode(node.id, nodeDimensions(node));
  }
  for (const edge of edges) {
    if (!topLevelIds.has(edge.source) || !topLevelIds.has(edge.target)) continue;
    graph.setEdge(edge.source, edge.target);
  }

  dagre.layout(graph);

  return nodes.map((node) => {
    if (!topLevelIds.has(node.id)) return node;
    const position = graph.node(node.id);
    if (!position) return node;
    const { width, height } = nodeDimensions(node);
    return {
      ...node,
      position: { x: position.x - width / 2, y: position.y - height / 2 },
    };
  });
}
