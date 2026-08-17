import { describe, expect, it, vi } from "vitest";

/**
 * Guards the module graph, not a style preference.
 *
 * Importing Streamdown statically from the preview panel put the markdown vendor chunk into the
 * eager graph, and rollup's `manualChunks` then emitted `syntax-highlight` and `markdown-vendor`
 * importing *each other*. That builds cleanly -- `tsc` and `vite build` both pass -- and throws
 * `can't access lexical declaration 'Q' before initialization` in the browser on first paint. So
 * the cheap test is not "does it build" but "is the heavy renderer still behind a dynamic
 * import", which is the property that keeps the two chunks acyclic.
 */
const evaluated = vi.hoisted(() => ({ streamdown: false, mermaid: false }));

vi.mock("streamdown", () => {
  evaluated.streamdown = true;
  return { Streamdown: () => null };
});

vi.mock("mermaid", () => {
  evaluated.mermaid = true;
  return { default: { initialize: () => {}, render: async () => ({ svg: "" }) } };
});

describe("the file preview panel's module graph", () => {
  it("does not load Streamdown or mermaid until a diagram is actually opened", async () => {
    await import("@/components/FilePreviewPanel");

    expect(evaluated.streamdown).toBe(false);
    expect(evaluated.mermaid).toBe(false);
  });

  it("loads them when the diagram renderer is reached", async () => {
    await import("@/components/preview/MermaidPreview");

    expect(evaluated.streamdown).toBe(true);
  });
});
