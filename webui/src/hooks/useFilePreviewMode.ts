import { useCallback, useEffect, useState } from "react";

import {
  defaultModeForRenderer,
  previewExtension,
  type PreviewRenderer,
} from "@/lib/file-preview-render";
import {
  FILE_PREVIEW_MODES_CHANGED_EVENT,
  readFilePreviewModes,
  writeFilePreviewMode,
  type FilePreviewMode,
} from "@/lib/local-preferences";

/** The raw/preview choice for a path, remembered per extension in this browser.
 *
 * The same three listeners as the other local preferences: ``storage`` for another tab, ``focus``
 * for a change made while this one was in the background, and the in-page event for the tab that
 * made the change, which never receives ``storage``.
 */
export function useFilePreviewMode(
  displayPath: string,
  renderer: PreviewRenderer | null,
): [FilePreviewMode, (mode: FilePreviewMode) => void] {
  const extension = previewExtension(displayPath);
  const fallback: FilePreviewMode = renderer ? defaultModeForRenderer(renderer) : "raw";

  const [stored, setStored] = useState<FilePreviewMode | undefined>(
    () => readFilePreviewModes()[extension],
  );

  useEffect(() => {
    const refresh = () => setStored(readFilePreviewModes()[extension]);
    refresh();
    window.addEventListener("storage", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener(FILE_PREVIEW_MODES_CHANGED_EVENT, refresh);
    return () => {
      window.removeEventListener("storage", refresh);
      window.removeEventListener("focus", refresh);
      window.removeEventListener(FILE_PREVIEW_MODES_CHANGED_EVENT, refresh);
    };
  }, [extension]);

  const choose = useCallback(
    (mode: FilePreviewMode) => {
      setStored(mode);
      if (extension) writeFilePreviewMode(extension, mode);
    },
    [extension],
  );

  return [renderer ? (stored ?? fallback) : "raw", choose];
}
