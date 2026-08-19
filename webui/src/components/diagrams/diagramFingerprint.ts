import type { Diagram, DiagramEdge, DiagramNode } from "./diagramTypes";

/**
 * Rounding applied to canvas coordinates before comparing.
 *
 * A drag produces fractional pixels, and the same value round-trips through
 * JSON and Python floats, so this is not about precision loss — it is about
 * refusing to call a sub-thousandth-of-a-pixel difference a change worth
 * telling the operator about.
 */
function round(value: number): number {
  return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : 0;
}

function canonicalNode(node: DiagramNode): unknown {
  const config = node.data.config ?? {};
  return [
    node.id,
    // `toFlowNodes` in DiagramsView.tsx turns an absent type into "component",
    // so the canvas and a freshly-loaded document must agree on that default
    // here too — otherwise every diagram saved before `type` existed would
    // read as "changed on disk" the moment it is opened.
    node.type ?? "component",
    node.parentId ?? null,
    round(node.position?.x ?? 0),
    round(node.position?.y ?? 0),
    node.style ? [round(node.style.width), round(node.style.height)] : null,
    node.data.label ?? "",
    node.data.componentTypeId ?? "",
    node.data.providerId ?? "",
    node.data.locked === true,
    Object.keys(config)
      .sort()
      .map((key) => [key, String(config[key] ?? "")]),
  ];
}

function canonicalEdge(edge: DiagramEdge): unknown {
  return [
    edge.id,
    edge.source,
    edge.target,
    String(edge.label ?? ""),
    // Same defaults `toFlowEdges` substitutes when a saved edge carries no
    // handles. Comparing the raw values would report a difference for every
    // edge saved before Top/Bottom had real ids.
    edge.sourceHandle ?? "bottom",
    edge.targetHandle ?? "top",
  ];
}

/**
 * A stable string standing for a diagram's *content*, ignoring key order,
 * node/edge order, and the defaults the canvas fills in on load.
 *
 * Used to answer two questions the live-update path asks, without needing the
 * store's revision counter on the wire: "does the editor hold unsaved changes?"
 * (fingerprint vs the last one loaded or saved) and "is this incoming
 * ``diagram_updated`` frame just the echo of my own save?" (fingerprint of the
 * refetched document vs the one on screen).
 */
export function diagramFingerprint(diagram: Diagram): string {
  return JSON.stringify([
    diagram.name ?? "",
    [...(diagram.targets ?? [])],
    [...(diagram.nodes ?? [])].sort((a, b) => a.id.localeCompare(b.id)).map(canonicalNode),
    [...(diagram.edges ?? [])].sort((a, b) => a.id.localeCompare(b.id)).map(canonicalEdge),
  ]);
}
