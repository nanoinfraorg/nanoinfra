/**
 * A manager turn's plan, read off the delegations it made (#252).
 *
 * A turn that delegates is not a turn that called N unrelated tools. The request goes to the
 * manager, the manager decides *who* and *how many*, and the thread's object is that decision --
 * one row per delegation, each naming the peer that ran it. So the calls are collected out of the
 * activity rows and rendered as one unit rather than as N tool traces.
 *
 * Two rules this module exists to keep:
 *
 * 1. **One call is one row, keyed by `call_id`.** Two delegations to the same peer with the same
 *    task are two rows. The trace *lines* dedupe by text on both the live and the replay path, so
 *    keying on the line would silently drop the second -- and the plan would under-report a
 *    fan-out. The structured events key on the call id, which is what the two paths agree on.
 * 2. **A cost is shown only where one was reported.** A delegated turn is its own turn with its
 *    own usage (#209), so the peer's cost arrives on the delegation's own event. The manager's
 *    `stepUsage` on the surrounding trace row is the *manager's* provider call and is never read
 *    here: printing it as the peer's cost would attribute one turn's tokens to another.
 *
 * Scope: one activity cluster, which is where a fan-out lands -- N calls in one turn are one
 * round of tool calls and therefore one activity row. A turn that delegates, edits a file, then
 * delegates again crosses a cluster boundary and shows two plans, which is what happened: two
 * rounds, two decisions.
 */

import type { ToolProgressEvent, TurnUsage, UIMessage } from "@/lib/types";
import { safeActivityDetail } from "./activity-text";

/** The one tool whose calls make a plan. Delegation is one level deep, so there is only one. */
export const DELEGATE_TOOL_NAME = "delegate_to_agent";

/**
 * What became of one delegation.
 *
 * `running` on a finished turn is not `done`: it means the peer never reported, which a reader has
 * to be able to tell apart from an answer. The component words it as such.
 */
export type DelegationStatus = "running" | "done" | "error";

export interface DelegationStep {
  /** The call this row is, so a fan-out to one peer twice stays two rows. */
  key: string;
  /** The peer that ran it. */
  agent: string;
  /** The task the manager wrote, redacted and shortened for a single line. */
  task: string;
  /** The manager's own label for the delegation, when it wrote one. */
  label?: string;
  status: DelegationStatus;
  /** What the peer failed with, when it failed. */
  error?: string;
  /** The peer's own turn cost, when the delegation reported one. */
  usage?: TurnUsage;
}

/**
 * The plan's total: arithmetic over its rows, never a number reported beside them.
 *
 * `cachedTokens` follows the #208 rule -- summed only over the steps that reported it, with the
 * input those steps carried, because a step that reported no cache metric is not a step that hit
 * a cold cache.
 */
export interface DelegationPlanCost {
  steps: number;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number | null;
  cachedOverInputTokens: number;
}

export interface DelegationPlan {
  steps: DelegationStep[];
  running: number;
  done: number;
  failed: number;
  /** `null` when no peer reported a cost, so the header prints no figure nobody measured. */
  cost: DelegationPlanCost | null;
}

const STATUS_RANK: Record<DelegationStatus, number> = { running: 1, done: 2, error: 3 };
const DELEGATE_TRACE_RE = new RegExp(`^${DELEGATE_TOOL_NAME}\\((.*)\\)$`);

/** True when this activity line is a delegation, and therefore belongs to the plan and not to the
 * tool timeline. */
export function isDelegationTraceLine(line: string): boolean {
  return DELEGATE_TRACE_RE.test(line.trim());
}

function toolEventName(event: ToolProgressEvent): string {
  const fnName = (event as { function?: { name?: unknown } }).function?.name;
  if (typeof fnName === "string" && fnName) return fnName;
  return typeof event.name === "string" ? event.name : "";
}

