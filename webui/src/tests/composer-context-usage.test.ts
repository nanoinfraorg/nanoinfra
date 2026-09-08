import { describe, expect, it } from "vitest";

import {
  latestComposerContextUsage,
  recentComposerRoundUsage,
} from "@/lib/composer-context-usage";
import type { TurnUsage, UIMessage } from "@/lib/types";

/**
 * The composer's context readout is a projection of the transcript.
 *
 * Both numbers are already on the rows -- `usage.context_tokens` and the window on the answer row
 * of `turn_end`, each provider call's own cost on the row it produced (#208) -- so these assert
 * that the projection reads them, and that it refuses to invent the parts nobody reported.
 */

function answer(id: string, extra: Partial<UIMessage> = {}): UIMessage {
  return {
    id,
    role: "assistant",
    kind: "message",
    content: "done",
    createdAt: 1_700_000_000_000,
    ...extra,
  } as UIMessage;
}

function usage(extra: Partial<TurnUsage> = {}): TurnUsage {
  return {
    prompt_tokens: 21_000,
    completion_tokens: 1_500,
    total_tokens: 22_500,
    request_count: 1,
    estimated_tokens: 0,
    ...extra,
  };
}

describe("latestComposerContextUsage", () => {
  it("reads the used figure and the window off the most recent settled answer", () => {
    const result = latestComposerContextUsage([
      answer("a1", { usage: usage({ context_tokens: 40_000 }), contextWindowTokens: 200_000 }),
      answer("a2", { usage: usage({ context_tokens: 87_300 }), contextWindowTokens: 200_000 }),
    ]);

    expect(result).toEqual({ contextTokens: 87_300, contextWindowTokens: 200_000 });
  });

  it("keeps the previous reading while a turn is still streaming", () => {
    const result = latestComposerContextUsage([
      answer("a1", { usage: usage({ context_tokens: 87_300 }), contextWindowTokens: 200_000 }),
      answer("a2", { isStreaming: true }),
    ]);

    expect(result?.contextTokens).toBe(87_300);
  });

  it("skips trace rows, which carry a step's cost and not the turn's position", () => {
    const result = latestComposerContextUsage([
      answer("a1", { usage: usage({ context_tokens: 87_300 }), contextWindowTokens: 200_000 }),
      answer("t1", {
        kind: "trace",
        usage: usage({ context_tokens: 5 }),
        contextWindowTokens: 8,
      }),
    ]);

    expect(result?.contextTokens).toBe(87_300);
  });

  it("reports the used figure with no window when the gateway sent none", () => {
    // A number, not a fraction. Drawing it against a guessed denominator would be worse than
    // drawing no ring at all, so the popover falls back to the rounds alone.
    const result = latestComposerContextUsage([
      answer("a1", { usage: usage({ context_tokens: 87_300 }) }),
    ]);

    expect(result).toEqual({ contextTokens: 87_300 });
  });

  it("is null when no turn ever reported a used figure", () => {
    expect(latestComposerContextUsage([answer("a1", { usage: usage() })])).toBeNull();
  });
});

describe("recentComposerRoundUsage", () => {
  it("returns one round per row carrying a step cost, oldest first", () => {
    const rounds = recentComposerRoundUsage([
      answer("t1", {
        kind: "trace",
        stepUsage: usage({ prompt_tokens: 9_000, cached_tokens: 8_000 }),
        stepModelMs: 4_400,
      }),
      answer("a1", {
        stepUsage: usage({ prompt_tokens: 21_000, cached_tokens: 20_160, cache_write_tokens: 500 }),
        completedAt: 1_700_000_050_000,
      }),
    ]);

    expect(rounds).toEqual([
      {
        id: "t1",
        timestamp: 1_700_000_000_000,
        inputTokens: 9_000,
        outputTokens: 1_500,
        cachedTokens: 8_000,
        generationMs: 4_400,
      },
      {
        id: "a1",
        timestamp: 1_700_000_050_000,
        inputTokens: 21_000,
        outputTokens: 1_500,
        cachedTokens: 20_160,
        cacheWriteTokens: 500,
      },
    ]);
  });

  it("omits a cache metric the provider never reported", () => {
    // xAI omitted `cached_tokens` on 3 of 23 calls on the measured turn, between neighbours at
    // 99% and 93%. A zero here renders a cold cache that never happened.
    const [round] = recentComposerRoundUsage([
      answer("a1", { stepUsage: usage({ prompt_tokens: 21_000 }) }),
    ]);

    expect(round).not.toHaveProperty("cachedTokens");
    expect(round).not.toHaveProperty("cacheWriteTokens");
  });

  it("keeps only the last eight rounds", () => {
    const rows = Array.from({ length: 12 }, (_, index) =>
      answer(`s${index}`, { stepUsage: usage({ prompt_tokens: 1_000 + index }) }),
    );

    const rounds = recentComposerRoundUsage(rows);

    expect(rounds).toHaveLength(8);
    expect(rounds[0].id).toBe("s4");
    expect(rounds[7].id).toBe("s11");
  });

  it("drops a round whose input was never measured", () => {
    expect(
      recentComposerRoundUsage([answer("a1", { stepUsage: usage({ prompt_tokens: 0 }) })]),
    ).toEqual([]);
  });
});
