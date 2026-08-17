import type { FilePreviewMode } from "@/lib/local-preferences";

/** The visual forms a workspace file can be shown in, beyond its source. */
export type PreviewRenderer = "mermaid" | "svg" | "markdown";

const RENDERER_BY_EXTENSION: Record<string, PreviewRenderer> = {
  md: "markdown",
  mdx: "markdown",
  mermaid: "mermaid",
  mmd: "mermaid",
  svg: "svg",
};

/** Which mode a renderable file opens in.
 *
 * `.mmd` and `.svg` open rendered because their source has no other viewer. Markdown opens as
 * source: the panel is usually reached from a transcript row about a file that just changed, and
 * the literal text is what the operator went looking for.
 */
const DEFAULT_MODE: Record<PreviewRenderer, FilePreviewMode> = {
  markdown: "raw",
  mermaid: "preview",
  svg: "preview",
};

/** The preference key for a path: its lower-cased extension, or "" when it has none. */
export function previewExtension(displayPath: string): string {
  const name = displayPath.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "";
  return name.slice(dot + 1).toLowerCase();
}

export function rendererForPath(displayPath: string): PreviewRenderer | null {
  return RENDERER_BY_EXTENSION[previewExtension(displayPath)] ?? null;
}

export function defaultModeForRenderer(renderer: PreviewRenderer): FilePreviewMode {
  return DEFAULT_MODE[renderer];
}
