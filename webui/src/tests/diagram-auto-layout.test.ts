import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";

import { autoLayout } from "@/components/diagrams/autoLayout";
import type { DiagramNodeData } from "@/components/diagrams/diagramTypes";

function node(
  id: string,
  overrides: Partial<Node<DiagramNodeData>> = {},
): Node<DiagramNodeData> {
  return {
    id,
    position: { x: 0, y: 0 },
    type: "component",
    data: { label: id, componentTypeId: "application", providerId: "custom-docker-app", config: {} },
    ...overrides,
  };
}

function group(id: string, overrides: Partial<Node<DiagramNodeData>> = {}): Node<DiagramNodeData> {
  return node(id, { type: "groupBox", data: { label: id, componentTypeId: "__group__", providerId: "generic", config: {} }, ...overrides });
}

function edge(id: string, source: string, target: string): Edge {
  return { id, source, target };
}

function footprint(n: Node<DiagramNodeData>): { width: number; height: number } {
  if (n.type === "groupBox") {
    const style = n.style as { width?: number; height?: number } | undefined;
    return { width: n.measured?.width ?? style?.width ?? 320, height: n.measured?.height ?? style?.height ?? 220 };
  }
  return { width: n.measured?.width ?? 220, height: n.measured?.height ?? 90 };
}

function boxesOverlap(a: Node<DiagramNodeData>, b: Node<DiagramNodeData>): boolean {
  const fa = footprint(a);
  const fb = footprint(b);
  return (
    a.position.x < b.position.x + fb.width
    && b.position.x < a.position.x + fa.width
    && a.position.y < b.position.y + fb.height
    && b.position.y < a.position.y + fa.height
  );
}

function assertNoSiblingOverlaps(nodes: Node<DiagramNodeData>[]): void {
  const byParent = new Map<string | undefined, Node<DiagramNodeData>[]>();
  for (const n of nodes) {
    const siblings = byParent.get(n.parentId) ?? [];
    siblings.push(n);
    byParent.set(n.parentId, siblings);
  }
  for (const siblings of byParent.values()) {
    for (let i = 0; i < siblings.length; i += 1) {
      for (let j = i + 1; j < siblings.length; j += 1) {
        expect(boxesOverlap(siblings[i], siblings[j])).toBe(false);
      }
    }
  }
}

describe("autoLayout", () => {
  it("repositions top-level nodes without overlap", () => {
    const nodes = [node("a"), node("b"), node("c")];
    const edges = [edge("e1", "a", "b"), edge("e2", "b", "c")];

    const result = autoLayout(nodes, edges);

    assertNoSiblingOverlaps(result);
    // a top-level node's y-rank should follow edge direction (a above b above c)
    const byId = new Map(result.map((n) => [n.id, n]));
    expect(byId.get("a")!.position.y).toBeLessThan(byId.get("b")!.position.y);
    expect(byId.get("b")!.position.y).toBeLessThan(byId.get("c")!.position.y);
  });

  it("also lays out a group's own children, not just top-level nodes", () => {
    // Before the fix, children kept whatever position they arrived with --
    // seed them deliberately overlapping to prove auto layout actually moves them.
    const nodes = [
      group("g", { style: { width: 320, height: 220 } }),
      node("child-a", { parentId: "g", position: { x: 0, y: 0 } }),
      node("child-b", { parentId: "g", position: { x: 0, y: 0 } }),
      node("child-c", { parentId: "g", position: { x: 0, y: 0 } }),
    ];
    const edges = [edge("e1", "child-a", "child-b"), edge("e2", "child-b", "child-c")];

    const result = autoLayout(nodes, edges);

    assertNoSiblingOverlaps(result);
    const byId = new Map(result.map((n) => [n.id, n]));
    // No longer all stacked at the same (0, 0) they started at.
    const positions = new Set(["child-a", "child-b", "child-c"].map((id) => {
      const p = byId.get(id)!.position;
      return `${p.x},${p.y}`;
    }));
    expect(positions.size).toBe(3);
  });

  it("sizes a group to actually contain its (re-laid-out) children", () => {
    const nodes = [
      group("g", { style: { width: 100, height: 100 } }),
      node("child-a", { parentId: "g" }),
      node("child-b", { parentId: "g" }),
    ];
    const edges: Edge[] = [];

    const result = autoLayout(nodes, edges);

    const g = result.find((n) => n.id === "g")!;
    const style = g.style as { width: number; height: number };
    const children = result.filter((n) => n.parentId === "g");
    const maxRight = Math.max(...children.map((c) => c.position.x + footprint(c).width));
    const maxBottom = Math.max(...children.map((c) => c.position.y + footprint(c).height));
    expect(style.width).toBeGreaterThanOrEqual(maxRight);
    expect(style.height).toBeGreaterThanOrEqual(maxBottom);
  });

  it("does not let a wide top-level group overlap a plain sibling", () => {
    // Regression guard: the top-level pass must size a group from its own
    // (freshly computed) children, not whatever default/stale size it had,
    // or a wide group can still collide with whatever landed next to it.
    const nodes = [
      node("solo"),
      group("wide", { style: { width: 900, height: 200 } }),
      node("wide-child-a", { parentId: "wide" }),
      node("wide-child-b", { parentId: "wide" }),
      node("wide-child-c", { parentId: "wide" }),
      node("wide-child-d", { parentId: "wide" }),
    ];
    const edges = [edge("e1", "solo", "wide")];

    const result = autoLayout(nodes, edges);

    assertNoSiblingOverlaps(result);
  });

  it("lays out a nested group-within-a-group from the inside out", () => {
    const nodes = [
      group("outer", { style: { width: 320, height: 220 } }),
      group("inner", { parentId: "outer", style: { width: 200, height: 150 } }),
      node("leaf-a", { parentId: "inner" }),
      node("leaf-b", { parentId: "inner" }),
    ];
    const edges: Edge[] = [];

    const result = autoLayout(nodes, edges);

    assertNoSiblingOverlaps(result);
    const inner = result.find((n) => n.id === "inner")!;
    const innerStyle = inner.style as { width: number; height: number };
    const leaves = result.filter((n) => n.parentId === "inner");
    const maxRight = Math.max(...leaves.map((c) => c.position.x + footprint(c).width));
    expect(innerStyle.width).toBeGreaterThanOrEqual(maxRight);
  });
});
