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
}

export interface Diagram {
  id: string;
  name: string;
  targets: string[];
  nodes: DiagramNode[];
  edges: DiagramEdge[];
}
