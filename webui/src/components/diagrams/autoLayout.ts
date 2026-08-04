import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

import type { DiagramNodeData } from "./diagramTypes";

const NODE_WIDTH = 200;
const NODE_HEIGHT = 60;

/**
 * Recomputes node positions with dagre so edges (and their labels) don't
 * overlap — manual/seed positions have no collision-avoidance, so crossing
 * edges end up with their label pills stacked on top of each other.
 */
export function autoLayout(nodes: Node<DiagramNodeData>[], edges: Edge[]): Node<DiagramNodeData>[] {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 70, marginx: 40, marginy: 40 });
  graph.setDefaultEdgeLabel(() => ({}));

  for (const node of nodes) {
    graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    graph.setEdge(edge.source, edge.target);
  }

  dagre.layout(graph);

  return nodes.map((node) => {
    const position = graph.node(node.id);
    if (!position) return node;
    return {
      ...node,
      position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 },
    };
  });
}
