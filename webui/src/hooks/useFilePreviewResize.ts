import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

export const FILE_PREVIEW_DEFAULT_WIDTH = 544;
export const FILE_PREVIEW_MIN_WIDTH = 360;
export const FILE_PREVIEW_MAX_WIDTH = 860;
/** What must be left of the surface behind the panel, so it never resizes to nothing. */
export const FILE_PREVIEW_MIN_MAIN_WIDTH = 420;

export function clampFilePreviewWidth(width: number, maxWidth: number): number {
  return Math.min(Math.max(width, FILE_PREVIEW_MIN_WIDTH), maxWidth);
}

export function maxFilePreviewWidth(containerWidth: number): number {
  return Math.max(
    FILE_PREVIEW_MIN_WIDTH,
    Math.min(FILE_PREVIEW_MAX_WIDTH, containerWidth - FILE_PREVIEW_MIN_MAIN_WIDTH),
  );
}

/**
 * The drag-to-resize behaviour of `FilePreviewPanel`, shared by every surface that
 * opens one.
 *
 * Extracted from `ThreadShell` when the Workspaces explorer grew a preview pane of
 * its own: this is one component's interaction, not a per-surface decision, and two
 * copies of a pointer-capture dance is two places for it to drift.
 *
 * While dragging, the width is written straight onto the panel's CSS custom
 * properties inside a rAF and committed to React state only on pointer-up. Setting
 * state per pointermove would re-render the previewed document — syntax
 * highlighting and all — on every frame of the drag.
 */
export function useFilePreviewResize(
  containerRef: RefObject<HTMLElement | null>,
  open: boolean,
): {
  width: number;
  onResizeStart: (event: ReactPointerEvent<HTMLButtonElement>) => void;
} {
  const [width, setWidth] = useState(FILE_PREVIEW_DEFAULT_WIDTH);
  const widthRef = useRef(FILE_PREVIEW_DEFAULT_WIDTH);

  useEffect(() => {
    widthRef.current = width;
  }, [width]);

  const onResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      const panel = event.currentTarget.closest<HTMLElement>("[data-file-preview-panel]");
      const containerRect = containerRef.current?.getBoundingClientRect();
      const rightEdge = containerRect?.right ?? window.innerWidth;
      const maxWidth = maxFilePreviewWidth(containerRect?.width ?? window.innerWidth);
      const originalBodyCursor = document.body.style.cursor;
      const originalBodyUserSelect = document.body.style.userSelect;
      const originalPanelTransition = panel?.style.transition ?? "";
      let nextWidth = widthRef.current;
      let frame: number | null = null;

      document.body.style.cursor = "col-resize";
      // Without this, dragging across the document selects text under the pointer.
      document.body.style.userSelect = "none";
      // The panel animates its own width when it opens; keeping that transition on
      // during a drag makes the edge lag behind the pointer.
      if (panel) panel.style.transition = "none";

      const applyWidth = (clientX: number) => {
        nextWidth = clampFilePreviewWidth(rightEdge - clientX, maxWidth);
        widthRef.current = nextWidth;
        if (frame !== null) return;
        frame = window.requestAnimationFrame(() => {
          frame = null;
          panel?.style.setProperty("--file-preview-width", `${nextWidth}px`);
          panel?.style.setProperty("--file-preview-slot-width", `${nextWidth}px`);
        });
      };
      const handlePointerMove = (moveEvent: PointerEvent) => {
        moveEvent.preventDefault();
        applyWidth(moveEvent.clientX);
      };
      const handlePointerUp = () => {
        if (frame !== null) {
          window.cancelAnimationFrame(frame);
          frame = null;
        }
        panel?.style.setProperty("--file-preview-width", `${nextWidth}px`);
        panel?.style.setProperty("--file-preview-slot-width", `${nextWidth}px`);
        if (panel) panel.style.transition = originalPanelTransition;
        setWidth(nextWidth);
        document.body.style.cursor = originalBodyCursor;
        document.body.style.userSelect = originalBodyUserSelect;
        window.removeEventListener("pointermove", handlePointerMove);
        window.removeEventListener("pointerup", handlePointerUp);
        window.removeEventListener("pointercancel", handlePointerUp);
      };

      applyWidth(event.clientX);
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp);
      // pointercancel too: a drag interrupted by the OS or a touch gesture must still
      // restore the cursor and the text selection it turned off.
      window.addEventListener("pointercancel", handlePointerUp);
    },
    [containerRef],
  );

  // A window narrowed while the panel is open would otherwise leave the surface
  // behind it under its minimum, or the panel wider than the window.
  useEffect(() => {
    if (!open) return;
    const clampToContainer = () => {
      const containerWidth = containerRef.current?.getBoundingClientRect().width
        ?? window.innerWidth;
      const next = clampFilePreviewWidth(widthRef.current, maxFilePreviewWidth(containerWidth));
      widthRef.current = next;
      setWidth(next);
    };
    clampToContainer();
    window.addEventListener("resize", clampToContainer);
    return () => {
      window.removeEventListener("resize", clampToContainer);
    };
  }, [containerRef, open]);

  return { width, onResizeStart };
}
