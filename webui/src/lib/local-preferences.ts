export type LocalDensity = "comfortable" | "compact";
export type LocalActivityMode = "auto" | "expanded";
export type FileEditDisplayMode = "summary" | "diff" | "collapsed_diff";
export type ThreadWidth = "standard" | "wide" | "full";

export interface LocalPreferences {
  density: LocalDensity;
  activityMode: LocalActivityMode;
  codeWrap: boolean;
  brandLogos: boolean;
  fileEditDisplayMode: FileEditDisplayMode;
  threadWidth: ThreadWidth;
}

/** Reading measure, and how far a table or a code block may reach past it.
 *
 * Two numbers, because prose and a table want different widths: prose is easier to read at a
 * bounded measure, and a table that has to scroll sideways is harder to read at any measure. So
 * the wider value is only for content that is not prose.
 */
export const THREAD_WIDTHS: Record<ThreadWidth, { measure: string; bleed: string }> = {
  standard: { measure: "49.5rem", bleed: "64rem" },
  wide: { measure: "58rem", bleed: "76rem" },
  full: { measure: "76rem", bleed: "90rem" },
};

export const LOCAL_PREFS_STORAGE_KEY = "nanoinfra-webui.settings-preferences";
export const LOCAL_PREFS_CHANGED_EVENT = "nanoinfra-webui.local-preferences-changed";

export const DEFAULT_LOCAL_PREFS: LocalPreferences = {
  density: "comfortable",
  activityMode: "auto",
  codeWrap: true,
  brandLogos: false,
  fileEditDisplayMode: "summary",
  threadWidth: "wide",
};

export function normalizeFileEditDisplayMode(value: unknown): FileEditDisplayMode {
  return value === "diff" || value === "collapsed_diff" ? value : "summary";
}

export function normalizeThreadWidth(value: unknown): ThreadWidth {
  return value === "standard" || value === "full" ? value : "wide";
}

export function readLocalPreferences(): LocalPreferences {
  try {
    const raw = window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY);
    if (!raw) return DEFAULT_LOCAL_PREFS;
    const parsed = JSON.parse(raw) as Partial<LocalPreferences>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      activityMode: parsed.activityMode === "expanded" ? "expanded" : "auto",
      codeWrap: parsed.codeWrap !== false,
      brandLogos: parsed.brandLogos === true,
      fileEditDisplayMode: normalizeFileEditDisplayMode(parsed.fileEditDisplayMode),
      threadWidth: normalizeThreadWidth(parsed.threadWidth),
    };
  } catch {
    return DEFAULT_LOCAL_PREFS;
  }
}

export function writeLocalPreferences(preferences: LocalPreferences): void {
  try {
    window.localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // Browser-only preferences should never block settings.
  }
  window.dispatchEvent(new CustomEvent<LocalPreferences>(
    LOCAL_PREFS_CHANGED_EVENT,
    { detail: preferences },
  ));
}
