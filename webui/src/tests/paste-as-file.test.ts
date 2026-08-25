import { describe, expect, it } from "vitest";

import {
  PASTE_AS_FILE_BYTES,
  PASTE_AS_FILE_LINES,
  largeTextFromPaste,
} from "@/hooks/useClipboardAndDrop";

function paste(text: string): React.ClipboardEvent {
  return {
    clipboardData: { getData: (type: string) => (type === "text/plain" ? text : ""), items: [] },
  } as unknown as React.ClipboardEvent;
}

describe("largeTextFromPaste", () => {
  it("leaves an ordinary paste alone", () => {
    expect(largeTextFromPaste(paste("a short note"))).toBeNull();
    expect(largeTextFromPaste(paste("two\nlines"))).toBeNull();
  });

  it("takes a paste with too many lines", () => {
    // A pasted script is a file someone is handing over, not a sentence.
    const script = Array.from({ length: PASTE_AS_FILE_LINES + 1 }, (_, i) => `echo ${i}`).join("\n");

    expect(largeTextFromPaste(paste(script))).toBe(script);
  });

  it("takes a paste that is one very wide line", () => {
    // Either threshold is enough: a wide single line is as unusable in a composer
    // as a thousand narrow ones.
    const wide = "x".repeat(PASTE_AS_FILE_BYTES + 1);

    expect(largeTextFromPaste(paste(wide))).toBe(wide);
  });

  it("counts bytes rather than characters", () => {
    // Just under the limit in characters, over it in UTF-8.
    const text = "é".repeat(Math.ceil(PASTE_AS_FILE_BYTES / 2) + 1);

    expect(text.length).toBeLessThan(PASTE_AS_FILE_BYTES);
    expect(largeTextFromPaste(paste(text))).toBe(text);
  });

  it("takes anything past the gateway's own text budget, whatever the thresholds say", () => {
    // Otherwise the composer accepts a message the gateway will refuse to carry.
    const text = "still small";

    expect(largeTextFromPaste(paste(text), { budgetBytes: 4 })).toBe(text);
    expect(largeTextFromPaste(paste(text), { budgetBytes: 4096 })).toBeNull();
  });

  it("ignores a paste with no text", () => {
    expect(largeTextFromPaste(paste(""))).toBeNull();
    expect(
      largeTextFromPaste({ clipboardData: null } as unknown as React.ClipboardEvent),
    ).toBeNull();
  });
});
