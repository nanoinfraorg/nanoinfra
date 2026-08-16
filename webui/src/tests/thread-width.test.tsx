import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ThreadViewport } from "@/components/thread/ThreadViewport";
import {
  DEFAULT_LOCAL_PREFS,
  LOCAL_PREFS_STORAGE_KEY,
  normalizeThreadWidth,
  readLocalPreferences,
  THREAD_WIDTHS,
  writeLocalPreferences,
  type ThreadWidth,
} from "@/lib/local-preferences";
import type { UIMessage } from "@/lib/types";

const messages: UIMessage[] = [
  { id: "u1", role: "user", content: "hello", createdAt: Date.now() },
];

function storeWidth(threadWidth: ThreadWidth): void {
  window.localStorage.setItem(
    LOCAL_PREFS_STORAGE_KEY,
    JSON.stringify({ ...DEFAULT_LOCAL_PREFS, threadWidth }),
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe("thread width preference", () => {
  it("defaults to wide, and an unknown stored value does not break the layout", () => {
    expect(DEFAULT_LOCAL_PREFS.threadWidth).toBe("wide");
    expect(normalizeThreadWidth("something-else")).toBe("wide");
    expect(normalizeThreadWidth(undefined)).toBe("wide");
    expect(normalizeThreadWidth("standard")).toBe("standard");
    expect(normalizeThreadWidth("full")).toBe("full");
  });

  it("round trips through storage", () => {
    writeLocalPreferences({ ...DEFAULT_LOCAL_PREFS, threadWidth: "full" });

    expect(readLocalPreferences().threadWidth).toBe("full");
  });

  it.each<ThreadWidth>(["standard", "wide", "full"])(
    "holds prose at the %s measure and lets a table reach further",
    (choice) => {
      storeWidth(choice);

      render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);

      const column = screen.getByTestId("thread-message-region").firstElementChild;
      expect(column).toBeInstanceOf(HTMLElement);
      const style = (column as HTMLElement).style;
      expect(style.maxWidth).toBe(THREAD_WIDTHS[choice].measure);
      // The property is what ``.thread-bleed`` reads. Prose keeps the measure; a table, a code
      // block or a diagram takes this instead, so the two never have to share one number.
      expect(style.getPropertyValue("--thread-bleed")).toBe(THREAD_WIDTHS[choice].bleed);
      expect(THREAD_WIDTHS[choice].bleed).not.toBe(THREAD_WIDTHS[choice].measure);
    },
  );

  it("follows a change made from Settings without a reload", async () => {
    storeWidth("standard");
    render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);
    const column = screen.getByTestId("thread-message-region").firstElementChild as HTMLElement;
    expect(column.style.maxWidth).toBe(THREAD_WIDTHS.standard.measure);

    await act(async () => {
      writeLocalPreferences({ ...DEFAULT_LOCAL_PREFS, threadWidth: "full" });
    });

    expect(column.style.maxWidth).toBe(THREAD_WIDTHS.full.measure);
  });

  it("keeps the grid wide enough for the widest thing it holds", () => {
    storeWidth("wide");

    render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);

    const layout = document.querySelector<HTMLElement>(".thread-layout");
    expect(layout).not.toBeNull();
    // A bleeding table is centred inside this grid, so a grid narrower than the bleed would clip
    // it: the message region hides horizontal overflow on purpose.
    expect(layout?.style.maxWidth).toBe(THREAD_WIDTHS.wide.bleed);
  });
});
