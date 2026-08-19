import { describe, expect, it } from "vitest";

import { diagramFingerprint } from "@/components/diagrams/diagramFingerprint";
import { resolveIncomingDiagramChange } from "@/components/diagrams/diagramLiveUpdate";
import type { Diagram } from "@/components/diagrams/diagramTypes";

function diagram(overrides: Partial<Diagram> = {}): Diagram {
  return {
    id: "a".repeat(32),
    name: "Web app",
    targets: ["prod-web-01"],
    nodes: [
      {
        id: "web",
        position: { x: 10, y: 20 },
        type: "component",
        data: { label: "Web", componentTypeId: "web_server", providerId: "nginx", config: { port: "80" } },
      },
    ],
    edges: [{ id: "web-db", source: "web", target: "db", label: "connects_to" }],
    ...overrides,
  };
}

describe("diagramFingerprint", () => {
  it("ignores node and edge order, and config key order", () => {
    const a = diagram({
      nodes: [
        {
          id: "db",
          position: { x: 0, y: 0 },
          type: "component",
          data: { label: "DB", componentTypeId: "database", providerId: "postgres", config: { a: "1", b: "2" } },
        },
        ...diagram().nodes,
      ],
    });
    const b = diagram({
      nodes: [
        ...diagram().nodes,
        {
          id: "db",
          position: { x: 0, y: 0 },
          type: "component",
          data: { label: "DB", componentTypeId: "database", providerId: "postgres", config: { b: "2", a: "1" } },
        },
      ],
    });

    expect(diagramFingerprint(a)).toBe(diagramFingerprint(b));
  });

  it("treats the defaults the canvas fills in on load as no change", () => {
    // `toFlowNodes`/`toFlowEdges` in DiagramsView.tsx substitute type
    // "component" and the bottom/top handle pair for anything a saved document
    // left out. Without matching that here, every diagram saved before those
    // fields existed would read as "changed on the server" the moment it opened.
    const saved = diagram({
      nodes: [{ id: "web", position: { x: 10, y: 20 }, data: diagram().nodes[0].data }],
      edges: [{ id: "web-db", source: "web", target: "db", label: "connects_to" }],
    });
    const afterCanvasLoad = diagram({
      edges: [
        {
          id: "web-db",
          source: "web",
          target: "db",
          label: "connects_to",
          sourceHandle: "bottom",
          targetHandle: "top",
        },
      ],
    });

    expect(diagramFingerprint(saved)).toBe(diagramFingerprint(afterCanvasLoad));
  });

  it("ignores the diagram id, so a first save is not a content change", () => {
    expect(diagramFingerprint(diagram({ id: "" }))).toBe(diagramFingerprint(diagram()));
  });

  it("notices a moved node, a rename, a new edge and a config edit", () => {
    const base = diagramFingerprint(diagram());
    const moved = diagram();
    moved.nodes = [{ ...moved.nodes[0], position: { x: 11, y: 20 } }];
    const reconfigured = diagram();
    reconfigured.nodes = [
      { ...reconfigured.nodes[0], data: { ...reconfigured.nodes[0].data, config: { port: "8080" } } },
    ];

    expect(diagramFingerprint(moved)).not.toBe(base);
    expect(diagramFingerprint(diagram({ name: "Renamed" }))).not.toBe(base);
    expect(diagramFingerprint(diagram({ targets: [] }))).not.toBe(base);
    expect(diagramFingerprint(reconfigured)).not.toBe(base);
    expect(
      diagramFingerprint(
        diagram({ edges: [...diagram().edges, { id: "db-web", source: "db", target: "web", label: "" }] }),
      ),
    ).not.toBe(base);
  });

  it("does not call a sub-thousandth-of-a-pixel drag a change", () => {
    const jittered = diagram();
    jittered.nodes = [{ ...jittered.nodes[0], position: { x: 10.00001, y: 20 } }];

    expect(diagramFingerprint(jittered)).toBe(diagramFingerprint(diagram()));
  });
});

describe("resolveIncomingDiagramChange", () => {
  const onScreen = "canvas";

  it("ignores a change that matches what is already on screen", () => {
    // The editor's own save comes back as a `diagram_updated` frame like any
    // other write, whether or not the baseline had caught up yet.
    expect(
      resolveIncomingDiagramChange({ fresh: onScreen, onScreen, baseline: onScreen }),
    ).toBe("ignore");
    expect(
      resolveIncomingDiagramChange({ fresh: onScreen, onScreen, baseline: "older" }),
    ).toBe("ignore");
  });

  it("applies a real change when the canvas has nothing unsaved", () => {
    expect(
      resolveIncomingDiagramChange({ fresh: "server", onScreen, baseline: onScreen }),
    ).toBe("apply");
  });

  it("asks when both sides moved", () => {
    expect(
      resolveIncomingDiagramChange({ fresh: "server", onScreen, baseline: "loaded" }),
    ).toBe("ask");
  });

  it("reports a deletion, and says whether unsaved work would be lost", () => {
    expect(resolveIncomingDiagramChange({ fresh: null, onScreen, baseline: onScreen })).toBe("gone");
    expect(resolveIncomingDiagramChange({ fresh: null, onScreen, baseline: "loaded" })).toBe(
      "gone-with-edits",
    );
  });
});
