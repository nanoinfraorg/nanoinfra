/**
 * The agent roster -- nanoinfraorg/nanoinfra#253.
 *
 * The rule this file exists to keep is one sentence: **the roster says how many, never which.** An
 * agent's tool groups, skills and delegates are its authority, and a *list* that enumerated them
 * would be publishing the authorization model to anyone who can open a settings panel. Counts
 * answer the question a roster is for -- how much does this agent carry -- and answer nothing else.
 * Which of them it carries is the editor's question, asked deliberately, for one agent, in order to
 * change it (#262).
 *
 * The list is **cards** now rather than hairlined rows, and the counts are chips on them: a row in
 * a single list reads as one setting among many, and an agent is an object. The subtitle line and
 * the `model - provider` line under the name come from the product this was matched against.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentRoster } from "@/components/agents/AgentRoster";
import type { NamedAgentRosterEntry, SettingsPayload } from "@/lib/types";

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

const PRESETS: SettingsPayload["model_presets"] = [
  {
    name: "primary",
    label: "Primary",
    active: true,
    is_default: false,
    model: "moonshotai/kimi-k2",
    provider: "auto",
    resolved_provider: "openrouter",
    max_tokens: 8192,
    context_window_tokens: 200_000,
    temperature: 0.1,
    reasoning_effort: null,
  },
];

function promptPayload() {
  return {
    agent: "sre",
    description: "hands-on checks on one host",
    sections: [
      {
        name: "Tool usage notes",
        permission: "replaceable",
        overridden: false,
        present: true,
        static: true,
        tokens: 1_240,
        text: "One tool call per message.",
        platform_text: "One tool call per message.",
        placeholders: [],
        warning: "This is the tool contract.",
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
  it("invites the deployment to name its first agent instead of rendering nothing", () => {
    /*
     * This used to render nothing at all, on the reasoning that a deployment which names no agent
     * has no use for a list of none. That reasoning had the audience backwards: the deployment with
     * no agent is the only one that cannot get one without hand-editing `config.json`, which is
     * exactly what #262 removes.
     */
    render(<AgentRoster agents={[]} token="tok" />);

    expect(screen.getByTestId("agent-roster-empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new agent/i })).toBeInTheDocument();
  });

  it("names each agent, explains it, and says what it is for", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByText("sre")).toBeInTheDocument();
    expect(screen.getByText("hands-on checks on one host")).toBeInTheDocument();
    expect(screen.getByText("Manage AI agents")).toBeInTheDocument();
  });

  it("says which model answers, and not the name config filed it under", () => {
    /*
     * `primary` and `cheap` are names a config file chose; they tell an operator comparing two
     * agents nothing. The model does, and the provider that serves it is the other half of the
     * same answer.
     */
    render(<AgentRoster agents={[entry()]} token="tok" modelPresets={PRESETS} />);

    expect(screen.getByTestId("agent-model-line-sre").textContent).toBe(
      "moonshotai/kimi-k2 · openrouter",
    );
  });

  it("falls back to the preset name when this build cannot resolve it", () => {
    // A preset the payload names and this build does not list: the name is all there is, and it
    // beats an empty line.
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByTestId("agent-model-line-sre").textContent).toBe("primary");
  });

  it("says how many tool groups, skills and delegates an agent carries", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByTestId("agent-count-tool-groups-sre").textContent).toContain("3");
    expect(screen.getByTestId("agent-count-skills-sre").textContent).toContain("2");
    expect(screen.getByTestId("agent-count-delegates-sre").textContent).toContain("1");
  });

  it("counts the MCP servers only when the gateway reports them", () => {
    // There is no `mcp_count` on the wire. A `0` for a payload that carries no list would read as
    // *none bound* when it means *not reported*.
    render(<AgentRoster agents={[entry({ mcp_servers: ["playwright"] })]} token="tok" />);
    expect(screen.getByTestId("agent-count-mcp-sre").textContent).toContain("1");

    render(<AgentRoster agents={[entry({ name: "db" })]} token="tok" />);
    expect(screen.queryByTestId("agent-count-mcp-db")).toBeNull();
  });

  it("says which of them it does not carry, rather than hiding the chip", () => {
    // A missing chip reads as "unknown". A zero reads as "none", which is the fact.
    render(<AgentRoster agents={[entry({ delegate_count: 0 })]} token="tok" />);

    expect(screen.getByTestId("agent-count-delegates-sre").textContent).toContain("0");
  });

  it("says whether the agent's instructions are its own, which is the one status it has", () => {
    /*
     * The reference product puts a status badge here and a named agent has no status: there is no
     * enabled, paused or draft field on `NamedAgentConfig`, so `Active` on every card would be
     * decoration wearing the clothes of data. What the slot holds is a fact the payload carries.
     */
    render(<AgentRoster agents={[entry()]} token="tok" />);
    expect(screen.getByTestId("agent-prompt-source-sre").textContent).toBe("Custom prompt");

    render(<AgentRoster agents={[entry({ name: "db", has_addendum: false })]} token="tok" />);
    expect(screen.getByTestId("agent-prompt-source-db").textContent).toBe("Default prompt");
  });

  it("offers a pencil and a trash on each card, and keeps the confirm on the trash", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByTestId("agent-edit-sre").getAttribute("aria-label")).toBe("Edit sre");
    expect(screen.getByTestId("agent-delete-sre").getAttribute("aria-label")).toBe("Delete sre");
    expect(screen.queryByTestId("agent-delete-confirm")).toBeNull();

    fireEvent.click(screen.getByTestId("agent-delete-sre"));

    expect(screen.getByTestId("agent-delete-confirm")).toBeInTheDocument();
  });

  it("never names a binding, even though the payload now carries them", () => {
    /*
     * The payload grew the lists so the editor could read what it is about to replace (#262), and
     * this is the half that has to stay true anyway: the *row* renders counts because that is what
     * a roster is for, not because the field happened to be absent. The editor is where a binding
     * is named, and it is not open here.
     */
    const withBindings: NamedAgentRosterEntry = {
      ...entry(),
      tool_groups: ["group-omega"],
      delegates: ["db-omega"],
    };

    const { container } = render(<AgentRoster agents={[withBindings]} token="tok" />);

    expect(container.textContent).not.toContain("omega");
  });

  it("offers a way to create an agent, now that there is a write path", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByRole("button", { name: /new agent/i })).toBeInTheDocument();
  });
});

