import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  ComposerUsagePopover,
  type ComposerRoundUsage,
} from "@/components/thread/ComposerUsagePopover";

/**
 * A bar's three segments are the three rates we bill, so their heights have to be a true division
 * of the input it reports.
 *
 * `prompt_tokens` **includes** cache reads and writes -- rule 1 of the `LLMUsage` contract -- so
 * the fresh bucket is a subtraction, exactly as `llm_usage/pricing.py:uncached_input_tokens` does
 * it server-side. Getting that wrong is not cosmetic: a bar drawn from `prompt_tokens` plus
 * `cached_tokens` counts the cached tokens twice, which is the same double-count that over-bills
 * a warm prompt by 5.7x.
 */

function round(extra: Partial<ComposerRoundUsage> = {}): ComposerRoundUsage {
  return {
    id: "r1",
    timestamp: 1_700_000_000_000,
    inputTokens: 20_000,
    outputTokens: 1_200,
    ...extra,
  };
}

function segmentHeights(): Record<string, string> {
  const bar = screen.getAllByTestId("round-usage-bar").at(-1)!;
  const heights: Record<string, string> = {};
  for (const segment of bar.querySelectorAll<HTMLElement>("[data-segment]")) {
    heights[segment.dataset.segment!] = segment.style.height;
  }
  return heights;
}

async function openPanel() {
  await userEvent.setup().click(screen.getByTestId("composer-context-usage"));
  await waitFor(() => expect(screen.getAllByTestId("round-usage-bar").length).toBeGreaterThan(0));
}

describe("ComposerUsagePopover", () => {
  it("renders nothing until something has been measured", () => {
    const { container } = render(<ComposerUsagePopover context={null} rounds={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("splits a bar into the three buckets, summing to the input it reports", async () => {
    render(
      <ComposerUsagePopover
        context={{ contextTokens: 87_300, contextWindowTokens: 200_000 }}
        rounds={[round({ cachedTokens: 16_000, cacheWriteTokens: 1_000 })]}
      />,
    );
    await openPanel();

    // 16,000 read + 1,000 written + 3,000 fresh = the 20,000 reported.
    expect(segmentHeights()).toEqual({
      written: "5%",
      fresh: "15%",
      read: "80%",
    });
  });

  it("clamps an impossible report instead of overflowing the bar", async () => {
    // A provider claiming more cached tokens than its own prompt total is stating something
    // impossible. Server-side that clamps the fresh bucket at zero; here it must additionally not
    // draw 150% of a bar.
    render(
      <ComposerUsagePopover
        context={null}
        rounds={[round({ cachedTokens: 25_000, cacheWriteTokens: 5_000 })]}
      />,
    );
    await openPanel();

    expect(segmentHeights()).toEqual({ read: "100%" });
  });

  it("shows one solid segment when the provider reported no cache metric", async () => {
    render(<ComposerUsagePopover context={null} rounds={[round()]} />);
    await openPanel();

    expect(segmentHeights()).toEqual({ fresh: "100%" });
  });

  it("reads the context as a fraction, and says the percentage on the trigger", () => {
    render(
      <ComposerUsagePopover
        context={{ contextTokens: 87_300, contextWindowTokens: 200_000 }}
        rounds={[]}
      />,
    );

    expect(screen.getByTestId("composer-context-usage")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("44%"),
    );
  });

  it("draws no meter when the turn reported no window", () => {
    // A used figure with no window is a token count. The control still opens for the rounds.
    render(<ComposerUsagePopover context={{ contextTokens: 87_300 }} rounds={[round()]} />);

    expect(screen.queryByTestId("composer-context-meter")).toBeNull();
    expect(screen.getByTestId("composer-context-usage")).toBeInTheDocument();
  });

  it("names every bucket in the accessible label, so the chart needs no hover", async () => {
    render(
      <ComposerUsagePopover
        context={null}
        rounds={[round({ cachedTokens: 16_000, cacheWriteTokens: 1_000, generationMs: 4_400 })]}
      />,
    );
    await openPanel();

    const label = screen.getAllByTestId("round-usage-bar").at(-1)!.getAttribute("aria-label")!;
    expect(label).toContain("20,000");
    expect(label).toContain("Cache writes 1,000");
    expect(label).toContain("Cache hit rate 80%");
    expect(label).toContain("Generation time");
  });
});
