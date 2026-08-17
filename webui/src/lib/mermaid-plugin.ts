import type { MermaidConfig } from "mermaid";
import type { DiagramPlugin } from "streamdown";

import type { Theme } from "@/hooks/useTheme";

interface MermaidModule {
  initialize: (config: MermaidConfig) => void;
  render: (id: string, source: string) => Promise<{ svg: string }>;
}

/** Settings this plugin owns outright, because they are what makes the rendered SVG safe.
 *
 * Streamdown injects the SVG that `render` returns with `dangerouslySetInnerHTML`, so mermaid's
 * own sanitiser is the only boundary on that path. `securityLevel: "strict"` is therefore merged
 * *over* whatever a caller passes rather than under it -- a diagram file in the workspace is
 * untrusted input, written by the agent or pulled by git.
 */
const ENFORCED: MermaidConfig = {
  securityLevel: "strict",
  startOnLoad: false,
};

let modulePromise: Promise<MermaidModule> | null = null;
let appliedConfig: string | null = null;

/** The config for a theme. Separate from the plugin so a caller can pass it as a stable value. */
export function mermaidConfigFor(theme: Theme): MermaidConfig {
  return {
    ...ENFORCED,
    // Mermaid's light theme is called "default"; there is no "light".
    theme: theme === "dark" ? "dark" : "default",
  };
}

async function loadMermaid(config?: MermaidConfig): Promise<MermaidModule> {
  // Dynamically imported so mermaid arrives with the first diagram someone opens rather than
  // with the app shell. Streamdown's own mermaid renderer is already an excluded lazy chunk
  // (see `webuiManualChunk` in vite.config.ts), so this keeps both halves off the main path.
  modulePromise ??= import("mermaid").then((module) => module.default as MermaidModule);
  const mermaid = await modulePromise;

  const resolved: MermaidConfig = { ...config, ...ENFORCED };
  const serialized = JSON.stringify(resolved);
  if (serialized !== appliedConfig) {
    // `initialize` is global and idempotent, so it runs once per distinct config -- which is
    // what re-themes an already-loaded mermaid when the app flips to dark.
    mermaid.initialize(resolved);
    appliedConfig = serialized;
  }
  return mermaid;
}

/**
 * The diagram plugin Streamdown waits for.
 *
 * Streamdown ships the mermaid *block* -- pan/zoom, fullscreen, copy, download as mmd/svg/png --
 * but renders a fence only when the app supplies the mermaid instance, which is what keeps
 * mermaid out of the bundle for everyone who never opens a diagram. `getMermaid` is synchronous
 * in that contract, so this returns a thin instance whose `render` awaits the import.
 */
export const mermaidDiagramPlugin: DiagramPlugin = {
  name: "mermaid",
  type: "diagram",
  language: "mermaid",
  getMermaid: (config?: MermaidConfig) => ({
    initialize: (next: MermaidConfig) => {
      void loadMermaid(next);
    },
    render: async (id: string, source: string) => {
      const mermaid = await loadMermaid(config);
      return mermaid.render(id, source);
    },
  }),
};

export function __resetMermaidForTests(): void {
  modulePromise = null;
  appliedConfig = null;
}