const DEFAULT_AGENT = {
  model: "moonshotai/kimi-k3",
  provider: "auto",
  resolved_provider: "openrouter",
  has_api_key: true,
  model_preset: "kimi-general",
  max_tokens: 8192,
  context_window_tokens: 200_000,
  temperature: 0.1,
  reasoning_effort: null,
  timezone: "UTC",
  tool_hint_max_length: 40,
  max_concurrent_subagents: 1,
};

describe("the agent that answers when nobody picks one", () => {
  it("has a row, because the composer offers it by name", () => {
    /*
     * `agents.defaults` is the one agent every deployment has, and this page listed only
     * `agents.named` -- so the agent that actually answers a message was the only one with no row
     * anywhere, and "¿Cuál se supone es el Default agent?" was a fair question to be left with.
     */
    render(
      <AgentRoster agents={[entry()]} token="tok" defaultAgent={DEFAULT_AGENT} />,
    );

    expect(screen.getByTestId("agent-default-row")).toBeInTheDocument();
    expect(screen.getByText("Default agent")).toBeInTheDocument();
    expect(screen.getByTestId("agent-default-model-line").textContent).toBe(
      "moonshotai/kimi-k3 · openrouter",
    );
  });

  it("is first, because everything below it inherits from it", () => {
    const { container } = render(
      <AgentRoster agents={[entry()]} token="tok" defaultAgent={DEFAULT_AGENT} />,
    );

    const rows = [...container.querySelectorAll("[data-testid^='agent-']")]
      .map((node) => node.getAttribute("data-testid"))
      .filter((id) => id === "agent-default-row" || id === "agent-row-sre");
    expect(rows).toEqual(["agent-default-row", "agent-row-sre"]);
  });

  it("cannot be deleted, because it is not a config entry that can be removed", () => {
    /*
     * It gained a pencil when `agents.defaults` gained a write route (#265) -- its addendum, its
     * replaced sections and its tool groups are genuinely editable now. What it will never gain is
     * a trash: there is no config entry to remove, so a delete could only ever be refused.
     */
    render(
      <AgentRoster agents={[entry()]} token="tok" defaultAgent={DEFAULT_AGENT} />,
    );

    const row = within(screen.getByTestId("agent-default-row"));
    expect(row.queryByLabelText(/^Delete/)).toBeNull();
    expect(row.getByTestId("agent-default-edit")).toBeInTheDocument();
    // The named row still has both.
    expect(screen.getByTestId("agent-delete-sre")).toBeInTheDocument();
  });

  it("says all, not zero, for the lists it declares nothing in", () => {
    render(
      <AgentRoster
        agents={[entry()]}
        token="tok"
        defaultAgent={DEFAULT_AGENT}
        declaredToolGroups={{ servers: {}, diagrams: {}, secrets: {} }}
      />,
    );

    expect(screen.getByTestId("agent-default-count-tool-groups").textContent).toContain("3");
    expect(screen.getByTestId("agent-default-count-skills").textContent).toContain("all");
    expect(screen.getByTestId("agent-default-count-mcp").textContent).toContain("all");
  });

  it("counts its delegates like any agent's, because it has them now", () => {
    /*
     * This row used to say `no delegates` and explain that the default agent *can* have none --
     * `AgentDefaults` had no such field. It has one (#266), and the reason is the shape of the
     * product rather than symmetry: this is the agent you talk to, so an agent that could not
     * hand a database question to `db` made `db` reachable only by stopping talking to it.
     */
    render(
      <AgentRoster agents={[entry()]} token="tok" defaultAgent={DEFAULT_AGENT} />,
    );

    const chip = screen.getByTestId("agent-default-count-delegates");
    expect(chip.textContent).toContain("0 delegates");
    expect(chip.textContent).not.toContain("all");
  });

  it("says none rather than all where a list is declared and empty", () => {
    /*
     * The lie this pins is the one its owner named: *"esto de si no hay nada es todo es una
     * pendejada"*. `all mcp` on an agent narrowed to no MCP server is the page reporting the
     * ceiling as the floor, and on this row it is the worst place for it -- the default agent
     * answers every turn that names nobody.
     */
    render(
      <AgentRoster
        agents={[entry()]}
        token="tok"
        defaultAgent={{ ...DEFAULT_AGENT, mcp_servers: [], skills: ["postgres"] }}
      />,
    );

    expect(screen.getByTestId("agent-default-count-mcp").textContent).toContain("none");
    expect(screen.getByTestId("agent-default-count-skills").textContent).toContain("1");
    // Nothing declared still reads `all`, which is what it actually is.
    expect(screen.getByTestId("agent-default-count-connectors").textContent).toContain("all");
  });

  it("opens its own page, the same one the named agents open", () => {
    // It expanded in place while it was read-only. `agents.defaults` has a write route now
    // (#265), so it goes where an editor goes -- and `agent-defaults-editor.test.tsx` is where
    // that page's rules are pinned.
    render(<AgentRoster agents={[entry()]} token="tok" defaultAgent={DEFAULT_AGENT} />);

    fireEvent.click(screen.getByTestId("agent-default-open"));

    expect(screen.getByTestId("agent-default-detail")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-default-row")).toBeNull();
  });

  it("is there on a fresh install, where it is the only agent there is", () => {
    /*
     * It used to hide until the deployment named an agent, mirroring the composer: `AgentBadge`
     * shows nothing without a roster, so there was no picker entry to explain. That held while
     * this row was a read-only fact, and stopped holding when it became the way in to an editor.
     * On a fresh install `agents.named` is empty -- so the agent that answers every turn, and the
     * only place to narrow the skills and MCP servers a conversation pays for, was reachable only
     * after inventing a named agent you did not want. There is nothing to seed for this: this
     * agent *is* what a deployment has before it names anything.
     */
    render(<AgentRoster agents={[]} token="tok" defaultAgent={DEFAULT_AGENT} />);

    expect(screen.getByTestId("agent-default-row")).toBeInTheDocument();
    // The invitation stays, and now sits under the card it points at rather than instead of it.
    expect(screen.getByTestId("agent-roster-empty")).toBeInTheDocument();
  });
});

describe("a count that is empty because it means everything", () => {
  it("reads all rather than 0, on the two lists where empty is the ceiling", () => {
    /*
     * The defect this pins was on screen: a `db` row reading `Tool groups 0 - Skills 0 -
     * Delegates 0`, which says the agent can do nothing. Two of those three zeroes meant the
     * opposite -- `toolGroups: []` is every group, `skills: []` is every skill, summarised.
     */
    render(
      <AgentRoster
        agents={[entry({ tool_group_count: 0, skill_count: 0, delegate_count: 0 })]}
        token="tok"
      />,
    );

    expect(screen.getByTestId("agent-count-tool-groups-sre").textContent).toContain("all");
    expect(screen.getByTestId("agent-count-skills-sre").textContent).toContain("all");
    // Delegates is the honest zero: an empty list there really is none.
    expect(screen.getByTestId("agent-count-delegates-sre").textContent).toContain("0");
    expect(screen.getByTestId("agent-count-delegates-sre").textContent).not.toContain("all");
  });

  it("keeps the number wherever there is one to keep", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.getByTestId("agent-count-tool-groups-sre").textContent).toContain("3");
    expect(screen.getByTestId("agent-count-skills-sre").textContent).toContain("2");
  });
});

