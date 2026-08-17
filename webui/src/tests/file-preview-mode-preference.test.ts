import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LOCAL_PREFS,
  FILE_PREVIEW_MODES_CHANGED_EVENT,
  FILE_PREVIEW_MODES_STORAGE_KEY,
  LOCAL_PREFS_STORAGE_KEY,
  normalizeFilePreviewMode,
  readFilePreviewModes,
  readLocalPreferences,
  writeFilePreviewMode,
  writeLocalPreferences,
} from "@/lib/local-preferences";

describe("the file preview mode preference", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("round-trips a choice per extension", () => {
    writeFilePreviewMode("mmd", "raw");
    writeFilePreviewMode("svg", "preview");

    expect(readFilePreviewModes()).toEqual({ mmd: "raw", svg: "preview" });
  });

  it("keeps the extensions it does not know about", () => {
    writeFilePreviewMode("mmd", "raw");
    writeFilePreviewMode("md", "preview");
    writeFilePreviewMode("mmd", "preview");

    expect(readFilePreviewModes()).toEqual({ mmd: "preview", md: "preview" });
  });

  it("drops values that are not a mode instead of trusting them", () => {
    window.localStorage.setItem(
      FILE_PREVIEW_MODES_STORAGE_KEY,
      JSON.stringify({ mmd: "sideways", svg: "preview", md: 7, mermaid: null }),
    );

    expect(readFilePreviewModes()).toEqual({ svg: "preview" });
    expect(normalizeFilePreviewMode("sideways")).toBeNull();
    expect(normalizeFilePreviewMode("raw")).toBe("raw");
  });

  it("survives storage that is not an object at all", () => {
    for (const stored of ["[]", "\"raw\"", "null", "{oops", ""]) {
      window.localStorage.setItem(FILE_PREVIEW_MODES_STORAGE_KEY, stored);
      expect(readFilePreviewModes()).toEqual({});
    }
  });

  it("announces the change in-page, since a storage event never reaches this tab", () => {
    const listener = vi.fn();
    window.addEventListener(FILE_PREVIEW_MODES_CHANGED_EVENT, listener);

    writeFilePreviewMode("mmd", "raw");

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(FILE_PREVIEW_MODES_CHANGED_EVENT, listener);
  });

  it("is not a field of LocalPreferences, so Settings cannot overwrite it", () => {
    // The reason for the separate key: Settings holds the whole preferences object in state from
    // mount and writes it back on any change, without resyncing. If these modes lived in that
    // object, choosing Raw for a diagram and then toggling any unrelated setting would silently
    // undo it. Asserted rather than trusted to a comment.
    writeFilePreviewMode("mmd", "raw");

    writeLocalPreferences({ ...DEFAULT_LOCAL_PREFS, density: "compact" });

    expect(readFilePreviewModes()).toEqual({ mmd: "raw" });
    expect(readLocalPreferences().density).toBe("compact");
    expect(window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY)).not.toContain("mmd");
  });
});
