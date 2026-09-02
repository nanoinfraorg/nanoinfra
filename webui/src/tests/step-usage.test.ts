import { describe, expect, it } from "vitest";

import { clusterStepUsage, stampStepUsage } from "@/lib/step-usage";
import type { TurnUsage, UIMessage } from "@/lib/types";

/**
 * A step carries the cost of the call behind it (#208).
 *
 * On the measured turn every one of the 23 `stream_end` records held `stream_id`, `turn_id`,
 * `turn_phase`, `turn_seq`, `resuming` and `created_at_ms` — and no tokens, no duration. So a step
 * could only repeat the turn's single figures, and eight consecutive clusters read `7m 57s`.
 *
 * These pin the two decisions that are easy to get wrong: which row a call's cost lands on, and
 * what happens to a cache figure the provider never reported.
 */

function usage(overrides: Partial<TurnUsage> = {}): TurnUsage {
  return {
    prompt_tokens: 21_000,
    completion_tokens: 1_500,
    total_tokens: 22_500,
    request_count: 1,
    estimated_tokens: 0,
    cached_tokens: 20_160,
    ...overrides,
  };
}

function trace(id: string, turnId = "t1"): UIMessage {
  return { id, role: "tool", kind: "trace", content: "Running exec", turnId } as UIMessage;
}

function answer(id: string, turnId = "t1"): UIMessage {
  return { id, role: "assistant", content: "done", turnId } as UIMessage;
}

function user(id: string, turnId = "t1"): UIMessage {
  return { id, role: "user", content: "hola", turnId } as UIMessage;
}

describe("stamping one call's cost", () => {
  it("lands on the activity row that call produced", () => {
    const next = stampStepUsage([user("u"), trace("a")], {
      turnId: "t1",
      usage: usage(),
      durationMs: 47_300,
    });

    expect(next[1].stepUsage?.prompt_tokens).toBe(21_000);
    expect(next[1].stepModelMs).toBe(47_300);
  });

  it("reaches a trace row despite its role being tool, not assistant", () => {
    // The bug this pins: a role test finds nothing, because an activity row is `role: "tool"`.
    const next = stampStepUsage([trace("a")], { turnId: "t1", usage: usage() });

    expect(next[0].stepUsage).toBeDefined();
  });

  it("falls through to the answer when the call streamed only text", () => {
    const next = stampStepUsage([user("u"), answer("a")], {
      turnId: "t1",
      usage: usage(),
      durationMs: 1_200,
    });

    expect(next[1].stepModelMs).toBe(1_200);
  });

  it("keeps both costs when one row anchors two calls", () => {
    // The cluster adds its rows up, so the second call must not replace the first: a turn that
    // cost 55K would otherwise render as 34K.
    const first = stampStepUsage([trace("a")], {
      turnId: "t1",
      usage: usage({ prompt_tokens: 21_000 }),
      durationMs: 4_400,
    });
    const second = stampStepUsage(first, {
      turnId: "t1",
      usage: usage({ prompt_tokens: 34_000 }),
      durationMs: 71_500,
    });

    expect(second[0].stepUsage?.prompt_tokens).toBe(55_000);
    expect(second[0].stepUsage?.request_count).toBe(2);
    expect(second[0].stepModelMs).toBe(75_900);
  });

  it("drops a cache figure only one of the two calls reported", () => {
    const first = stampStepUsage([trace("a")], { turnId: "t1", usage: usage() });
    const second = stampStepUsage(first, {
      turnId: "t1",
      usage: usage({ cached_tokens: undefined }),
    });

    expect(second[0].stepUsage?.prompt_tokens).toBe(42_000);
    expect(second[0].stepUsage?.cached_tokens).toBeUndefined();
  });

  it("does not cross turns", () => {
    const next = stampStepUsage([trace("a", "t0"), user("u", "t1")], {
      turnId: "t1",
      usage: usage(),
    });

    expect(next[0].stepUsage).toBeUndefined();
  });

  it("returns the same array when there is nothing to stamp", () => {
    const messages = [trace("a")];

    expect(stampStepUsage(messages, { turnId: "t1" })).toBe(messages);
  });
});

describe("a cluster's step usage", () => {
  it("sums the calls its rows hold", () => {
    const rows = [
      { ...trace("a"), stepUsage: usage({ prompt_tokens: 21_000 }), stepModelMs: 4_400 },
      { ...trace("b"), stepUsage: usage({ prompt_tokens: 34_000 }), stepModelMs: 71_500 },
    ] as UIMessage[];

    const total = clusterStepUsage(rows);

    expect(total?.steps).toBe(2);
    expect(total?.inputTokens).toBe(55_000);
    expect(total?.outputTokens).toBe(3_000);
    expect(total?.modelMs).toBe(75_900);
  });

  it("averages the cache share only over the steps that reported one", () => {
    // 3 of the 23 calls reported no `cached_tokens`, between neighbours at 99% and 93%. Counting
    // those as zero would have printed a cold cache that never happened.
    const rows = [
      { ...trace("a"), stepUsage: usage({ prompt_tokens: 20_000, cached_tokens: 19_000 }) },
      { ...trace("b"), stepUsage: usage({ prompt_tokens: 30_000, cached_tokens: undefined }) },
    ] as UIMessage[];

    const total = clusterStepUsage(rows);

    expect(total?.cachedTokens).toBe(19_000);
    expect(total?.cachedOverInputTokens).toBe(20_000);
    expect(total?.inputTokens).toBe(50_000);
  });

  it("reports nothing for a cluster whose calls reported nothing", () => {
    expect(clusterStepUsage([trace("a"), trace("b")])).toBeNull();
  });

  it("still reports a duration when only that was measured", () => {
    const rows = [{ ...trace("a"), stepModelMs: 4_400 }] as UIMessage[];

    expect(clusterStepUsage(rows)?.modelMs).toBe(4_400);
    expect(clusterStepUsage(rows)?.steps).toBe(0);
  });
});
