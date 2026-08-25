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

export type FilePreviewMode = "raw" | "preview";

/** Whether each kind of file opens rendered or as source, remembered per extension.
 *
 * Deliberately its own storage key rather than a field of ``LocalPreferences``: Settings holds
 * the whole preferences object in state captured at mount and writes it back whenever any
 * setting changes, so a map updated from the preview panel while Settings was open would be
 * silently overwritten by the next unrelated toggle.
 */
export const FILE_PREVIEW_MODES_STORAGE_KEY = "nanoinfra-webui.file-preview-modes";
export const FILE_PREVIEW_MODES_CHANGED_EVENT = "nanoinfra-webui.file-preview-modes-changed";

export const FILE_PREVIEW_WRAP_STORAGE_KEY = "nanoinfra-webui.file-preview-wrap";
export const FILE_PREVIEW_WRAP_CHANGED_EVENT = "nanoinfra-webui.file-preview-wrap-changed";

/** Whether long lines soft-wrap in the raw view.
 *
 * One flag rather than one per extension, unlike the raw/preview mode: wrapping is a
 * reading habit, and an operator who wants it does not want it only for `.md`.
 */
export function readFilePreviewWrap(): boolean {
  try {
    return window.localStorage.getItem(FILE_PREVIEW_WRAP_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeFilePreviewWrap(wrap: boolean): void {
  try {
    window.localStorage.setItem(FILE_PREVIEW_WRAP_STORAGE_KEY, wrap ? "1" : "0");
  } catch {
    // Browser-only preferences should never block the panel.
  }
  window.dispatchEvent(new CustomEvent<boolean>(FILE_PREVIEW_WRAP_CHANGED_EVENT, { detail: wrap }));
}

export function normalizeFilePreviewMode(value: unknown): FilePreviewMode | null {
  return value === "raw" || value === "preview" ? value : null;
}

export function readFilePreviewModes(): Record<string, FilePreviewMode> {
  try {
    const raw = window.localStorage.getItem(FILE_PREVIEW_MODES_STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const modes: Record<string, FilePreviewMode> = {};
    for (const [extension, value] of Object.entries(parsed as Record<string, unknown>)) {
      const mode = normalizeFilePreviewMode(value);
      if (mode) modes[extension] = mode;
    }
    return modes;
  } catch {
    return {};
  }
}

export function writeFilePreviewMode(extension: string, mode: FilePreviewMode): void {
  const modes = { ...readFilePreviewModes(), [extension]: mode };
  try {
    window.localStorage.setItem(FILE_PREVIEW_MODES_STORAGE_KEY, JSON.stringify(modes));
  } catch {
    // Browser-only preferences should never block the panel.
  }
  window.dispatchEvent(new CustomEvent<Record<string, FilePreviewMode>>(
    FILE_PREVIEW_MODES_CHANGED_EVENT,
    { detail: modes },
  ));
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
