import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { AlertCircle, ChevronRight, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { splitFilePath } from "@/components/FileReferenceChip";
import { MarkdownText } from "@/components/MarkdownText";
import { InertSvg } from "@/components/preview/InertSvg";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { useFilePreviewMode } from "@/hooks/useFilePreviewMode";
import { ApiError, fetchFilePreview } from "@/lib/api";
import { rendererForPath } from "@/lib/file-preview-render";
import type { FilePreviewPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

interface FilePreviewPanelProps {
  /** Ignored when `loadPreview` is given: the workspace explorer has no session. */
  sessionKey?: string;
  path: string;
  /**
   * How to fetch the preview. Defaults to the session-scoped route
   * (`/api/sessions/<key>/file-preview`); the Workspaces explorer passes the
   * workspace-scoped one instead. Parameterising the one fetch call is what lets
   * both surfaces share this panel's renderers (markdown, mermaid, SVG) rather
   * than growing a second viewer beside it.
   */
  loadPreview?: (path: string) => Promise<FilePreviewPayload>;
  token: string;
  desktopWidth?: number;
  /**
   * Take the whole surface instead of a fixed width.
   *
   * For a caller that hid what sat beside the panel: the width props describe a
   * pane sharing the row, and there is nothing to share it with. Set together with
   * omitting `onResizeStart`, since a drag has no edge to move.
   */
  fill?: boolean;
  isClosing?: boolean;
  onResizeStart?: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onClose: () => void;
}

/** Loaded on demand, and that is load-bearing rather than a nicety.
 *
 * `MermaidPreview` imports Streamdown, which until now was only reachable through
 * `MarkdownText`'s own `lazy()`. Importing it statically from this panel put the whole markdown
 * vendor chunk into the eager graph, and rollup's `manualChunks` then emitted
 * `syntax-highlight` and `markdown-vendor` importing each other -- a cycle that builds
 * cleanly and throws `can't access lexical declaration ... before initialization` in the
 * browser. Keeping the import dynamic keeps the two chunks acyclic, which is the same reason
 * `vite.config.ts` pins Refractor's HAST helper next to Refractor.
 */
const MermaidPreview = lazy(async () => ({
  default: (await import("@/components/preview/MermaidPreview")).MermaidPreview,
}));

type PreviewState =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; payload: FilePreviewPayload };

export function FilePreviewPanel({
  sessionKey,
  path,
  loadPreview,
  token,
  desktopWidth = 544,
  fill = false,
  isClosing = false,
  onResizeStart,
  onClose,
}: FilePreviewPanelProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const [entered, setEntered] = useState(false);
  const tokenRef = useRef(token);
  tokenRef.current = token;
  // Held in a ref for the same reason the token is: the fetch below must re-run when
  // the *path* changes and at no other time. A caller that builds this inline —
  // `loadPreview={(p) => fetch(p)}` — hands us a new function on every one of its
  // renders, and with that in the effect's dependencies an unrelated poll elsewhere
  // in the app (the approvals refresh, every 5s) reloaded the open file each time.
  const loadPreviewRef = useRef(loadPreview);
  loadPreviewRef.current = loadPreview;

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setEntered(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    const loader = loadPreviewRef.current;
    const pending = loader
      ? loader(path)
      : fetchFilePreview(tokenRef.current, sessionKey ?? "", path);
    pending
      .then((payload) => {
        if (!cancelled) setState({ status: "ready", payload });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", error });
      });
    return () => {
      cancelled = true;
    };
  }, [path, sessionKey]);

  const displayPath = state.status === "ready" ? state.payload.display_path : path;
  const previewPath = state.status === "ready" ? state.payload.path : displayPath;
  const normalizedPreviewPath = previewPath.replace(/\\/g, "/");
  const hasRootPrefix = normalizedPreviewPath.startsWith("/");
  const { name } = splitFilePath(displayPath);
  const fileName = name || displayPath;
  const pathParts = useMemo(
    () => normalizedPreviewPath.split("/").filter(Boolean),
    [normalizedPreviewPath],
  );
  const directoryParts = useMemo(
    () => (pathParts.length > 1 ? pathParts.slice(0, -1) : []),
    [pathParts],
  );
  const breadcrumbParts = useMemo(
    () => (directoryParts.length > 0 ? [...directoryParts, fileName] : [fileName]),
    [directoryParts, fileName],
  );
  const compactBreadcrumbParts = useMemo(
    () => (breadcrumbParts.length > 3 ? breadcrumbParts.slice(-3) : breadcrumbParts),
    [breadcrumbParts],
  );
  const hasCompactPrefix = breadcrumbParts.length > compactBreadcrumbParts.length;
  const breadcrumbTitle = `${hasRootPrefix ? "/" : ""}${[
    ...directoryParts,
    fileName,
  ].join("/")}`;
  const renderer = rendererForPath(displayPath);
  const [mode, setMode] = useFilePreviewMode(displayPath, renderer);
  const showSource = useCallback(() => setMode("raw"), [setMode]);
  // A truncated payload is not a smaller document: half an `<svg>` is not a smaller picture, and
  // half a flowchart is a parse error. Rendering either would report a large file as a broken one.
  const truncated = state.status === "ready" && state.payload.truncated;
  const effectiveMode = truncated ? "raw" : mode;
  const canRender = renderer !== null && state.status === "ready";

  const errorMessage = state.status === "error"
    ? (state.error instanceof ApiError
      ? (state.error.status === 404 && /API route not found/i.test(state.error.message)
        ? t("filePreview.routeMissing", {
          defaultValue: "File preview needs the latest gateway. Restart nanoinfra gateway and try again.",
        })
        : state.error.message)
      : t("filePreview.failed", { defaultValue: "Could not preview this file." }))
    : null;

  return (
    <aside
      aria-label={t("filePreview.aria", { defaultValue: "File preview" })}
      style={{
        "--file-preview-width": `${desktopWidth}px`,
        "--file-preview-slot-width": !entered || isClosing ? "0px" : `${desktopWidth}px`,
      } as CSSProperties}
      className={cn(
        "absolute inset-y-0 right-0 z-30 w-[min(100vw,var(--file-preview-slot-width))] overflow-hidden",
        "transition-[width] duration-300 ease-out will-change-[width]",
        fill
          ? "md:relative md:z-auto md:w-full md:min-w-0 md:flex-1"
          : "md:relative md:z-auto md:w-[var(--file-preview-slot-width)] md:min-w-0 md:shrink-0",
        isClosing && "pointer-events-none",
      )}
      data-testid="file-preview-panel"
      data-file-preview-panel
    >
      <div
        className={cn(
          "absolute inset-y-0 right-0 flex w-[min(100vw,var(--file-preview-width))] flex-col overflow-hidden pb-[env(safe-area-inset-bottom)] md:pb-0",
          fill ? "md:w-full" : "md:w-[var(--file-preview-width)]",
          "border-l border-border/70 bg-background shadow-2xl md:shadow-none",
          "transition-[opacity,transform] duration-300 ease-out will-change-transform",
          !entered || isClosing ? "translate-x-full opacity-0" : "translate-x-0 opacity-100",
          "motion-reduce:translate-x-0",
        )}
      >
        {onResizeStart ? (
          <button
            type="button"
            aria-label={t("filePreview.resize", { defaultValue: "Resize file preview" })}
            className={cn(
              "group absolute inset-y-0 left-0 z-20 hidden w-3 -translate-x-1/2 cursor-col-resize touch-none md:flex",
              "items-stretch justify-center focus-visible:outline-none",
            )}
            onPointerDown={onResizeStart}
          >
            <span
              aria-hidden
              className={cn(
                "h-full w-px bg-foreground/25 opacity-0 transition-opacity",
                "group-hover:opacity-100 group-focus-visible:bg-ring group-focus-visible:opacity-100",
              )}
            />
          </button>
        ) : null}
        <div className="flex min-h-0 flex-1 flex-col">
          <div
            className="flex h-11 shrink-0 items-center gap-2 border-b border-border/60 px-3"
            title={previewPath}
          >
            <nav
              aria-label={t("filePreview.breadcrumb", { defaultValue: "File path" })}
              className="flex min-w-0 flex-1 items-center overflow-hidden text-sm leading-5"
              title={breadcrumbTitle}
              data-testid="file-preview-breadcrumb"
            >
              {hasCompactPrefix ? (
                <>
                  <span className="shrink-0 text-muted-foreground/55">...</span>
                  <ChevronRight
                    className="mx-1 h-3.5 w-3.5 shrink-0 text-muted-foreground/35"
                    aria-hidden
                  />
                </>
              ) : hasRootPrefix ? (
                <>
                  <span className="shrink-0 text-muted-foreground/55">/</span>
                  <ChevronRight
                    className="mx-1 h-3.5 w-3.5 shrink-0 text-muted-foreground/35"
                    aria-hidden
                  />
                </>
              ) : null}
              {compactBreadcrumbParts.map((part, index) => {
                const isLast = index === compactBreadcrumbParts.length - 1;
                return (
                  <span
                    key={`${part}-${index}`}
                    className="flex min-w-0 items-center overflow-hidden"
                  >
                    {index > 0 ? (
                      <ChevronRight
                        className="mx-1 h-3.5 w-3.5 shrink-0 text-muted-foreground/35"
                        aria-hidden
                      />
                    ) : null}
                    <span
                      className={cn(
                        "min-w-0 truncate rounded-[4px] px-1 py-0.5",
                        isLast
                          ? "font-medium text-foreground"
                          : "max-w-[26vw] shrink text-muted-foreground/78",
                      )}
                      data-testid={isLast ? "file-preview-title" : undefined}
                    >
                      {part}
                    </span>
                  </span>
                );
              })}
            </nav>
            {canRender ? (
              <SegmentedControl
                size="sm"
                className="shrink-0"
                value={effectiveMode}
                disabled={truncated}
                ariaLabel={t("filePreview.renderMode", { defaultValue: "How to show this file" })}
                title={truncated
                  ? t("filePreview.truncatedNoPreview", {
                    defaultValue: "Large files are shown as source only.",
                  })
                  : undefined}
                options={[
                  { value: "raw", label: t("filePreview.raw", { defaultValue: "Raw" }) },
                  { value: "preview", label: t("filePreview.rendered", { defaultValue: "Preview" }) },
                ]}
                onChange={(next) => setMode(next === "preview" ? "preview" : "raw")}
              />
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className={cn(
                "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
                "text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
              title={t("filePreview.close", { defaultValue: "Close file preview" })}
              aria-label={t("filePreview.close", { defaultValue: "Close file preview" })}
              data-testid="file-preview-close"
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-auto">
            {state.status === "loading" ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                {t("filePreview.loading", { defaultValue: "Loading preview..." })}
              </div>
            ) : state.status === "error" ? (
              <div className="flex h-full items-center justify-center px-8 text-center text-sm text-muted-foreground">
                <div className="max-w-sm">
                  <AlertCircle
                    className="mx-auto mb-3 h-5 w-5 text-muted-foreground/70"
                    aria-hidden
                  />
                  <p>{errorMessage}</p>
                </div>
              </div>
            ) : (
              <div className="min-h-full">
                {state.payload.truncated ? (
                  <div className="mx-4 mt-3 rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-200">
                    {t("filePreview.truncated", {
                      defaultValue: "Preview is truncated because this file is large.",
                    })}
                  </div>
                ) : null}
                {effectiveMode === "preview" && renderer === "mermaid" ? (
                  <Suspense
                    fallback={
                      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                        {t("filePreview.rendering", { defaultValue: "Rendering diagram..." })}
                      </div>
                    }
                  >
                    <MermaidPreview source={state.payload.content} onShowSource={showSource} />
                  </Suspense>
                ) : effectiveMode === "preview" && renderer === "svg" ? (
                  <InertSvg markup={state.payload.content} label={fileName} />
                ) : effectiveMode === "preview" && renderer === "markdown" ? (
                  <MarkdownText className="px-4 py-3">{state.payload.content}</MarkdownText>
                ) : (
                  <CodeBlock
                    language={state.payload.language}
                    code={state.payload.content}
                    chrome="none"
                    highlight
                    showLineNumbers
                    wrapLongLines={false}
                    className="min-h-full"
                  />
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