describe("an agent's own page", () => {
  it("stays on the list until an agent is chosen", () => {
    render(<AgentRoster agents={[entry()]} token="tok" />);

    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByTestId("agent-detail")).toBeNull();
  });

  it("opens in the page with a tab strip, not in a dialog", async () => {
    /*
     * An agent has nine config keys across several kinds of question, and one modal holding all
     * of them is a scrolling form whose lower half is below the fold. The strip is plain
     * underlined text rather than the segmented tray this file used to assert: a segmented control
     * picks one of two or three things, and six of them in a rounded tray read as a filter over
     * one page rather than as six pages.
     */
    stubPromptRoute();
    render(<AgentRoster agents={[entry()]} token="tok" />);

    fireEvent.click(screen.getByTestId("agent-open-sre"));

    expect(screen.getByTestId("agent-detail")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Basic",
      "Model",
      "Tools",
      "Skills",
      "Delegates",
      "Prompt",
    ]);
    await waitFor(() => {
      expect(screen.getByTestId("agent-tab-basic")).toBeInTheDocument();
    });
  });

  it("goes back to the list, so two agents are never open at once", async () => {
    stubPromptRoute();
    render(<AgentRoster agents={[entry(), entry({ name: "db" })]} token="tok" />);

    fireEvent.click(screen.getByTestId("agent-open-sre"));
    await waitFor(() => expect(screen.getByTestId("agent-detail")).toBeInTheDocument());
    expect(screen.queryByTestId("agent-open-db")).toBeNull();

    fireEvent.click(screen.getByTestId("agent-detail-back"));

    expect(screen.getByTestId("agent-open-db")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-detail")).toBeNull();
  });
});
