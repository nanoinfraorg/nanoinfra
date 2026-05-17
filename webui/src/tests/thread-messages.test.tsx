import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  assistantCopyFlags,
  buildDisplayUnits,
  ThreadMessages,
} from "@/components/thread/ThreadMessages";
import type { UIMessage } from "@/lib/types";

describe("ThreadMessages", () => {
  it("groups consecutive reasoning and tool rows into one cluster before the answer", () => {
    const messages: UIMessage[] = [
      {
        id: "r1",
        role: "assistant",
        content: "",
        reasoning: "thinking",
        reasoningStreaming: false,
        isStreaming: true,
        createdAt: Date.now(),
      },
      {
        id: "t1",
        role: "tool",
        kind: "trace",
        content: "search()",
        traces: ["search()"],
        createdAt: Date.now(),
      },
      {
        id: "r2",
        role: "assistant",
        content: "",
        reasoning: "more thinking",
        reasoningStreaming: false,
        isStreaming: true,
        createdAt: Date.now(),
      },
      {
        id: "a1",
        role: "assistant",
        content: "final answer",
        createdAt: Date.now(),
      },
    ];

    const { container } = render(
      <ThreadMessages messages={messages} isStreaming={false} />,
    );
    const rows = Array.from(container.firstElementChild?.children ?? []);

    expect(rows).toHaveLength(2);
    expect(rows[0]).not.toHaveClass("mt-2", "mt-4", "mt-5");
    expect(rows[1]).toHaveClass("mt-4");
  });

  it("shows copy only on the last assistant slice before the next user turn", () => {
    const messages: UIMessage[] = [
      {
        id: "early",
        role: "assistant",
        content: "starting…",
        createdAt: 1,
      },
      {
        id: "t1",
        role: "tool",
        kind: "trace",
        content: "search()",
        traces: ["search()"],
        createdAt: 2,
      },
      {
        id: "late",
        role: "assistant",
        content: "final reply",
        createdAt: 3,
      },
    ];

    render(<ThreadMessages messages={messages} isStreaming={false} />);

    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(1);
    expect(screen.getByText("final reply")).toBeInTheDocument();
  });

  it("shows copy only on the second assistant when two text slices appear before user", () => {
    const messages: UIMessage[] = [
      { id: "a1", role: "assistant", content: "part one", createdAt: 1 },
      { id: "a2", role: "assistant", content: "part two", createdAt: 2 },
    ];
    render(<ThreadMessages messages={messages} isStreaming={false} />);
    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(1);
  });

  it("computes final assistant copy flags with user-boundary semantics", () => {
    const units = buildDisplayUnits([
      { id: "u1", role: "user", content: "one", createdAt: 1 },
      { id: "a1", role: "assistant", content: "draft", createdAt: 2 },
      {
        id: "t1",
        role: "tool",
        kind: "trace",
        content: "tool()",
        traces: ["tool()"],
        createdAt: 3,
      },
      { id: "a2", role: "assistant", content: "final", createdAt: 4 },
      { id: "u2", role: "user", content: "two", createdAt: 5 },
      { id: "a3", role: "assistant", content: "next", createdAt: 6 },
    ]);

    const flags = assistantCopyFlags(units);
    const assistantFlags = units
      .map((unit, index) =>
        unit.type === "single" && unit.message.role === "assistant"
          ? [unit.message.id, flags[index]]
          : null,
      )
      .filter(Boolean);

    expect(assistantFlags).toEqual([
      ["a1", false],
      ["a2", true],
      ["a3", true],
    ]);
  });
});
