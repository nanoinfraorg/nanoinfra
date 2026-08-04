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
