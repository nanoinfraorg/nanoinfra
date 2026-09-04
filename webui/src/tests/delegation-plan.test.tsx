/**
 * The plan, as the thread's own object (#252).
 *
 * One test per rule, and each one fails if its rule is dropped:
 *
 * 1. A turn with no delegation renders exactly as it did before -- that is every turn today.
 * 2. A two-delegation plan shows both peers, as one object rather than two tool traces.
 * 3. Each delegation carries its own cost, and the plan's total is the sum of its rows.
 * 4. A failed delegation is never shown as complete.
 * 5. A reloaded plan matches the live one.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentActivityCluster } from "@/components/thread/AgentActivityCluster";
import { collectDelegationPlan } from "@/components/thread/activity/delegation-plan-model";
import type { ToolProgressEvent, TurnUsage, UIMessage } from "@/lib/types";

function usage(prompt: number, completion: number, cached?: number): TurnUsage {
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: prompt + completion,
    request_count: 1,
    estimated_tokens: 0,
    ...(cached === undefined ? {} : { cached_tokens: cached }),
  };
}

function delegateEvent(
  callId: string,
  agent: string,
  task: string,
  phase: "start" | "end" | "error",
  extra: Partial<ToolProgressEvent> = {},
): ToolProgressEvent {
  return {
    version: 1,
    phase,
    call_id: callId,
    name: "delegate_to_agent",
    arguments: { agent, task },
    result: phase === "end" ? "answered" : null,
    error: null,
    files: [],
    embeds: [],
    ...extra,
  };
}

function traceLine(event: ToolProgressEvent): string {
  return `delegate_to_agent(${JSON.stringify(event.arguments)})`;
}

/** One activity row holding a finished fan-out, the shape the live client merges frames into. */
function planRow(events: ToolProgressEvent[]): UIMessage {
  return {
    id: "t1",
    role: "tool",
    kind: "trace",
    content: traceLine(events[events.length - 1]),
    traces: events.map(traceLine),
    toolEvents: events,
    turnId: "turn-1",
    createdAt: 2,
  };
}

const REASONING_ROW: UIMessage = {
  id: "r1",
  role: "assistant",
  content: "",
  reasoning: "two things to check",
  turnId: "turn-1",
  createdAt: 1,
};

