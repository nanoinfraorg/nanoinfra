import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConnectionBadge } from "@/components/ConnectionBadge";
import type { NanoinfraClient } from "@/lib/nanoinfra-client";
import type { ConnectionStatus } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

/**
 * The identity beside the connection state -- nanoinfraorg/nanoinfra#70.
 *
 * A misconfigured proxy is invisible until an approval fails, and an approval fails at the
 * worst moment. The badge already holds the connection state, so the identity the gateway
 * resolved reads there too, and not on a page an operator has to find.
 *
 * A deployment with no proxy reads as normal. ``webui`` is the true actor there, so the badge
 * states it in the same words and with the same tone as an asserted identity.
 */
function fakeClient(actor: string | null, status: ConnectionStatus = "open"): NanoinfraClient {
  return {
    status,
    operatorActor: actor,
    onStatus: (handler: (value: ConnectionStatus) => void) => {
      handler(status);
      return () => {};
    },
    onOperatorActor: (handler: (value: string | null) => void) => {
      handler(actor);
      return () => {};
    },
  } as unknown as NanoinfraClient;
}

function renderBadge(actor: string | null, status: ConnectionStatus = "open") {
  return render(
    <ClientProvider client={fakeClient(actor, status)} token="tok">
      <ConnectionBadge />
    </ClientProvider>,
  );
}

describe("ConnectionBadge", () => {
  it("names the identity the gateway resolved", () => {
    renderBadge("webui:alberto@example.com");

    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "Connected\nThe gateway reads this path as webui:alberto@example.com."
        + "\nAn approver entry must name that value exactly.",
    );
    expect(screen.getByRole("status")).toHaveTextContent("webui:alberto@example.com");
  });

  it("reads a deployment with no proxy as normal", () => {
    renderBadge("webui");

    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("The gateway reads this path as webui.");
    expect(badge.className).not.toContain("destructive");
  });

  it("says nothing about an identity until the gateway answers", () => {
    renderBadge(null, "connecting");

    expect(screen.getByRole("status")).toHaveAttribute("title", "Connecting…");
  });
});