function eventArguments(event: ToolProgressEvent): Record<string, unknown> {
  const fnArgs = (event as { function?: { arguments?: unknown } }).function?.arguments;
  const raw = fnArgs ?? event.arguments;
  if (typeof raw === "string") {
    if (!raw.trim()) return {};
    try {
      return asRecord(JSON.parse(raw));
    } catch {
      return {};
    }
  }
  return asRecord(raw);
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function statusFromPhase(phase: unknown): DelegationStatus {
  if (phase === "error") return "error";
  if (phase === "end") return "done";
  return "running";
}

function errorText(error: unknown): string | undefined {
  // The `Error:` prefix is how a tool result says "this failed" to the model. The row already
  // says so in its own words, so keeping it would state the same thing twice.
  if (typeof error === "string" && error.trim()) {
    return error.trim().replace(/^Error:\s*/i, "") || undefined;
  }
  if (error && typeof error === "object") {
    try {
      return JSON.stringify(error);
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function usageOrUndefined(value: unknown): TurnUsage | undefined {
  const record = asRecord(value);
  const input = record.prompt_tokens;
  const output = record.completion_tokens;
  if (typeof input !== "number" || typeof output !== "number") return undefined;
  return record as unknown as TurnUsage;
}

/**
 * The peer's own cost, from wherever the delegation reported it.
 *
 * Two places, because a delegated turn's usage belongs to the delegation and not to the row: on
 * the event itself, and inside a structured result for a producer that returns an artifact rather
 * than text. Absent from both means the peer reported nothing, and the row prints no cost.
 */
function delegationUsage(event: ToolProgressEvent): TurnUsage | undefined {
  return (
    usageOrUndefined((event as { usage?: unknown }).usage)
    ?? usageOrUndefined(asRecord(event.result).usage)
  );
}

function stepFromEvent(event: ToolProgressEvent): DelegationStep | null {
  if (toolEventName(event) !== DELEGATE_TOOL_NAME) return null;
  const args = eventArguments(event);
  const agent = typeof args.agent === "string" ? args.agent.trim() : "";
  const task = typeof args.task === "string" ? args.task : "";
  const label = typeof args.label === "string" && args.label.trim() ? args.label.trim() : undefined;
  const key = event.call_id ? `call:${event.call_id}` : `args:${JSON.stringify(args)}`;
  const status = statusFromPhase(event.phase);
  const failure = status === "error"
    ? errorText(event.error) ?? errorText(event.result)
    : undefined;
  const usage = delegationUsage(event);
  return {
    key,
    agent,
    task: safeActivityDetail(task, 120),
    ...(label ? { label } : {}),
    status,
    ...(failure ? { error: failure } : {}),
    ...(usage ? { usage } : {}),
  };
}

function stepFromTraceLine(line: string): DelegationStep | null {
  const match = DELEGATE_TRACE_RE.exec(line.trim());
  if (!match) return null;
  const args = ((): Record<string, unknown> => {
    const text = match[1].trim();
    if (!text) return {};
    try {
      return asRecord(JSON.parse(text));
    } catch {
      return {};
    }
  })();
  const agent = typeof args.agent === "string" ? args.agent.trim() : "";
  const task = typeof args.task === "string" ? args.task : "";
  const label = typeof args.label === "string" && args.label.trim() ? args.label.trim() : undefined;
  return {
    key: `line:${line}`,
    agent,
    task: safeActivityDetail(task, 120),
    ...(label ? { label } : {}),
    status: "running",
  };
}

function mergeStep(existing: DelegationStep, incoming: DelegationStep): DelegationStep {
  const keepStatus = STATUS_RANK[incoming.status] >= STATUS_RANK[existing.status];
  return {
    ...existing,
    ...(keepStatus ? { status: incoming.status } : {}),
    ...(incoming.error ? { error: incoming.error } : {}),
    ...(incoming.usage ? { usage: incoming.usage } : {}),
    ...(incoming.label ? { label: incoming.label } : {}),
    ...(incoming.task ? { task: incoming.task } : {}),
  };
}

function traceLines(message: UIMessage): string[] {
  if (message.traces?.length) return message.traces;
  return message.content.trim() ? [message.content] : [];
}

/**
 * The turn's plan, or `null` when it delegated nothing -- which is every turn today, and the
 * property that has to stay true: no delegation, no new object in the thread.
 */
export function collectDelegationPlan(messages: UIMessage[]): DelegationPlan | null {
  const byKey = new Map<string, DelegationStep>();

  for (const message of messages) {
    if (message.kind !== "trace") continue;
    let hasStructuredDelegation = false;
    for (const event of message.toolEvents ?? []) {
      const step = stepFromEvent(event);
      if (!step) continue;
      hasStructuredDelegation = true;
      const existing = byKey.get(step.key);
      byKey.set(step.key, existing ? mergeStep(existing, step) : step);
    }
    // The trace line is the fallback for a deployment whose channel sends hints without the
    // structured events. Skipped when the same row carried them, because the line and the event
    // describe the same call under two different keys.
    if (hasStructuredDelegation) continue;
    for (const line of traceLines(message)) {
      const step = stepFromTraceLine(line);
      if (!step || byKey.has(step.key)) continue;
      byKey.set(step.key, step);
    }
  }

  const steps = [...byKey.values()];
  if (!steps.length) return null;

  let usageSteps = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens: number | null = null;
  let cachedOverInputTokens = 0;
  for (const step of steps) {
    if (!step.usage) continue;
    usageSteps += 1;
    inputTokens += step.usage.prompt_tokens ?? 0;
    outputTokens += step.usage.completion_tokens ?? 0;
    if (typeof step.usage.cached_tokens === "number") {
      cachedTokens = (cachedTokens ?? 0) + step.usage.cached_tokens;
      cachedOverInputTokens += step.usage.prompt_tokens ?? 0;
    }
  }

  return {
    steps,
    running: steps.filter((step) => step.status === "running").length,
    done: steps.filter((step) => step.status === "done").length,
    failed: steps.filter((step) => step.status === "error").length,
    cost: usageSteps
      ? { steps: usageSteps, inputTokens, outputTokens, cachedTokens, cachedOverInputTokens }
      : null,
  };
}
