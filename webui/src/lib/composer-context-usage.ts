/**
 * Read the composer's context readout off the transcript, rather than storing it a second place.
 *
 * Both facts are already on the rows: `turn_end` puts `usage.context_tokens` and the window the
 * turn ran under on its answer row, and `stream_end` puts each provider call's own cost on the row
 * that call produced (#208). So this module is projection only -- no state, no subscription, and
 * nothing that can disagree with what the thread shows.
 *
 * Why the window travels per turn instead of being read from the active preset: a thread may
 * switch presets, and the window selected *now* is not the window an earlier turn ran under. A
 * readout built from the current preset would silently restate every earlier turn's fraction
 * against the wrong denominator.
 */

import type {
  ComposerContextUsage,
  ComposerRoundUsage,
} from "@/components/thread/ComposerUsagePopover";
import type { UIMessage } from "@/lib/types";

/** How many of the most recent provider calls the chart holds. */
const MAX_ROUNDS = 8;

function isAnswerRow(message: UIMessage): boolean {
  return message.role === "assistant" && message.kind !== "trace" && !message.isStreaming;
}

/**
 * The most recent settled turn's context position.
 *
 * Walks backwards and stops at the first answer row that reported a used figure, so a turn still
 * streaming does not blank the readout the previous one earned. Returns `null` unless **both**
 * numbers are present: a used figure with no window is a token count, not a fraction, and drawing
 * it against a guessed denominator would be worse than drawing nothing.
 *
 * No compaction special case is needed. A compaction shrinks the context, and the next turn
 * reports the smaller figure -- so the readout corrects itself from the record instead of being
 * reset by a signal that would have to arrive separately and in order.
 */
export function latestComposerContextUsage(messages: UIMessage[]): ComposerContextUsage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!isAnswerRow(message)) continue;
    const contextTokens = message.usage?.context_tokens;
    if (typeof contextTokens !== "number" || !Number.isFinite(contextTokens) || contextTokens < 0) {
      continue;
    }
    return {
      contextTokens,
      ...(typeof message.contextWindowTokens === "number" && message.contextWindowTokens > 0
        ? { contextWindowTokens: message.contextWindowTokens }
        : {}),
    };
  }
  return null;
}

/**
 * The last few provider calls, oldest first.
 *
 * One entry per row carrying `stepUsage`, which is one provider call -- or the two or three a
 * single row anchored, because a call that streams no trace of its own lands on the previous row
 * and `stampStepUsage` sums rather than replaces. That merge is deliberate there: it keeps a live
 * turn and a reloaded one at the same total. It means a bar is *a round*, not always *one call*.
 */
export function recentComposerRoundUsage(messages: UIMessage[]): ComposerRoundUsage[] {
  const rounds: ComposerRoundUsage[] = [];
  for (const message of messages) {
    const usage = message.stepUsage;
    if (!usage) continue;
    const inputTokens = usage.prompt_tokens;
    if (typeof inputTokens !== "number" || !Number.isFinite(inputTokens) || inputTokens <= 0) {
      continue;
    }
    const generationMs = message.stepModelMs ?? usage.generation_ms;
    rounds.push({
      id: message.id,
      timestamp: message.completedAt ?? message.createdAt,
      inputTokens,
      ...(Number.isFinite(usage.completion_tokens)
        ? { outputTokens: usage.completion_tokens }
        : {}),
      ...(typeof usage.cached_tokens === "number" && Number.isFinite(usage.cached_tokens)
        ? { cachedTokens: usage.cached_tokens }
        : {}),
      ...(typeof usage.cache_write_tokens === "number"
        && Number.isFinite(usage.cache_write_tokens)
        ? { cacheWriteTokens: usage.cache_write_tokens }
        : {}),
      // Only when there is something to say. `estimated_tokens` is always reported, and a zero
      // carried here would be an "includes estimated usage" note the round does not warrant.
      ...(Number.isFinite(usage.estimated_tokens) && usage.estimated_tokens > 0
        ? { estimatedTokens: usage.estimated_tokens }
        : {}),
      ...(typeof generationMs === "number" && Number.isFinite(generationMs) && generationMs > 0
        ? { generationMs }
        : {}),
    });
  }
  return rounds.slice(-MAX_ROUNDS);
}
