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
  "Metrics",
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
        onOpenMetrics={noop}
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

    // `Abilities` and `Infrastructure` both start closed as of 2.2.6, so their members are
    // behind their headings until somebody opens them.
    expect(destinations()).toEqual([
      "New topic",
      "Search",
      "Agents",
      "Automations",
      "Approvals",
      "Metrics",
      "Abilities",
      "Workspaces",
      "Infrastructure",
      "Settings",
    ]);
  });

  it("puts Apps and Skills behind a heading that starts closed", () => {
    /*
     * This asserted the opposite until 2.2.6, on the reasoning that closing the group by default
     * would cost every operator a click for a reorganisation nobody asked for. Somebody did ask,
     * and a default is a product decision rather than a derivation. The click is paid once,
     * because opening it is remembered.
     */
    renderSidebar({});

    expect(screen.getByRole("button", { name: "Abilities" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apps" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
  });

  it("lets an operator collapse it, which is their choice and not the default", () => {
    /*
     * The click now records the choice instead of holding it in local state, so the collapse is
     * asserted from the stored map. "The rail groups remember whether you closed them" below
     * covers the handler and the persistence; this keeps the property #253 argued for.
     */
    renderSidebar({ collapsedGroups: { "nav:abilities": true } });

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
    renderSidebar({
      onOpenApps,
      onOpenSkills,
      collapsedGroups: { "nav:abilities": false },
    });

    fireEvent.click(screen.getByRole("button", { name: "Apps" }));
    fireEvent.click(screen.getByRole("button", { name: "Skills" }));

    // A menu grouping and nothing more: no page moved, so no destination changed.
    expect(onOpenApps).toHaveBeenCalledTimes(1);
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });

  it("stays closed even while you are on a page inside it", () => {
    /*
     * It used to reopen itself here, so that a reload would not hide the page you were looking at
     * behind a collapsed heading. That is what a closed default rejects: the page is still on
     * screen in the main area, and a group that reopens on its own is not closed.
     */
    renderSidebar({ activeUtility: "skills" });

    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
    expect(screen.getByRole("button", { name: "Abilities" })).toBeInTheDocument();
  });

  it("routes the Agents row to the Agents destination", () => {
    const onOpenAgents = vi.fn();
    renderSidebar({ onOpenAgents });

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));

    expect(onOpenAgents).toHaveBeenCalledTimes(1);
  });

  it("routes the Metrics row to its own destination, not under Infrastructure", () => {
    // Metrics (#235) sits at the top level on purpose. `Infrastructure` groups what this
    // deployment *manages*; these numbers are about the deployment itself.
    const onOpenMetrics = vi.fn();
    renderSidebar({ onOpenMetrics });

    fireEvent.click(screen.getByRole("button", { name: "Metrics" }));

    expect(onOpenMetrics).toHaveBeenCalledTimes(1);
    const rail = destinations();
    expect(rail.indexOf("Metrics")).toBeLessThan(rail.indexOf("Infrastructure"));
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

describe("the rail groups remember whether you closed them", () => {
  /*
   * They were plain `useState`, so collapsing one lasted until the next reload — and the comment
   * on that state said collapsing "is the operator's choice, not the default". A choice that does
   * not survive a reload is not really one.
   *
   * `collapsed_groups` is the map the sidebar already round-trips to the server for chat project
   * groups. These two just were not using it.
   */
  it("starts closed for a deployment that has never touched it", () => {
    // Absent means closed for both rail groups. This asserted the opposite one commit earlier.
    renderSidebar({ collapsedGroups: {} });

    expect(screen.queryByRole("button", { name: "Apps" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Diagrams" })).toBeNull();
  });

  it("opens Abilities when the operator has opened it before", () => {
    renderSidebar({ collapsedGroups: { "nav:abilities": false } });

    expect(screen.getByRole("button", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
  });

  it("stays closed when the stored state says so explicitly", () => {
    renderSidebar({ collapsedGroups: { "nav:abilities": true } });

    expect(screen.queryByRole("button", { name: "Apps" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
    // The heading is still there — it is a grouping, not a removal.
    expect(screen.getByRole("button", { name: "Abilities" })).toBeInTheDocument();
  });

  it("persists the toggle through the same handler the project groups use", () => {
    const onToggleGroup = vi.fn();
    renderSidebar({ onToggleGroup });

    fireEvent.click(screen.getByRole("button", { name: "Abilities" }));

    expect(onToggleGroup).toHaveBeenCalledWith("nav:abilities");
  });

  it("namespaces its key, because that map is shared with project groups", () => {
    // A bare "abilities" could collide with a project of that name.
    const onToggleGroup = vi.fn();
    renderSidebar({ onToggleGroup });

    fireEvent.click(screen.getByRole("button", { name: "Infrastructure" }));

    expect(onToggleGroup).toHaveBeenCalledWith("nav:infrastructure");
  });

  it("keeps Infrastructure closed by default, which is not the same rule as Abilities", () => {
    /*
     * The regression a single rule caused: "absent means expanded" opened Infrastructure, which
     * has started closed since it existed. Each group keeps its own default, so the two read the
     * map in opposite directions — `true` means collapsed for a default-open group, `false` means
     * expanded for a default-closed one.
     */
    renderSidebar({ collapsedGroups: {} });

    expect(screen.getByRole("button", { name: "Infrastructure" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Diagrams" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Servers" })).toBeNull();
  });

  it("opens Infrastructure when the operator has opened it before", () => {
    renderSidebar({ collapsedGroups: { "nav:infrastructure": false } });

    expect(screen.getByRole("button", { name: "Diagrams" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Servers" })).toBeInTheDocument();
  });

  it("renders Infrastructure open when it holds the page you are on", () => {
    /*
     * The visual guarantee the old effects gave, kept where it was not asked to change.
     * `Abilities` gave it up with the closed default -- see "stays closed even while you are on a
     * page inside it" -- and doing the same to `Infrastructure` unasked would be the overreach
     * that produced persistence when a default flip was wanted.
     */
    renderSidebar({ activeUtility: "servers" });

    expect(screen.getByRole("button", { name: "Servers" })).toBeInTheDocument();
  });

  it("does not rewrite the preference when it overrides it for the active page", () => {
    /*
     * The two effects this replaced called the setter. Against a persisted map that would undo
     * the operator's setting the first time they opened a page inside the group, and a reload
     * would then find it expanded with nobody knowing why. So the override is a render-time read.
     */
    const onToggleGroup = vi.fn();
    renderSidebar({ activeUtility: "servers", onToggleGroup });

    expect(screen.getByRole("button", { name: "Servers" })).toBeInTheDocument();
    expect(onToggleGroup).not.toHaveBeenCalled();
  });
});