describe("a turn with no delegation is unchanged", () => {
  it("adds no plan to the thread and still counts its tool call in the fold", () => {
    const { container } = render(
      <AgentActivityCluster
        messages={[
          REASONING_ROW,
          {
            id: "t1",
            role: "tool",
            kind: "trace",
            content: 'grep({"pattern":"disk"})',
            traces: ['grep({"pattern":"disk"})'],
            toolEvents: [
              {
                version: 1,
                phase: "end",
                call_id: "c1",
                name: "grep",
                arguments: { pattern: "disk" },
                result: "3 matches",
                error: null,
              },
            ],
            turnId: "turn-1",
            createdAt: 2,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(container.querySelector("[data-testid='delegation-plan']")).toBeNull();
    expect(container.querySelector("[data-testid='delegation-step']")).toBeNull();
    // The fold still exists, still says the turn worked rather than only thought, and still
    // renders the tool call it holds.
    expect(screen.getByRole("button", { name: /Worked/ })).toBeTruthy();
    expect(screen.getByText(/Searched files/)).toBeTruthy();
  });

  it("reads no plan out of activity rows that hold no delegation", () => {
    expect(collectDelegationPlan([REASONING_ROW])).toBeNull();
    expect(collectDelegationPlan([])).toBeNull();
  });
});

describe("the plan is one object in the thread", () => {
  it("shows one row per delegation, each naming the peer that ran it", () => {
    render(
      <AgentActivityCluster
        messages={[
          REASONING_ROW,
          planRow([
            delegateEvent("c1", "sre-copilot", "check disk and load on barrahome", "end"),
            delegateEvent("c2", "db-expert", "check slow queries", "end"),
          ]),
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    const plan = screen.getByTestId("delegation-plan");
    expect(plan.getAttribute("data-delegation-count")).toBe("2");
    const rows = screen.getAllByTestId("delegation-step");
    expect(rows.map((row) => row.getAttribute("data-delegation-agent")))
      .toEqual(["sre-copilot", "db-expert"]);
    expect(rows.map((row) => row.getAttribute("data-delegation-status")))
      .toEqual(["done", "done"]);
    expect(screen.getByText(/sre-copilot answered/)).toBeTruthy();
    expect(screen.getByText(/db-expert answered/)).toBeTruthy();
  });

  it("keeps two delegations to the same peer as two rows", () => {
    // The trace *lines* are identical, so a plan keyed on the line would report one delegation
    // where two ran. The call id is what the live and the replayed record agree on.
    const plan = collectDelegationPlan([
      planRow([
        delegateEvent("c1", "sre-copilot", "check disk", "end"),
        delegateEvent("c2", "sre-copilot", "check disk", "end"),
      ]),
    ]);
    expect(plan?.steps).toHaveLength(2);
  });

  it("does not also render the delegation as a tool trace inside the fold", () => {
    render(
      <AgentActivityCluster
        messages={[
          REASONING_ROW,
          planRow([delegateEvent("c1", "sre-copilot", "check disk", "end")]),
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(screen.queryByText(/delegate to agent/i)).toBeNull();
    expect(screen.getAllByTestId("delegation-step")).toHaveLength(1);
  });
});

describe("each delegation carries its own cost", () => {
  it("shows the peer's own usage on its row and the plan's total as their sum", () => {
    render(
      <AgentActivityCluster
        messages={[
          planRow([
            delegateEvent("c1", "sre-copilot", "check disk", "end", {
              usage: usage(9_000, 400, 8_000),
            }),
            delegateEvent("c2", "db-expert", "check slow queries", "end", {
              usage: usage(3_000, 100, 2_000),
            }),
          ]),
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    const costs = screen
      .getAllByTestId("delegation-step")
      .map((row) => row.querySelector("[data-delegation-cost]")?.textContent);
    expect(costs).toEqual(["9.0K in · 400 out", "3.0K in · 100 out"]);
    // 12K in, 500 out: the total a reader can check by adding up the rows above it.
    expect(screen.getByTestId("delegation-plan").querySelector("[data-delegation-total]")?.textContent)
      .toBe("delegated 12K in · 83% cached · 500 out");
  });

  it("prints no cost for a delegation that reported none, and never borrows the manager's", () => {
    // `stepUsage` on the row is the *manager's* provider call. Showing it as the peer's cost
    // would attribute one turn's tokens to another.
    const row = planRow([delegateEvent("c1", "sre-copilot", "check disk", "end")]);
    render(
      <AgentActivityCluster
        messages={[{ ...row, stepUsage: usage(21_000, 1_500, 20_000), stepModelMs: 4_000 }]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(screen.getByTestId("delegation-step").querySelector("[data-delegation-cost]")).toBeNull();
    expect(screen.getByTestId("delegation-plan").querySelector("[data-delegation-total]")).toBeNull();
  });
});

describe("partial failure is visible", () => {
  it("does not show a failed delegation as complete", () => {
    render(
      <AgentActivityCluster
        messages={[
          planRow([
            delegateEvent("c1", "sre-copilot", "check disk", "end", {
              usage: usage(9_000, 400),
            }),
            delegateEvent("c2", "db-expert", "check slow queries", "error", {
              error: "Error: the peer could not reach the database",
            }),
          ]),
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    const rows = screen.getAllByTestId("delegation-step");
    const failed = rows.find((row) => row.getAttribute("data-delegation-agent") === "db-expert");
    expect(failed?.getAttribute("data-delegation-status")).toBe("error");
    expect(failed?.getAttribute("data-delegation-status")).not.toBe("done");
    expect(screen.getByText("db-expert failed · the peer could not reach the database")).toBeTruthy();
    // Which was which, on the plan itself.
    expect(screen.getByTestId("delegation-plan").querySelector("[data-delegation-summary]")?.textContent)
      .toBe("1 answered · 1 failed");
    // And the total says it covers one of the two rows, rather than reading as the plan's cost.
    expect(screen.getByTestId("delegation-plan").querySelector("[data-delegation-total]")?.textContent)
      .toBe("delegated 9.0K in · 400 out · 1 of 2 reported");
  });

  it("does not show a delegation that never reported as complete either", () => {
    render(
      <AgentActivityCluster
        messages={[planRow([delegateEvent("c1", "db-expert", "check slow queries", "start")])]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    const row = screen.getByTestId("delegation-step");
    expect(row.getAttribute("data-delegation-status")).toBe("no-answer");
    expect(screen.getByText(/db-expert did not report/)).toBeTruthy();
  });
});

describe("a reload shows what the live turn showed", () => {
  it("reads the same plan out of the replayed record as out of the live frames", () => {
    // The live shape: the client merges the `start` frame and the finish frame by call id.
    const live = collectDelegationPlan([
      planRow([
        delegateEvent("c1", "sre-copilot", "check disk", "end", { usage: usage(9_000, 400, 8_000) }),
        delegateEvent("c2", "db-expert", "check slow queries", "error", {
          error: "Error: the peer could not reach the database",
        }),
      ]),
    ]);

    // The replayed shape, exactly as `replay_transcript_to_ui_messages` emits it -- pinned from
    // the Python side by `tests/utils/test_webui_transcript_delegation_plan.py`.
    const replayed = collectDelegationPlan([
      {
        id: "tr-1",
        role: "tool",
        kind: "trace",
        content: 'delegate_to_agent({"agent": "db-expert", "task": "check slow queries"})',
        traces: [
          'delegate_to_agent({"agent": "sre-copilot", "task": "check disk"})',
          'delegate_to_agent({"agent": "db-expert", "task": "check slow queries"})',
        ],
        toolEvents: [
          {
            version: 1,
            phase: "end",
            call_id: "c1",
            name: "delegate_to_agent",
            arguments: { agent: "sre-copilot", task: "check disk" },
            result: "answered",
            error: null,
            files: [],
            embeds: [],
            usage: usage(9_000, 400, 8_000),
          },
          {
            version: 1,
            phase: "error",
            call_id: "c2",
            name: "delegate_to_agent",
            arguments: { agent: "db-expert", task: "check slow queries" },
            result: null,
            error: "Error: the peer could not reach the database",
            files: [],
            embeds: [],
          },
        ],
        activitySegmentId: "activity-1",
        turnId: "turn-1",
        turnPhase: "activity",
        createdAt: 2,
      },
    ]);

    expect(replayed).toEqual(live);
    expect(replayed?.cost).toEqual({
      steps: 1,
      inputTokens: 9_000,
      outputTokens: 400,
      cachedTokens: 8_000,
      cachedOverInputTokens: 9_000,
    });
  });

  it("reads a plan out of trace lines alone, for a record that carries no structured events", () => {
    const plan = collectDelegationPlan([
      {
        id: "tr-1",
        role: "tool",
        kind: "trace",
        content: 'delegate_to_agent({"agent": "db-expert", "task": "check slow queries"})',
        traces: [
          'delegate_to_agent({"agent": "sre-copilot", "task": "check disk"})',
          'delegate_to_agent({"agent": "db-expert", "task": "check slow queries"})',
        ],
        turnId: "turn-1",
        createdAt: 2,
      },
    ]);
    expect(plan?.steps.map((step) => step.agent)).toEqual(["sre-copilot", "db-expert"]);
  });
});
