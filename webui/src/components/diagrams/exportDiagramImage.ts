import { toPng, toSvg } from "html-to-image";
import { getNodesBounds, getViewportForBounds, type ReactFlowInstance } from "@xyflow/react";

// getViewportForBounds's padding is a *fraction* of width/height (like
// fitView's own padding option), not pixels -- passing a pixel value here
// once made it read as "4800% padding," clamping zoom to its floor and
// rendering the whole diagram as a speck in the middle of a huge canvas.
const EXPORT_PADDING_FRACTION = 0.08;
const MIN_EXPORT_SIZE = 200;

function slugify(name: string): string {
  const slug = name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "diagram";
}

/**
 * Exports the *actual rendered canvas* (icons, category-color borders,
 * legends, group nesting) rather than regenerating an image from the
 * Mermaid text -- that would produce a generic flowchart and lose all of
 * that. Follows the pattern from React Flow's own "Download Image"
 * example: render `.react-flow__viewport` through html-to-image with a
 * temporary transform that fits every node, not just whatever's currently
 * visible on screen. html-to-image renders a cloned copy for capture, so
 * this never touches the live, on-screen viewport transform.
 */
export async function exportDiagramImage(
  reactFlowInstance: ReactFlowInstance,
  diagramName: string,
  format: "png" | "svg",
): Promise<void> {
  const nodes = reactFlowInstance.getNodes();
  if (nodes.length === 0) return;

  const viewportEl = document.querySelector<HTMLElement>(".react-flow__viewport");
  if (!viewportEl) return;

  const bounds = getNodesBounds(nodes);
  const width = Math.max(bounds.width, MIN_EXPORT_SIZE);
  const height = Math.max(bounds.height, MIN_EXPORT_SIZE);
  const viewport = getViewportForBounds(bounds, width, height, 0.1, 2, EXPORT_PADDING_FRACTION);

  const backgroundHsl = getComputedStyle(document.documentElement).getPropertyValue("--background").trim();
  const backgroundColor = backgroundHsl ? `hsl(${backgroundHsl})` : "#0b0b0d";

  const capture = format === "png" ? toPng : toSvg;
  const dataUrl = await capture(viewportEl, {
    backgroundColor,
    width,
    height,
    pixelRatio: format === "png" ? 2 : 1,
    style: {
      width: `${width}px`,
      height: `${height}px`,
      transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
    },
  });

  const link = document.createElement("a");
  link.download = `${slugify(diagramName)}.${format}`;
  link.href = dataUrl;
  link.click();
}
