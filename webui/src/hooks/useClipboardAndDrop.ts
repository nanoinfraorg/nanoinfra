import { useCallback, useRef, useState } from "react";

import { acceptedAttachmentKind } from "@/hooks/useAttachedImages";

/** Extract supported attachment ``File``s from a paste / drop event.
 *
 * Deliberate behaviour:
 *   - Only items whose ``kind === "file"`` and match the Composer whitelist
 *     are returned; HTML fragments are ignored (defending against remote URL
 *     fetch + XSS surfaces).
 *   - Plain text pasted alongside attachments is *not* consumed by this helper,
 *     so the caller can still let the textarea receive it naturally.
 */
export function extractImageFilesFromPaste(
  event: ClipboardEvent | React.ClipboardEvent,
): File[] {
  const clipboard = (event as ClipboardEvent).clipboardData
    ?? (event as React.ClipboardEvent).clipboardData;
  if (!clipboard) return [];
  const files: File[] = [];
  for (const item of Array.from(clipboard.items)) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file && acceptedAttachmentKind(file)) files.push(file);
  }
  return files;
}

/** Bytes of pasted text past which it becomes an attachment instead of message text. */
export const PASTE_AS_FILE_BYTES = 4 * 1024;

/** Lines of pasted text past which it becomes an attachment. */
export const PASTE_AS_FILE_LINES = 60;

/**
 * The pasted text that should become a file rather than message text, or ``null``.
 *
 * A paste this large is a file someone is handing over, not a sentence they are
 * writing: it buries the composer, and past the gateway's own text limit it cannot
 * be sent at all. Either threshold is enough — a wide single line is as unusable as
 * a thousand narrow ones.
 *
 * *budgetBytes* is the message-text limit the gateway advertises. A paste over it
 * has to become an attachment for the message to be sendable at all, whatever the
 * thresholds say.
 */
export function largeTextFromPaste(
  event: ClipboardEvent | React.ClipboardEvent,
  options?: { maxBytes?: number; maxLines?: number; budgetBytes?: number },
): string | null {
  const clipboard = (event as ClipboardEvent).clipboardData
    ?? (event as React.ClipboardEvent).clipboardData;
  if (!clipboard) return null;
  const text = clipboard.getData("text/plain");
  if (!text) return null;
  const bytes = new TextEncoder().encode(text).length;
  const lines = text.split("\n").length;
  const maxBytes = options?.maxBytes ?? PASTE_AS_FILE_BYTES;
  const maxLines = options?.maxLines ?? PASTE_AS_FILE_LINES;
  const budget = options?.budgetBytes;
  if (bytes > maxBytes || lines > maxLines || (budget !== undefined && bytes > budget)) {
    return text;
  }
  return null;
}

/** Extract dropped attachment files, mirroring ``extractImageFilesFromPaste``. */
export function extractImageFilesFromDrop(
  event: DragEvent | React.DragEvent,
): File[] {
  const dt = (event as DragEvent).dataTransfer
    ?? (event as React.DragEvent).dataTransfer;
  if (!dt) return [];
  const files: File[] = [];
  for (const item of Array.from(dt.files)) {
    if (acceptedAttachmentKind(item)) files.push(item);
  }
  return files;
}

export interface UseClipboardAndDropApi {
  /** Whether a drag is currently hovering the drop zone (toggle dragover UI). */
  isDragging: boolean;
  onPaste: (
    event: React.ClipboardEvent,
  ) => void;
  onDragEnter: (event: React.DragEvent) => void;
  onDragOver: (event: React.DragEvent) => void;
  onDragLeave: (event: React.DragEvent) => void;
  onDrop: (event: React.DragEvent) => void;
}

/** Wire paste + drag-and-drop to a callback.
 *
 * The hook owns ``isDragging`` state and the refcount that keeps it accurate
 * across nested ``dragenter`` / ``dragleave`` events (a known DOM gotcha: the
 * text cursor inside a textarea fires ``dragleave`` on entry, flicking the
 * highlight off otherwise). */
export function useClipboardAndDrop(
  onImageFiles: (files: File[]) => void,
  /** Called instead of pasting, when the text is large enough to be a file. */
  onLargeText?: (text: string) => void,
): UseClipboardAndDropApi {
  const [isDragging, setIsDragging] = useState(false);
  const dragDepth = useRef(0);

  const onPaste = useCallback(
    (event: React.ClipboardEvent) => {
      const files = extractImageFilesFromPaste(event);
      if (files.length > 0) {
        // Consume only when an attachment is actually present; an ordinary
        // plain-text paste still reaches the textarea unmolested.
        event.preventDefault();
        onImageFiles(files);
        return;
      }
      if (onLargeText) {
        const text = largeTextFromPaste(event);
        if (text !== null) {
          event.preventDefault();
          onLargeText(text);
        }
      }
    },
    [onImageFiles, onLargeText],
  );

  const onDragEnter = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    dragDepth.current += 1;
    setIsDragging(true);
  }, []);

  const onDragOver = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const onDragLeave = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setIsDragging(false);
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      dragDepth.current = 0;
      setIsDragging(false);
      const files = extractImageFilesFromDrop(event);
      if (files.length === 0) return;
      event.preventDefault();
      onImageFiles(files);
    },
    [onImageFiles],
  );

  return { isDragging, onPaste, onDragEnter, onDragOver, onDragLeave, onDrop };
}
