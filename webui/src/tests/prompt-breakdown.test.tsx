import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PromptBreakdown } from "@/components/thread/PromptBreakdown";
import type { PromptManifest } from "@/lib/types";

/**
 * The panel exists to answer *where did the input tokens go*, and it kept stopping one question
 * short. It said `builtin TOOLS ×31 — 10,273` and the operator's reply was the obvious one: which
 * thirty-one? A count that cannot be opened is a number that raises a question it will not answer.
 */

function manifest(): PromptManifest {
  return {
    sections: [
      {
        name: "builtin",
        chars: 40_000,
        tokens: 10_273,
        group: "tools",
        items: 3,
        tools: [
          { name: "exec", chars: 4_000, tokens: 1_000 },
          { name: "read_file", chars: 2_000, tokens: 500 },
          { name: "grep", chars: 1_000, tokens: 250 },
        ],
      },
      {
        name: "connector:google-calendar",
        chars: 2_400,
        tokens: 606,
        group: "tools",
        items: 1,
        tools: [{ name: "google_calendar_list_events", chars: 600, tokens: 150 }],
      },
      { name: "Memory", chars: 10_867, tokens: 2_762, group: "system" },
    ],
    groups: { tools: 10_879, system: 2_762 },
    total_tokens: 13_641,
    measured: false,
  };
}

function open() {
  render(<PromptBreakdown manifest={manifest()} />);
  fireEvent.click(screen.getByRole("button", { expanded: false }));
}

describe("which tools", () => {
  it("opens the list from the count that raised the question", () => {
    open();

    fireEvent.click(screen.getByRole("button", { name: "which tools: builtin" }));

    expect(screen.getByText("exec")).toBeTruthy();
    expect(screen.getByText("read_file")).toBeTruthy();
    expect(screen.getByText("grep")).toBeTruthy();
  });

  it("orders them largest first, because that is the one worth trimming", () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "which tools: builtin" }));

    const names = screen
      .getAllByText(/^(exec|read_file|grep)$/)
      .map((element) => element.textContent);

    expect(names).toEqual(["exec", "read_file", "grep"]);
  });

  it("keeps the list closed until asked", () => {
    open();

    expect(screen.queryByText("exec")).toBeNull();
  });

  it("says nothing about tools for a section that is not a tool set", () => {
    open();

    // Memory is one block of text, so `×n` would be a count of nothing.
    expect(screen.getByText("Memory")).toBeTruthy();
  });
});

describe("a tool row's name", () => {
  it("reads as the connector's name, not as its source string", () => {
    // The raw `connector:google-calendar` went straight into the row, and at this size the colon
    // was easy to miss -- it read as one run-together word.
    open();

    expect(screen.getByText("google-calendar")).toBeTruthy();
    expect(screen.queryByText("connector:google-calendar")).toBeNull();
  });

  it("keeps the prefix visible as its own tag", () => {
    open();

    expect(screen.getByText("connector")).toBeTruthy();
  });

  it("leaves a source with no prefix alone", () => {
    open();

    expect(screen.getByText("builtin")).toBeTruthy();
  });
});

describe("the collapsed line", () => {
  it("reads as one line of group shares", () => {
    render(<PromptBreakdown manifest={manifest()} />);

    expect(screen.getByText(/in prompt/)).toBeTruthy();
  });

  it("renders nothing at all for a turn with no manifest total", () => {
    const empty: PromptManifest = {
      sections: [],
      groups: {},
      total_tokens: 0,
      measured: false,
    };

    const { container } = render(<PromptBreakdown manifest={empty} />);

    expect(container.firstChild).toBeNull();
  });
});
