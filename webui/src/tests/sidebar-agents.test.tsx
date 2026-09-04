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

describe("a deployment that names no agents", () => {
  it("sees the navigation it already had", () => {
    renderSidebar({ namedAgentCount: 0 });

    expect(destinations()).toEqual([
      "New topic",
      "Search",
      "Apps",
      "Skills",
      "Automations",
      "Approvals",
      "Workspaces",
      "Infrastructure",
      "Settings",
    ]);
  });

  it("is also what an older gateway looks like, which reports no count at all", () => {
    // `namedAgentCount` is absent until the settings payload arrives, and "absent" has to read as
    // "no agents" rather than as "unknown, so show everything".
    renderSidebar({});

    expect(screen.queryByRole("button", { name: "Agents" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Abilities" })).toBeNull();
  });

  it("keeps Apps and Skills where an operator's muscle memory left them", () => {
    renderSidebar({ namedAgentCount: 0 });

    expect(screen.getByRole("button", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
  });
});

describe("a deployment that names agents", () => {
  it("gains an Agents destination and an Abilities grouping", () => {
    renderSidebar({ namedAgentCount: 2 });

    expect(destinations()).toEqual([
      "New topic",
      "Search",
      "Agents",
      "Automations",
      "Approvals",
      "Abilities",
      "Workspaces",
      "Infrastructure",
      "Settings",
    ]);
  });

  it("leaves Approvals at the top level, where the person answering it looks", () => {
    // Filing an approval under agent configuration was considered and rejected: an approval
    // belongs to a *person*, and a turn is suspended while it waits.
    renderSidebar({ namedAgentCount: 1 });

    const rail = destinations();
    expect(rail).toContain("Approvals");
    expect(rail.indexOf("Approvals")).toBeLessThan(rail.indexOf("Abilities"));
  });

  it("opens Apps and Skills from inside the grouping, through the same handlers", () => {
    const onOpenApps = vi.fn();
    const onOpenSkills = vi.fn();
    renderSidebar({ namedAgentCount: 1, onOpenApps, onOpenSkills });

    fireEvent.click(screen.getByRole("button", { name: "Abilities" }));
    fireEvent.click(screen.getByRole("button", { name: "Apps" }));
    fireEvent.click(screen.getByRole("button", { name: "Skills" }));

    // A menu grouping and nothing more: no page moved, so no destination changed.
    expect(onOpenApps).toHaveBeenCalledTimes(1);
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });

  it("opens the grouping already expanded when you are inside it", () => {
    // A reload while on Skills must not hide the page you are looking at behind a collapsed
    // heading.
    renderSidebar({ namedAgentCount: 1, activeUtility: "skills" });

    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
  });

  it("routes the Agents row to the Agents destination", () => {
    const onOpenAgents = vi.fn();
    renderSidebar({ namedAgentCount: 1, onOpenAgents });

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));

    expect(onOpenAgents).toHaveBeenCalledTimes(1);
  });

  it("keeps both destinations reachable in the collapsed rail, without a heading", () => {
    // 56 px has no room for a group label, so the rows stay flat -- the same choice the
    // Infrastructure group already makes.
    renderSidebar({ namedAgentCount: 1, collapsed: true });

    expect(screen.getByRole("button", { name: "Agents" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Abilities" })).toBeNull();
  });
});
