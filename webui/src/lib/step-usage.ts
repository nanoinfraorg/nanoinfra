/**
 * What one provider call inside a turn cost (#208).
 *
 * A turn that made 23 calls carried one `usage` and one `latencyMs`, so every step could do was
 * repeat them -- which is how eight consecutive clusters came to read `7m 57s`. The `stream_end`
 * frame now carries the usage of the single call behind it, and this module decides which row it
 * lands on and how a cluster summarises the rows it holds.
 *
 * The anchor rule, in one sentence: **the most recent activity row of this turn, else the turn's
 * answer row.** A call streams its reasoning and its tool hints before its `stream_end`, so that
 * row is the one the call produced; a call that streamed only text has no activity row and lands
 * on its answer. A row reached twice keeps both costs rather than the later one, because the
 * cluster adds its rows up and a reloaded turn has to come to the same total as the live one.
 */

import { isAgentActivityMember } from "@/lib/activity-timeline";
import type { TurnUsage, UIMessage } from "@/lib/types";

/** A cluster's aggregate, computed only from the steps that reported each figure. */
export interface ClusterStepUsage {
  /** Provider calls in this cluster that reported usage. */
  steps: number;
  inputTokens: number;
  outputTokens: number;
  /**
   * Cache reads, summed over the steps that reported them, and the input those steps carried.
   *
   * Two numbers rather than a percentage because they cannot be mixed: 3 of the 23 calls on the
   * measured turn reported no `cached_tokens` at all, sitting between neighbours at 99% and 93%.
   * Averaging those as zero would have printed a cold cache that never happened.
   */
  cachedTokens: number | null;
  cachedOverInputTokens: number;
  /** Wall time spent inside the provider calls, which is part of -- not all of -- the cluster. */
  modelMs: number;
}

function sameTurn(message: UIMessage, turnId: string | undefined): boolean {
  if (!turnId) return true;
  return message.turnId === turnId;
}

/**
 * Attach one call's usage to the row that call produced.
 *
 * Returns the same array when there is nothing to stamp, so a caller can pass it straight to
 * `setMessages` without forcing a render.
 */
export function stampStepUsage(
  messages: UIMessage[],
  {
    turnId,
    usage,
    durationMs,
  }: { turnId?: string; usage?: TurnUsage; durationMs?: number },
): UIMessage[] {
  if (!usage && !durationMs) return messages;

  let answerIndex = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!sameTurn(message, turnId)) continue;
    // An activity row: a tool trace, or an assistant row holding only reasoning. A trace row
    // carries `role: "tool"`, which is why this is a membership test rather than a role test.
    if (isAgentActivityMember(message)) {
      const next = messages.slice();
      next[i] = mergeStep(message, usage, durationMs);
      return next;
    }
    if (message.role === "assistant" && answerIndex < 0) answerIndex = i;
  }

  if (answerIndex < 0) return messages;
  const next = messages.slice();
  next[answerIndex] = mergeStep(messages[answerIndex], usage, durationMs);
  return next;
}

/**
 * Add one more call's cost to a row, keeping whatever it already held.
 *
 * A row can anchor more than one call -- a call that streams no trace of its own lands on the
 * previous one -- and the replay merges consecutive tool hints into a single row outright. The
 * cluster sums its rows, so summing here is what keeps a live turn and a reloaded one equal.
 *
 * `cached_tokens` survives only when **both** calls reported it: 3 of the 23 calls on the measured
 * turn reported none, and mixing a known figure with an unknown one would print a cache share for
 * input nobody measured.
 */
function mergeStep(
  message: UIMessage,
  usage: TurnUsage | undefined,
  durationMs: number | undefined,
): UIMessage {
  const merged: UIMessage = { ...message };
  if (durationMs) merged.stepModelMs = (message.stepModelMs ?? 0) + durationMs;
  if (!usage) return merged;
  const existing = message.stepUsage;
  if (!existing) {
    merged.stepUsage = usage;
    return merged;
  }
  const summed: TurnUsage = {
    prompt_tokens: existing.prompt_tokens + usage.prompt_tokens,
    completion_tokens: existing.completion_tokens + usage.completion_tokens,
    total_tokens: existing.total_tokens + usage.total_tokens,
    request_count: existing.request_count + usage.request_count,
    estimated_tokens: existing.estimated_tokens + usage.estimated_tokens,
  };
  if (
    typeof existing.cached_tokens === "number"
    && typeof usage.cached_tokens === "number"
  ) {
    summed.cached_tokens = existing.cached_tokens + usage.cached_tokens;
  }
  merged.stepUsage = summed;
  return merged;
}

/**
 * Sum the step usage held by one cluster's rows, or `null` when none of them carry any.
 *
 * Arithmetic only: the wording lives in the component, because that is where `t` is.
 */
export function clusterStepUsage(messages: UIMessage[]): ClusterStepUsage | null {
  let steps = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens: number | null = null;
  let cachedOverInputTokens = 0;
  let modelMs = 0;

  for (const message of messages) {
    modelMs += message.stepModelMs ?? 0;
    const usage = message.stepUsage;
    if (!usage) continue;
    steps += 1;
    inputTokens += usage.prompt_tokens ?? 0;
    outputTokens += usage.completion_tokens ?? 0;
    if (typeof usage.cached_tokens === "number") {
      cachedTokens = (cachedTokens ?? 0) + usage.cached_tokens;
      cachedOverInputTokens += usage.prompt_tokens ?? 0;
    }
  }

  if (steps === 0 && modelMs === 0) return null;
  return { steps, inputTokens, outputTokens, cachedTokens, cachedOverInputTokens, modelMs };
}
