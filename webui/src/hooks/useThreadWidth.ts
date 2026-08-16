import { useEffect, useState } from "react";

import {
  LOCAL_PREFS_CHANGED_EVENT,
  normalizeThreadWidth,
  readLocalPreferences,
  THREAD_WIDTHS,
  type LocalPreferences,
  type ThreadWidth,
} from "@/lib/local-preferences";

export interface ThreadWidthStyle {
  /** The reading measure for prose. */
  measure: string;
  /** How far a table, a code block or a diagram may reach past the measure. */
  bleed: string;
  choice: ThreadWidth;
}

/** The thread width chosen in this browser, kept live while Settings changes it.
 *
 * The same three listeners as the other local preferences: ``storage`` for another tab, ``focus``
 * for a change made while this one was in the background, and the in-page event for the tab that
 * made the change, which never receives ``storage``.
 */
export function useThreadWidth(): ThreadWidthStyle {
  const [choice, setChoice] = useState<ThreadWidth>(() => readLocalPreferences().threadWidth);

  useEffect(() => {
    const refresh = () => setChoice(readLocalPreferences().threadWidth);
    const refreshFromLocalPreferenceEvent = (event: Event) => {
      const detail = (event as CustomEvent<Partial<LocalPreferences> | undefined>).detail;
      setChoice(
        detail ? normalizeThreadWidth(detail.threadWidth) : readLocalPreferences().threadWidth,
      );
    };
    window.addEventListener("storage", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener(LOCAL_PREFS_CHANGED_EVENT, refreshFromLocalPreferenceEvent);
    return () => {
      window.removeEventListener("storage", refresh);
      window.removeEventListener("focus", refresh);
      window.removeEventListener(LOCAL_PREFS_CHANGED_EVENT, refreshFromLocalPreferenceEvent);
    };
  }, []);

  return { ...THREAD_WIDTHS[choice], choice };
}
