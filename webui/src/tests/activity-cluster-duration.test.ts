import { describe, expect, it } from "vitest";

import { normalizeActivityTimeline } from "@/lib/activity-timeline";
import type { UIMessage } from "@/lib/types";

/**
 * Each activity cluster reports its own duration (#208).
 *
 * Read on a real turn: 23 provider calls over 7m 57s, and **eight consecutive clusters all read
 * `Worked for 7m 57s`** — the turn's `latency_ms` rendered once per cluster. Individual steps in
 * that turn ranged from 4.4s to 71.5s, so a 71-second step and a 4-second one looked identical.
 *
 * `pushActivityUnits` was the cause rather than the component: it handed every unit the same
 * `turnLatencyMs`, found by `activityTurnLatencyMs` scanning backwards for the last `latencyMs` in
 * the turn. The component only displayed what it was given.
 *
 * A turn starts at the user message's `createdAt`, so these build one, which is also what makes the
 * first cluster's wait measurable: the provider call happens before any trace arrives.
 */

function user(createdAt: number): UIMessage {
  return {
    id: `u-${createdAt}`,
    role: "user",
    content: "do the thing",
    createdAt,
    turnId: "t1",
  } as UIMessage;
}

function trace(id: string, createdAt: number, extra: Partial<UIMessage> = {}): UIMessage {
  return {
    id,
    role: "assistant",
    kind: "trace",
    content: "",
    createdAt,
    turnId: "t1",
    ...extra,
  } as UIMessage;
}

function fileEdit(id: string, createdAt: number, path: string): UIMessage {
  return trace(id, createdAt, {
    fileEdits: [{ path, additions: 1, deletions: 0 }],
    activitySegmentId: path,
  } as Partial<UIMessage>);
}

function durations(messages: UIMessage[]): (number | undefined)[] {
  return normalizeActivityTimeline(messages)
    .filter((unit) => unit.type === "activity")
    .map((unit) => (unit as { turnLatencyMs?: number }).turnLatencyMs);
}

describe("a cluster's duration", () => {
  it("gives the first cluster the wait before its first trace", () => {
    // The turn starts at 1s and the first trace lands at 41s: the model was working for 40s and
    // the cluster measured from its own messages would call that zero.
    const spans = durations([user(1_000), trace("a1", 41_000), trace("a2", 48_000)]);

    expect(spans[0]).toBe(47_000);
  });

  it("measures a later cluster from where the previous one ended", () => {
    // Otherwise every cluster's span includes all the work before it and the last one always
    // reports the whole turn.
    const spans = durations([
      user(0),
      trace("a1", 10_000),
      fileEdit("f1", 10_500, "x.md"),
      trace("b1", 70_000, { latencyMs: 80_000 }),
    ]);

    expect(spans[0]).toBe(10_000);
    expect(spans[spans.length - 1]).toBe(59_500);
  });

  it("does not repeat one figure across every cluster of a turn", () => {
    const spans = durations([
      user(0),
      trace("a1", 5_000),
      fileEdit("f1", 6_000, "x.md"),
      trace("b1", 30_000),
      fileEdit("f2", 32_000, "y.md"),
      trace("c1", 60_000, { latencyMs: 476_657 }),
    ]);

    expect(spans.length).toBeGreaterThan(2);
    expect(new Set(spans).size).toBe(spans.length);
    expect(spans).not.toContain(476_657);
  });

  it("falls back to the turn latency when the turn start is unknown" , () => {
    // A replayed transcript may carry no user message, so there is no start to measure from.
    // Reporting the turn's own figure is then the only honest answer available.
    const spans = durations([trace("a1", 1_000, { latencyMs: 12_400 })]);

    expect(spans[0]).toBe(12_400);
  });

  it("reports nothing rather than a negative span", () => {
    // `created_at_ms` is written by the server and the browser reorders by `turn_seq`. A cluster
    // whose end precedes its start is a clock artefact, not a duration.
    const spans = durations([user(9_000), trace("a1", 1_000)]);

    expect(spans[0] === undefined || spans[0] >= 0).toBe(true);
  });

  it("keeps a single-cluster turn reading as the turn", () => {
    // The common case, and the one the existing cluster tests pin: one cluster, and its span is
    // the turn's span.
    const spans = durations([user(0), trace("a1", 12_400, { latencyMs: 12_400 })]);

    expect(spans).toEqual([12_400]);
  });
});
