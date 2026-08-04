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

// Walks parentId up to the outermost ancestor — the node dagre actually
// positions. A nested node has no edge of its own in the top-level graph,
// so its edges must attach to this ancestor instead, or the group they
// belong to has nothing anchoring it and dagre drops it anywhere (including
// on top of unrelated nodes).
function topLevelAncestor(id: string, byId: Map<string, Node<DiagramNodeData>>): string {
  let current = byId.get(id);
  while (current?.parentId) {
    const parent = byId.get(current.parentId);
    if (!parent) break;
    current = parent;
  }
  return current?.id ?? id;
}

/**
 * Recomputes node positions with dagre so edges (and their labels) don't
 * overlap — manual/seed positions have no collision-avoidance, so crossing
 * edges end up with their label pills stacked on top of each other.
 *
 * Only top-level nodes (no parentId) are actually re-laid-out — a node
 * nested inside a group has a position relative to that group, not the
 * canvas, so running dagre on it directly would treat that relative
 * position as absolute and scatter it. But edges through a nested endpoint
 * still count: they're re-attached to that node's top-level ancestor, so a
 * group with connected children is still anchored to the rest of the graph
 * instead of floating free (and landing wherever dagre puts disconnected
 * nodes, typically overlapping something else).
 *
 * Only the first such re-attached edge per group is kept. Dagre ranks a
 * node one below the deepest of its inbound sources — a group whose three
 * children each connect to a different, differently-ranked outside node
 * (as "Storage via Isilon" does here) would otherwise get pulled all the
 * way down to the deepest of the three, cramming it in below nodes it has
 * no real reason to be under. One anchor is enough to place the group in
 * the graph; the real per-child edges still render normally regardless.
 */
export function autoLayout(nodes: Node<DiagramNodeData>[], edges: Edge[]): Node<DiagramNodeData>[] {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 70, marginx: 40, marginy: 40 });
  graph.setDefaultEdgeLabel(() => ({}));

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const topLevelIds = new Set(nodes.filter((n) => !n.parentId).map((n) => n.id));

  for (const node of nodes) {
    if (!topLevelIds.has(node.id)) continue;
    graph.setNode(node.id, nodeDimensions(node));
  }
  const anchoredGroups = new Set<string>();
  for (const edge of edges) {
    const source = topLevelAncestor(edge.source, byId);
    const target = topLevelAncestor(edge.target, byId);
    if (source === target || !topLevelIds.has(source) || !topLevelIds.has(target)) continue;

    const sourceIsAnchor = source !== edge.source;
    const targetIsAnchor = target !== edge.target;
    if ((sourceIsAnchor && anchoredGroups.has(source)) || (targetIsAnchor && anchoredGroups.has(target))) {
      continue;
    }
    if (sourceIsAnchor) anchoredGroups.add(source);
    if (targetIsAnchor) anchoredGroups.add(target);

    graph.setEdge(source, target);
  }

  dagre.layout(graph);

  // Dagre only saw one edge per group (above), so it has no reason to place
  // a group next to the other real nodes its members connect to — it can
  // land on the far side of the row from them. Re-center each group at the
  // average x of everything it's actually connected to, sliding same-rank
  // siblings apart just enough to fit it there without overlapping. This
  // only reorders within a rank; dagre's rank/y assignment is untouched.
  const xOverrides = new Map<string, number>();
  for (const groupId of anchoredGroups) {
    const partners = new Set<string>();
    for (const edge of edges) {
      const source = topLevelAncestor(edge.source, byId);
      const target = topLevelAncestor(edge.target, byId);
      if (source === target) continue;
      if (source === groupId && topLevelIds.has(target)) partners.add(target);
      if (target === groupId && topLevelIds.has(source)) partners.add(source);
    }
    if (partners.size === 0) continue;

    const currentX = (id: string) => xOverrides.get(id) ?? graph.node(id).x;
    const idealX = [...partners].reduce((sum, id) => sum + currentX(id), 0) / partners.size;

    const rank = graph.node(groupId).rank;
    const row = graph
      .nodes()
      .filter((id) => id !== groupId && graph.node(id).rank === rank)
      .map((id) => ({ id, x: currentX(id), width: nodeDimensions(byId.get(id)!).width }))
      .sort((a, b) => a.x - b.x);
    const insertAt = row.findIndex((n) => idealX < n.x);
    row.splice(insertAt === -1 ? row.length : insertAt, 0, {
      id: groupId,
      x: idealX,
      width: nodeDimensions(byId.get(groupId)!).width,
    });

    const nodesep = graph.graph().nodesep ?? 60;
    let cursor = row[0].x - row[0].width / 2;
    for (const item of row) {
      xOverrides.set(item.id, cursor + item.width / 2);
      cursor += item.width + nodesep;
    }
  }

  return nodes.map((node) => {
    if (!topLevelIds.has(node.id)) return node;
    const position = graph.node(node.id);
    if (!position) return node;
    const { width, height } = nodeDimensions(node);
    const x = xOverrides.get(node.id) ?? position.x;
    return {
      ...node,
      position: { x: x - width / 2, y: position.y - height / 2 },
    };
  });
}
