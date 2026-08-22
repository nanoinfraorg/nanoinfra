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

function renderSidebar(props: { version?: string; collapsed?: boolean; docsUrl?: string }) {
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
        docsUrl={props.docsUrl}
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
  it("links the name, the docs and the release, in that order", () => {
    renderSidebar({ version: "0.15.4" });

    expect(screen.getByRole("link", { name: "nanoinfra" })).toHaveAttribute(
      "href",
      "https://nanoinfra.org/",
    );
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute(
      "href",
      "https://docs.nanoinfra.org",
    );
    const release = screen.getByRole("link", { name: "v0.15.4" });
    expect(release).toHaveAttribute(
      "href",
      "https://github.com/nanoinfraorg/nanoinfra/releases/tag/v0.15.4",
    );
    // A new tab, and no referrer: the sidebar is not a navigation the reader wants to lose.
    expect(release).toHaveAttribute("target", "_blank");
    expect(release).toHaveAttribute("rel", "noreferrer");
  });

  it("takes the docs host from the settings payload rather than assuming one", () => {
    renderSidebar({ version: "0.15.4", docsUrl: "https://docs.internal.example" });

    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute(
      "href",
      "https://docs.internal.example",
    );
  });

  it("shows a local build as text, and still links the name and the docs", () => {
    renderSidebar({ version: "0.16.0.dev3+g1a2b3c4" });

    expect(screen.getByText("v0.16.0.dev3+g1a2b3c4")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /^v0\.16\.0/ }),
    ).not.toBeInTheDocument();
    // The two that do not depend on a published release stay.
    expect(screen.getByRole("link", { name: "nanoinfra" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "docs" })).toBeInTheDocument();
  });

  it("shows nothing before the settings payload arrives", () => {
    renderSidebar({});

    expect(screen.queryByRole("link", { name: "nanoinfra" })).not.toBeInTheDocument();
  });

  it("leaves the collapsed rail alone", () => {
    // 56 px of width, where every other label is hidden too.
    renderSidebar({ version: "0.15.4", collapsed: true });

    expect(screen.queryByRole("link", { name: "nanoinfra" })).not.toBeInTheDocument();
  });
});
