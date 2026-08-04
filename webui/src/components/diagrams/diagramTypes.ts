export interface DiagramNodeData {
  label: string;
  componentTypeId: string;
  providerId: string;
  config: Record<string, string>;
  locked?: boolean;
  [key: string]: unknown;
}

export interface DiagramNode {
  id: string;
  position: { x: number; y: number };
  data: DiagramNodeData;
  // Optional — most nodes are plain components with no parent. Only set
  // these to nest a node inside a group (component or another group).
  type?: "component" | "groupBox";
  parentId?: string;
  style?: { width: number; height: number };
}

export interface DiagramEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  // Optional — most edges use each node's default top/bottom handle. Set
  // these (e.g. "left"/"right") to route a side-by-side connection straight
  // across instead of forcing a detour through whatever sits between the
  // two nodes' default handles.
  sourceHandle?: string;
  targetHandle?: string;
}

export interface Diagram {
  id: string;
  name: string;
  targets: string[];
  nodes: DiagramNode[];
  edges: DiagramEdge[];
}

/** The lightweight listing shape shown in the Diagrams gallery. */
export interface DiagramSummary {
  id: string;
  name: string;
  targets: string[];
  nodeCount: number;
  updatedAt: string;
}

/** A fresh, unsaved diagram — the id is assigned server-side on first save. */
export function createBlankDiagram(): Diagram {
  return { id: "", name: "Untitled diagram", targets: [], nodes: [], edges: [] };
}
