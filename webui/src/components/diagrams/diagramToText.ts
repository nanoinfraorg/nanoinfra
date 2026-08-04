import type { Diagram, DiagramNode } from "./diagramTypes";
import { findComponentType, findProvider, type ComponentType } from "./componentCatalog";

function slug(id: string): string {
  return id.replace(/[^a-zA-Z0-9_]/g, "_");
}

function nodeTitle(node: DiagramNode, componentTypes: ComponentType[]): string {
  const provider = findProvider(componentTypes, node.data.componentTypeId, node.data.providerId);
  const suffix = provider ? `: ${provider.label}` : "";
  return `${node.data.label}${suffix}`;
}

function yamlQuote(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/** One-way, read-only text projection of the visual diagram (Mermaid-flowchart-flavored). */
export function diagramToText(diagram: Diagram, componentTypes: ComponentType[]): string {
  const lines: string[] = [];
  // Mermaid's YAML frontmatter is how a diagram carries its own title —
  // this is what turns "just some flowchart syntax" into a named, saveable
  // diagram when the text is pasted into any standard Mermaid renderer.
  if (diagram.name) {
    lines.push("---", `title: ${yamlQuote(diagram.name)}`, "---");
  }
  lines.push("flowchart TD");

  const childrenByParent = new Map<string, DiagramNode[]>();
  const roots: DiagramNode[] = [];
  for (const node of diagram.nodes) {
    if (node.parentId) {
      const siblings = childrenByParent.get(node.parentId) ?? [];
      siblings.push(node);
      childrenByParent.set(node.parentId, siblings);
    } else {
      roots.push(node);
    }
  }

  // Nested groups render as `subgraph ... end` blocks so a group's children
  // stay visually contained under it, matching the canvas. Plain components
  // are flat `id["label"]` declarations, indented to reflect nesting depth.
  function emitNode(node: DiagramNode, depth: number) {
    const indent = "    ".repeat(depth + 1);
    const isGroup = findComponentType(componentTypes, node.data.componentTypeId)?.isGroup || node.type === "groupBox";
    const children = childrenByParent.get(node.id) ?? [];
    if (isGroup) {
      lines.push(`${indent}subgraph ${slug(node.id)}["${nodeTitle(node, componentTypes)}"]`);
      for (const child of children) {
        emitNode(child, depth + 1);
      }
      lines.push(`${indent}end`);
    } else {
      lines.push(`${indent}${slug(node.id)}["${nodeTitle(node, componentTypes)}"]`);
      for (const child of children) {
        emitNode(child, depth + 1);
      }
    }
  }

  for (const node of roots) {
    emitNode(node, 0);
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
