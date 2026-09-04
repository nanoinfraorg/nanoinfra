/**
 * The Agents destination and the Abilities grouping -- nanoinfraorg/nanoinfra#253.
 *
 * The property tested hardest here is the negative one. **A deployment that names no agents must
 * see no change at all to its navigation**, and that is every deployment today: the same rows, in
 * the same order, under the same names. A menu that grouped Apps and Skills under a new word, or
 * offered an Agents page listing nothing, would charge every existing operator a re-learn for a
 * feature they do not have.
 *
 * The second property is that the grouping is a *menu* change and nothing else. Apps and Skills
 * keep their pages and their handlers; they move one indent to the right and no link breaks.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import type { NanoinfraClient } from "@/lib/nanoinfra-client";
import type { ConnectionStatus } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

function fakeClient(): NanoinfraClient {
  return {
    status: "open",
    operatorActor: "webui",
    onStatus: (handler: (value: ConnectionStatus) => void) => {
      handler("open");
      return () => {};
    },
    onOperatorActor: (handler: (value: string | null) => void) => {
      handler("webui");
      return () => {};
    },
  } as unknown as NanoinfraClient;
}

/**
 * Every label the rail can offer as a destination.
 *
 * The assertions below compare the *whole ordered list* against this vocabulary rather than
 * checking one row at a time, because "nothing changed" is a statement about the list and a
 * per-row test cannot make it: a row added between two checked rows passes every one of them.
 */
const NAV_LABELS = [
  "New topic",
  "Search",
  "Agents",
  "Apps",
  "Skills",
  "Abilities",
  "Automations",
  "Approvals",
  "Workspaces",
  "Infrastructure",
  "Diagrams",
  "Servers",
  "Secrets",
  "Settings",
];

function renderSidebar(over: Record<string, unknown> = {}) {
  const noop = () => {};
  return render(
    <ClientProvider client={fakeClient()} token="tok">
      <Sidebar
        sessions={[]}
        activeKey={null}
        loading={false}
        newChatActive={false}
        collapsed={false}
        onNewChat={noop}
        onSelect={noop}
        onRequestDelete={noop}
        onTogglePin={noop}
        onRequestRename={noop}
        onToggleArchive={noop}
        onReorderSessions={noop}
        onToggleGroup={noop}
        onRequestRenameProject={noop}
        onNewChatInProject={noop}
        onOpenSettings={noop}
        onOpenApps={noop}
        onOpenSkills={noop}
        onOpenAutomations={noop}
        onOpenDiagrams={noop}
        onOpenWorkspace={noop}
        onOpenServers={noop}
        onOpenSecrets={noop}
        onOpenApprovals={noop}
        onOpenAgents={noop}
        onOpenSearch={noop}
        onToggleArchived={noop}
        onCollapse={noop}
        {...over}
      />
    </ClientProvider>,
  );
}

/** The rail's destinations, in the order they are rendered. */
function destinations(): string[] {
  return screen
    .getAllByRole("button")
    .map((button) => {
      const text = (button.textContent ?? "").trim();
      return NAV_LABELS.find((label) => text.startsWith(label)) ?? null;
    })
    .filter((label): label is string => label !== null);
}

describe("the rail, in one shape whatever the roster holds", () => {
  /*
   * Both the `Agents` destination and the `Abilities` grouping used to appear only for a
   * deployment that named an agent, so that naming none meant *no change at all*. That is the bug
   * these tests replace, and the report was concrete: a fresh install has no named agents, so the
   * rail had no Agents row -- and the first card on that page is the deployment's **own** agent,
   * the one that answers every turn and the only place to narrow the skills and MCP servers each
   * conversation pays for. The surface existed and nothing led to it.
   *
   * A rail whose shape depends on config is also a rail nobody can be shown a screenshot of.
   */
  it("offers Agents whether or not the deployment names one", () => {
    renderSidebar({});

    expect(destinations()).toEqual([
      "New topic",
      "Search",
      "Agents",
      "Automations",
      "Approvals",
      "Abilities",
      "Apps",
      "Skills",
      "Workspaces",
      "Infrastructure",
      "Settings",
    ]);
  });

  it("groups Apps and Skills without hiding them", () => {
    /*
     * The grouping is open by default, and that is the difference between a grouping and a hiding
     * place. Closing it by default would take two destinations out of a rail where they have
     * always been one click away, on every existing deployment, for a reorganisation nobody asked
     * for -- and a heading that costs a click to undo is worse than the flat list it replaced.
     */
    renderSidebar({});

    expect(screen.getByRole("button", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
  });

  it("lets an operator collapse it, which is their choice and not the default", () => {
    renderSidebar({});

    fireEvent.click(screen.getByRole("button", { name: "Abilities" }));

    expect(screen.queryByRole("button", { name: "Apps" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
  });

  it("leaves Approvals at the top level, where the person answering it looks", () => {
    // Filing an approval under agent configuration was considered and rejected: an approval
    // belongs to a *person*, and a turn is suspended while it waits.
    renderSidebar({});

    const rail = destinations();
    expect(rail).toContain("Approvals");
    expect(rail.indexOf("Approvals")).toBeLessThan(rail.indexOf("Abilities"));
  });

  it("opens Apps and Skills from inside the grouping, through the same handlers", () => {
    const onOpenApps = vi.fn();
    const onOpenSkills = vi.fn();
    renderSidebar({ onOpenApps, onOpenSkills });

    fireEvent.click(screen.getByRole("button", { name: "Apps" }));
    fireEvent.click(screen.getByRole("button", { name: "Skills" }));

    // A menu grouping and nothing more: no page moved, so no destination changed.
    expect(onOpenApps).toHaveBeenCalledTimes(1);
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });

  it("keeps the grouping expanded when you are inside it", () => {
    // A reload while on Skills must not hide the page you are looking at behind a collapsed
    // heading -- which stays true now that the default is open, and stays pinned because the
    // default is the kind of thing somebody changes.
    renderSidebar({ activeUtility: "skills" });

    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
  });

  it("routes the Agents row to the Agents destination", () => {
    const onOpenAgents = vi.fn();
    renderSidebar({ onOpenAgents });

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));

    expect(onOpenAgents).toHaveBeenCalledTimes(1);
  });

  it("keeps both destinations reachable in the collapsed rail, without a heading", () => {
    // 56 px has no room for a group label, so the rows stay flat -- the same choice the
    // Infrastructure group already makes.
    renderSidebar({ collapsed: true });

    expect(screen.getByRole("button", { name: "Agents" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Abilities" })).toBeNull();
  });
});
