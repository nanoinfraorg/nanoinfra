import { useCallback, useEffect, useState } from "react";

import {
  FILE_PREVIEW_WRAP_CHANGED_EVENT,
  readFilePreviewWrap,
  writeFilePreviewWrap,
} from "@/lib/local-preferences";

/** Whether the raw view soft-wraps long lines, remembered in this browser.
 *
 * The same three listeners as the other local preferences: ``storage`` for another
 * tab, ``focus`` for a change made while this one was in the background, and the
 * in-page event for the tab that made the change, which never receives ``storage``.
 */
export function useFilePreviewWrap(): [boolean, (wrap: boolean) => void] {
  const [wrap, setWrap] = useState(readFilePreviewWrap);

  useEffect(() => {
    const refresh = () => setWrap(readFilePreviewWrap());
    refresh();
    window.addEventListener("storage", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener(FILE_PREVIEW_WRAP_CHANGED_EVENT, refresh);
    return () => {
      window.removeEventListener("storage", refresh);
      window.removeEventListener("focus", refresh);
      window.removeEventListener(FILE_PREVIEW_WRAP_CHANGED_EVENT, refresh);
    };
  }, []);

  const update = useCallback((next: boolean) => {
    setWrap(next);
    writeFilePreviewWrap(next);
  }, []);

  return [wrap, update];
}
