/**
 * The sidebar's identity row.
 *
 * Two rules, and both are about not lying to the reader: a deployment behind a
 * shared token has no person to name, and a deployment whose proxy configured no
 * sign-out route has no way out to offer.
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
        onOpenSettings={noop}
        {...over}
      />
    </ClientProvider>,
  );
}

describe("Sidebar identity", () => {
  it("names the person a proxy asserted, without the path prefix", () => {
    renderSidebar({ identityActor: "webui:alberto@example.com", signOutPath: "/oauth2/sign_out" });
    const actor = screen.getByTestId("sidebar-identity-actor");
    expect(actor).toHaveTextContent("alberto@example.com");
    // The whole string still belongs to Settings, where an approver row is written.
    expect(actor).toHaveAttribute("title", "webui:alberto@example.com");
  });

  it("offers the way out the deployment configured", () => {
    renderSidebar({ identityActor: "webui:alberto@example.com", signOutPath: "/oauth2/sign_out" });
    expect(screen.getByTestId("sidebar-sign-out")).toHaveAttribute("href", "/oauth2/sign_out");
  });

  it("offers no sign-out when the deployment has none", () => {
    renderSidebar({ identityActor: "webui:alberto@example.com", signOutPath: null });
    expect(screen.getByTestId("sidebar-identity")).toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-sign-out")).toBeNull();
  });

  it("shows no identity row when nobody was asserted", () => {
    renderSidebar({ identityActor: null });
    expect(screen.queryByTestId("sidebar-identity")).toBeNull();
  });
});
