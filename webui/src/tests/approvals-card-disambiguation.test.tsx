/**
 * Telling one pending approval from the next one (nanoinfraorg/nanoinfra#258).
 *
 * The maintainer looked at a waiting approval and asked which agent was asking: "si llegan dos
 * similares se puede confundir". Two similar requests were distinguishable only by a session
 * uuid, which is not something a person compares. Two answers, to two different questions:
 *
 * - **who will run it** -- the acting-agent row, present on every card of a deployment that names
 *   agents, naming the default agent when nothing named itself;
 * - **which conversation asked** -- the Session row as a link to that thread, carrying its title
 *   where the shell knows one.
 *
 * Both stay below the context divider, under the note that the digest does not cover those lines,
 * and the deployment that names no agent must render byte for byte what it rendered before.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApprovalsView, type PendingApprovalEntry } from "@/components/approvals/ApprovalsView";
import type { ChatSummary } from "@/lib/types";

const DIGEST = "sha256:42459a566eadc21441aa08728e6fe0cdc5fc3c3f0e8e362746fc398a97ba618e";
const SESSION = "websocket:40dfb243-69db-40f9-b334-0ef52c3bc49d";
const AGENT_LABEL = "Acting agent";
const ASSERTED = /The agent named itself/;
const DEFAULT_NOTE = /No agent named itself/;
const DIVIDER = "The lines below are context. The digest does not cover them.";

function pendingAction(over: Partial<PendingApprovalEntry> = {}): PendingApprovalEntry {
  return {
    capabilityClass: "mutate.remote",
    executionContext: "interactive",
    expiresAt: Date.now() + 107_000,
    expiresInS: 107,
    hostCount: 1,
    hosts: ["web-01"],
    originPath: "webui",
    payload: "nanoinfra approval request v1\n  | systemctl restart nginx",
    requestId: "req-1",
    samePath: false,
    scope: "group",
    sessionId: SESSION,
    targetDigest: DIGEST,
    ...over,
  };
}

function session(over: Partial<ChatSummary> = {}): ChatSummary {
  return {
    channel: "websocket",
    chatId: "40dfb243-69db-40f9-b334-0ef52c3bc49d",
    createdAt: null,
    key: SESSION,
    preview: "",
    updatedAt: null,
    ...over,
  };
}

function view(over: Partial<Parameters<typeof ApprovalsView>[0]> = {}) {
  return (
    <ApprovalsView
      answering={null}
      degraded={false}
      loading={false}
      onAnswer={vi.fn()}
      outcome={null}
      pending={[pendingAction()]}
      unavailable={false}
      {...over}
    />
  );
}

describe("the approvals card names the acting agent", () => {
  it("adds nothing at all on a deployment that names no agents", () => {
    render(view({ pending: [pendingAction({ agentsConfigured: false })] }));

    // No roster, no question to answer: the screen is what it is today, and neither the row nor
    // its caveat appears.
    expect(screen.queryByText(AGENT_LABEL)).not.toBeInTheDocument();
    expect(screen.queryByText(ASSERTED)).not.toBeInTheDocument();
    expect(screen.queryByText(DEFAULT_NOTE)).not.toBeInTheDocument();
    // And the rows that were there are still there.
    expect(screen.getByText("Requested on")).toBeInTheDocument();
    expect(screen.getByText("Session")).toBeInTheDocument();
    expect(screen.getByText("Binding digest")).toBeInTheDocument();
  });

  it("names the default agent when a deployment with a roster names none", () => {
    render(
      view({ pending: [pendingAction({ actingAgent: null, agentsConfigured: true })] }),
    );

    // A roster makes "the default agent" a known fact rather than a missing one, so the row is
    // there and it says which agent that is.
    expect(screen.getByText(AGENT_LABEL)).toBeInTheDocument();
    expect(screen.getByText("Default agent")).toBeInTheDocument();
    expect(screen.getByText(DEFAULT_NOTE)).toBeInTheDocument();
  });

  it("names the peer that will act and the agent that delegated to it", () => {
    render(
      view({
        pending: [
          pendingAction({ actingAgent: "sre-prod", agentsConfigured: true, delegatedBy: "manager" }),
        ],
      }),
    );

    expect(screen.getByText(AGENT_LABEL)).toBeInTheDocument();
    expect(screen.getByText("sre-prod — delegated by manager")).toBeInTheDocument();
    // The name is the request's own claim, and the row says so.
    expect(screen.getByText(ASSERTED)).toBeInTheDocument();
  });

  it("keeps the agent row below the divider the digest does not cover", () => {
    render(
      view({ pending: [pendingAction({ actingAgent: "sre-prod", agentsConfigured: true })] }),
    );

    const divider = screen.getByText(DIVIDER);
    const row = screen.getByText(AGENT_LABEL);
    // Node.DOCUMENT_POSITION_FOLLOWING: the row comes after the caveat, never above it.
    expect(divider.compareDocumentPosition(row) & 4).toBe(4);
  });
});

describe("the approvals card links to the conversation that asked", () => {
  it("points the session row at that thread's own address", () => {
    render(view());

    const link = screen.getByTestId("approval-session-link");
    expect(link).toHaveAttribute(
      "href",
      "#/chat/websocket%3A40dfb243-69db-40f9-b334-0ef52c3bc49d",
    );
  });

  it("shows the conversation's title and keeps the raw key one hover away", () => {
    render(view({ sessions: [session({ title: "nginx restart on web-01" })] }));

    const link = screen.getByTestId("approval-session-link");
    expect(link).toHaveTextContent("nginx restart on web-01");
    // The key is what the audit record holds, so it stays reachable from the card.
    expect(link).toHaveAttribute("title", SESSION);
  });

  it("prefers the name the operator gave the conversation over the generated one", () => {
    render(
      view({
        sessionTitleOverrides: { [SESSION]: "Friday deploy" },
        sessions: [session({ title: "nginx restart on web-01" })],
      }),
    );

    expect(screen.getByTestId("approval-session-link")).toHaveTextContent("Friday deploy");
  });

  it("shows the raw session key when the shell knows no title for it", () => {
    // A session the sidebar has not loaded, or one it no longer holds. A placeholder title would
    // replace the one string that identifies the thread with one that identifies nothing.
    render(view({ sessions: [session({ key: "websocket:another" })] }));

    expect(screen.getByTestId("approval-session-link")).toHaveTextContent(SESSION);
  });

  it("keeps the link below the divider, because a link is context and not signed bytes", () => {
    render(view({ sessions: [session({ title: "nginx restart on web-01" })] }));

    const divider = screen.getByText(DIVIDER);
    const link = screen.getByTestId("approval-session-link");
    expect(divider.compareDocumentPosition(link) & 4).toBe(4);
  });
});
