/**
 * The running build, under Settings.
 *
 * Two cases, and the second is the reason the first is not just a link. A released version links
 * to its notes. A local build carries a version this project never published -- a source checkout
 * reports something like `0.16.0.dev3+g1a2b3c4` -- and linking that would send a reader to a 404,
 * so it stays text that still answers "which build is this?".
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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

function renderSidebar(props: { version?: string; collapsed?: boolean }) {
  const noop = () => {};
  return render(
    <ClientProvider client={fakeClient()} token="tok">
      <Sidebar
        sessions={[]}
        activeKey={null}
        loading={false}
        newChatActive={false}
        collapsed={props.collapsed ?? false}
        version={props.version}
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
        onOpenServers={noop}
        onOpenSecrets={noop}
        onOpenApprovals={noop}
        onOpenSearch={noop}
        onToggleArchived={noop}
        onCollapse={noop}
        onExpand={noop}
      />
    </ClientProvider>,
  );
}

describe("Sidebar version", () => {
  it("links a released version to its own release notes", () => {
    renderSidebar({ version: "0.15.3" });

    const link = screen.getByRole("link", { name: "nanoinfra 0.15.3" });
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/nanoinfraorg/nanoinfra/releases/tag/v0.15.3",
    );
    // A new tab, and no referrer: the sidebar is not a navigation the reader wants to lose.
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("shows a local build as text rather than a link to a page that does not exist", () => {
    renderSidebar({ version: "0.16.0.dev3+g1a2b3c4" });

    expect(screen.getByText("nanoinfra 0.16.0.dev3+g1a2b3c4")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /nanoinfra/ })).not.toBeInTheDocument();
  });

  it("shows nothing before the settings payload arrives", () => {
    renderSidebar({});

    expect(screen.queryByText(/^nanoinfra /)).not.toBeInTheDocument();
  });

  it("leaves the collapsed rail alone", () => {
    // 56 px of width, where every other label is hidden too.
    renderSidebar({ version: "0.15.3", collapsed: true });

    expect(screen.queryByRole("link", { name: /nanoinfra/ })).not.toBeInTheDocument();
  });
});
