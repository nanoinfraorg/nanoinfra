import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ThreadViewport } from "@/components/thread/ThreadViewport";
import {
  DEFAULT_LOCAL_PREFS,
  LOCAL_PREFS_STORAGE_KEY,
  THREAD_WIDTHS,
} from "@/lib/local-preferences";
import type { UIMessage } from "@/lib/types";

const TABLE = "| Package | Version |\n| --- | --- |\n| docker-ce | 29.7.2 |";

/** Two assistant turns: the first is off the end of the list, so it is the deferred one. */
const messages: UIMessage[] = [
  { id: "u1", role: "user", content: "what is upgradable?", createdAt: 1 },
  { id: "a1", role: "assistant", content: TABLE, createdAt: 2 },
  { id: "u2", role: "user", content: "and now?", createdAt: 3 },
  { id: "a2", role: "assistant", content: TABLE, createdAt: 4 },
];

function units(): HTMLElement[] {
  const region = screen.getByTestId("thread-message-region");
  return Array.from(region.querySelectorAll<HTMLElement>(".thread-unit"));
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem(
    LOCAL_PREFS_STORAGE_KEY,
    JSON.stringify({ ...DEFAULT_LOCAL_PREFS, threadWidth: "full" }),
  );
});

describe("a display unit is as wide as the content that bleeds past the measure", () => {
  it("gives every unit the bleed geometry, deferred or not", () => {
    render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);

    const all = units();
    expect(all.length).toBeGreaterThan(1);
    const deferred = all.filter((unit) => unit.classList.contains("thread-render-unit"));
    const eager = all.filter((unit) => !unit.classList.contains("thread-render-unit"));

    // The bug: only the deferred units carried paint containment, and they were the width of the
    // reading measure, so a bleeding table lost its left edge. Both branches must be one box.
    expect(deferred.length).toBeGreaterThan(0);
    expect(eager.length).toBeGreaterThan(0);
    for (const unit of all) {
      expect(unit.classList.contains("thread-unit")).toBe(true);
      expect(unit.querySelector(".thread-unit-measure")).not.toBeNull();
    }
  });

  it("keeps the message inside a measure wrapper, so prose is unaffected", () => {
    render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);

    for (const unit of units()) {
      const measure = unit.querySelector<HTMLElement>(".thread-unit-measure");
      expect(measure).not.toBeNull();
      // Content lives inside the wrapper rather than beside it, or the measure means nothing.
      expect(measure?.childElementCount).toBeGreaterThan(0);
      expect(unit.firstElementChild).toBe(measure);
    }
  });

  it("publishes the measure as a variable the unit can read", () => {
    render(<ThreadViewport messages={messages} isStreaming={false} composer={null} />);

    const column = screen.getByTestId("thread-message-region").firstElementChild as HTMLElement;
    expect(column.style.getPropertyValue("--thread-measure")).toBe(THREAD_WIDTHS.full.measure);
    expect(column.style.getPropertyValue("--thread-bleed")).toBe(THREAD_WIDTHS.full.bleed);
  });
});
