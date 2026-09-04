/**
 * The agent roster -- nanoinfraorg/nanoinfra#253.
 *
 * The rule this file exists to keep is one sentence: **the roster says how many, never which.** An
 * agent's tool groups, skills and delegates are its authority, decided in a config file a human
 * reviews, and a page that enumerated them would be publishing the authorization model to anyone
 * who can open a settings panel. Counts answer the question a roster is for -- how much does this
 * agent carry -- and answer nothing else.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentRoster } from "@/components/agents/AgentRoster";
import type { NamedAgentRosterEntry } from "@/lib/types";

function entry(over: Partial<NamedAgentRosterEntry> = {}): NamedAgentRosterEntry {
  return {
    name: "sre",
    description: "hands-on checks on one host",
    model_preset: "primary",
    tool_group_count: 3,
    skill_count: 2,
    delegate_count: 1,
    has_addendum: true,
    ...over,
  };
}

function promptPayload() {
  return {
    agent: "sre",
    description: "hands-on checks on one host",
    sections: [
      {
        name: "Tool usage notes",
        permission: "fixed",
        overridden: false,
        present: true,
        static: true,
        tokens: 1_240,
      },
    ],
    addendum: "Prefer read-only checks.",
    measured: false,
  };
}

function stubPromptRoute(body: unknown = promptPayload()): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as Response),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the roster", () => {
  it("renders nothing at all when the deployment names no agents", () => {
    // Which is every deployment today, and the reason the panel above it is unchanged for them.
    const { container } = render(<AgentRoster agents={[]} token="tok" />);

    expect(container.textContent).toBe("");
    expect(screen.queryByTestId("agent-roster")).toBeNull();
  });

  it("names each agent, explains it, and says which model answers", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByText("sre")).toBeInTheDocument();
    expect(screen.getByText("hands-on checks on one host")).toBeInTheDocument();
    expect(screen.getByText("primary")).toBeInTheDocument();
  });

  it("says how many tool groups, skills and delegates an agent carries", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByTestId("agent-count-tool-groups-sre").textContent).toContain("3");
    expect(screen.getByTestId("agent-count-skills-sre").textContent).toContain("2");
    expect(screen.getByTestId("agent-count-delegates-sre").textContent).toContain("1");
    expect(screen.getByTestId("agent-has-addendum-sre")).toBeInTheDocument();
  });

  it("says which of them it does not carry, rather than hiding the row", () => {
    // A missing row reads as "unknown". A zero reads as "none", which is the fact.
    render(<AgentRoster agents={[entry({ delegate_count: 0, has_addendum: false })]} token="tok" />);

    expect(screen.getByTestId("agent-count-delegates-sre").textContent).toContain("0");
    expect(screen.queryByTestId("agent-has-addendum-sre")).toBeNull();
  });

  it("never names a binding, even if a payload starts carrying them", () => {
    /*
     * The server keeps the bindings out of the payload, and this pins the other half: the roster
     * renders counts because that is what it reads, not because the field happened to be absent.
     * A future payload that grew a `tool_groups` array would not leak it through this page.
     */
    const withBindings = {
      ...entry(),
      tool_groups: ["group-omega"],
      delegates: ["db-omega"],
    } as unknown as NamedAgentRosterEntry;

    const { container } = render(<AgentRoster agents={[withBindings]} token="tok" />);

    expect(container.textContent).not.toContain("omega");
  });

  it("offers no way to create an agent, because there is no write path", () => {
    // An agent is declared where authority lives. A button opening a form the gateway will not
    // accept is a worse answer than no button.
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.queryByRole("button", { name: /new agent/i })).toBeNull();
  });
});

describe("an agent's own page", () => {
  it("stays closed until an agent is chosen", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("opens the Prompt tab from the roster row", async () => {
    stubPromptRoute();
    render(<AgentRoster agents={[entry()]} token="tok" />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByRole("tab", { name: "Prompt" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("agent-prompt-sections")).toBeInTheDocument();
    });
    expect(screen.getByText("Tool usage notes")).toBeInTheDocument();
  });

  it("closes again on a second click, so two agents are never open at once", async () => {
    stubPromptRoute();
    render(<AgentRoster agents={[entry(), entry({ name: "db" })]} token="tok" />);

    fireEvent.click(screen.getByTestId("agent-row-sre").querySelector("button")!);
    await waitFor(() => {
      expect(screen.getByTestId("agent-prompt-sections")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("agent-row-db").querySelector("button")!);
    await waitFor(() => {
      expect(screen.getByTestId("agent-prompt-sections")).toBeInTheDocument();
    });

    expect(screen.getAllByRole("tab")).toHaveLength(1);
  });
});
