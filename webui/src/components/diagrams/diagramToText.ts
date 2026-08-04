import type { Diagram } from "./diagramTypes";
import { findComponentType, findProvider } from "./componentCatalog";

function slug(id: string): string {
  return id.replace(/[^a-zA-Z0-9_]/g, "_");
}

/** One-way, read-only text projection of the visual diagram (Mermaid-flowchart-flavored). */
export function diagramToText(diagram: Diagram): string {
  const lines: string[] = ["flowchart TD"];
  for (const node of diagram.nodes) {
    const type = findComponentType(node.data.componentTypeId);
    const provider = findProvider(node.data.componentTypeId, node.data.providerId);
    const suffix = provider ? `: ${provider.label}` : "";
    lines.push(`    ${slug(node.id)}["${node.data.label}${suffix}"]`);
    void type;
  }
  for (const edge of diagram.edges) {
    const label = edge.label ? `|${edge.label}|` : "";
    lines.push(`    ${slug(edge.source)} -->${label} ${slug(edge.target)}`);
  }
  if (diagram.targets.length > 0) {
    lines.push("");
    lines.push(`%% targets: ${diagram.targets.join(", ")}`);
  }
  return lines.join("\n");
}
