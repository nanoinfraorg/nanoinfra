import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

import type { DiagramNodeData } from "./diagramTypes";

// Fallbacks only -- a node already on the canvas has a real `measured` size
// from its last render, which reflects its actual legend text (it can wrap
// to 2+ lines and grow wider/taller than any fixed guess), and is always
// preferred over these constants.
const NODE_WIDTH = 220;
const NODE_HEIGHT = 90;
const GROUP_DEFAULT_WIDTH = 320;
const GROUP_DEFAULT_HEIGHT = 220;
const GROUP_PADDING = 30;
// Vertical space a group's own header (label + provider + legend) occupies
// above its first child -- without this, a child placed right at the group's
// top edge visually overlaps the header text.
const GROUP_HEADER_CLEARANCE = 74;

function nodeDimensions(node: Node<DiagramNodeData>): { width: number; height: number } {
  if (node.type === "groupBox") {
    const styleWidth = typeof node.style?.width === "number" ? node.style.width : undefined;
    const styleHeight = typeof node.style?.height === "number" ? node.style.height : undefined;
    return {
      width: node.measured?.width ?? styleWidth ?? GROUP_DEFAULT_WIDTH,
      height: node.measured?.height ?? styleHeight ?? GROUP_DEFAULT_HEIGHT,
    };
  }
  return {
    width: node.measured?.width ?? NODE_WIDTH,
    height: node.measured?.height ?? NODE_HEIGHT,
  };
}

/**
 * Lays out one group's direct children with their own dagre pass, in the
 * group's local coordinate space, then reports the size the group needs to
 * actually contain them. Recurses depth-first (a nested group's own children
 * are placed, and that group sized, before its parent's pass runs) so a
 * parent sizing itself around a child group uses that child's *final* size.
 *
 * `sizeOverrides` doubles as the "already visited" set for groups.
 */
function layoutGroupChildren(
  groupId: string,
  nodes: Node<DiagramNodeData>[],
  edges: Edge[],
  byParent: Map<string | undefined, Node<DiagramNodeData>[]>,
  positionOverrides: Map<string, { x: number; y: number }>,
  sizeOverrides: Map<string, { width: number; height: number }>,
): void {
  const children = byParent.get(groupId) ?? [];
  for (const child of children) {
    if (child.type === "groupBox") {
      layoutGroupChildren(child.id, nodes, edges, byParent, positionOverrides, sizeOverrides);
    }
  }
  if (children.length === 0) return;

  const dims = (n: Node<DiagramNodeData>) => sizeOverrides.get(n.id) ?? nodeDimensions(n);

  const graph = new dagre.graphlib.Graph();
  graph.setGraph({
    rankdir: "TB",
    nodesep: 50,
    ranksep: 60,
    marginx: GROUP_PADDING,
    marginy: GROUP_HEADER_CLEARANCE,
  });
  graph.setDefaultEdgeLabel(() => ({}));

  const childIds = new Set(children.map((c) => c.id));
  for (const child of children) graph.setNode(child.id, dims(child));
  for (const edge of edges) {
    if (edge.source !== edge.target && childIds.has(edge.source) && childIds.has(edge.target)) {
      graph.setEdge(edge.source, edge.target);
    }
  }
  dagre.layout(graph);

  let maxRight = 0;
  let maxBottom = 0;
  for (const child of children) {
    const pos = graph.node(child.id);
    const { width, height } = dims(child);
    const x = pos.x - width / 2;
    const y = pos.y - height / 2;
    positionOverrides.set(child.id, { x, y });
    maxRight = Math.max(maxRight, x + width);
    maxBottom = Math.max(maxBottom, y + height);
  }
  sizeOverrides.set(groupId, {
    width: Math.max(GROUP_DEFAULT_WIDTH, maxRight + GROUP_PADDING),
    height: Math.max(GROUP_DEFAULT_HEIGHT, maxBottom + GROUP_PADDING),
  });
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

  // Every group (at any nesting depth) gets its own children laid out with a
  // dedicated dagre pass in the group's local coordinate space -- the
  // top-level pass below only ever moved groups themselves, never tidied up
  // what was inside them, so a big group stayed exactly as overlapping as
  // whatever produced its children (a hand-placed layout, or a guess made
  // before anything was ever rendered) no matter how many times Auto Layout
  // ran. Sizes computed here also feed the top-level pass, so a group is
  // sized to actually fit its (freshly re-laid-out) children.
  const byParent = new Map<string | undefined, Node<DiagramNodeData>[]>();
  for (const node of nodes) {
    const key = node.parentId;
    const siblings = byParent.get(key) ?? [];
    siblings.push(node);
    byParent.set(key, siblings);
  }
  const positionOverrides = new Map<string, { x: number; y: number }>();
  const sizeOverrides = new Map<string, { width: number; height: number }>();
  for (const node of nodes) {
    if (topLevelIds.has(node.id) && node.type === "groupBox") {
      layoutGroupChildren(node.id, nodes, edges, byParent, positionOverrides, sizeOverrides);
    }
  }

  for (const node of nodes) {
    if (!topLevelIds.has(node.id)) continue;
    graph.setNode(node.id, sizeOverrides.get(node.id) ?? nodeDimensions(node));
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

    const dims = (id: string) => sizeOverrides.get(id) ?? nodeDimensions(byId.get(id)!);
    const rank = graph.node(groupId).rank;
    const row = graph
      .nodes()
      .filter((id) => id !== groupId && graph.node(id).rank === rank)
      .map((id) => ({ id, x: currentX(id), width: dims(id).width }))
      .sort((a, b) => a.x - b.x);
    const insertAt = row.findIndex((n) => idealX < n.x);
    row.splice(insertAt === -1 ? row.length : insertAt, 0, {
      id: groupId,
      x: idealX,
      width: dims(groupId).width,
    });

    const nodesep = graph.graph().nodesep ?? 60;
    let cursor = row[0].x - row[0].width / 2;
    for (const item of row) {
      xOverrides.set(item.id, cursor + item.width / 2);
      cursor += item.width + nodesep;
    }
  }

  return nodes.map((node) => {
    if (!topLevelIds.has(node.id)) {
      // A nested node's position is relative to its own parent, which
      // `layoutGroupChildren` already computed above regardless of how deep
      // it's nested -- only the top-level dagre pass needs graph.node(). A
      // nested *group* also got its own size computed the same way.
      const nestedPosition = positionOverrides.get(node.id);
      const nestedSize = sizeOverrides.get(node.id);
      if (!nestedPosition) return node;
      return {
        ...node,
        position: nestedPosition,
        ...(nestedSize ? { style: { ...node.style, ...nestedSize } } : {}),
      };
    }
    const position = graph.node(node.id);
    if (!position) return node;
    const size = sizeOverrides.get(node.id) ?? nodeDimensions(node);
    const x = xOverrides.get(node.id) ?? position.x;
    return {
      ...node,
      position: { x: x - size.width / 2, y: position.y - size.height / 2 },
      ...(node.type === "groupBox" && sizeOverrides.has(node.id) ? { style: { ...node.style, ...size } } : {}),
    };
  });
}
