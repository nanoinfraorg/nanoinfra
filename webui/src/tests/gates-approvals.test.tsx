import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApprovalsView } from "@/components/approvals/ApprovalsView";
import { useApprovals } from "@/hooks/useApprovals";
import type {
  GatesApprovalAnswer,
  GatesApprovalsPayload,
  GatesPendingApprovalView,
} from "@/lib/types";

const READ_PATH = "/api/webui/gates/approvals";
const ANSWER_PATH = "/api/webui/gates/approvals/answer";
const DIGEST = "sha256:42459a566eadc21441aa08728e6fe0cdc5fc3c3f0e8e362746fc398a97ba618e";
const HOSTS = Array.from({ length: 14 }, (_, index) => `web-${String(index + 1).padStart(2, "0")}`);
const PAYLOAD = [
  "nanoinfra approval request v1",
  "The executor resolved this request. No part of it comes from the agent.",
  "",
  "Command, exactly as the executor will run it:",
  "  | systemctl restart nginx",
  "",
  "Hosts: 14",
  ...HOSTS.map((host, index) => `  ${String(index + 1).padStart(2, " ")}. ${host}`),
  "",
  `Binding digest: ${DIGEST}`,
].join("\n");

function pendingAction(over: Partial<GatesPendingApprovalView> = {}): GatesPendingApprovalView {
  return {
    capabilityClass: "mutate.remote",
    executionContext: "interactive",
    expiresAt: Date.now() + 107_000,
    expiresInS: 107,
    hostCount: 14,
    hosts: HOSTS,
    originPath: "telegram",
    payload: PAYLOAD,
    requestId: "req-1",
    samePath: false,
    scope: "group",
    sessionId: "telegram:chat-1",
    targetDigest: DIGEST,
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

function answer(over: Partial<GatesApprovalAnswer> = {}): GatesApprovalAnswer {
  return {
    actor: "webui",
    decision: "approve",
    degraded: false,
    error: null,
    ok: true,
    refusal: null,
    requestId: "req-1",
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function payload(over: Partial<GatesApprovalsPayload> = {}): GatesApprovalsPayload {
  // The wire carries a remaining time. The hook is what turns it into a deadline.
  const { expiresAt, ...wire } = pendingAction();
  void expiresAt;
  return {
    approvalPath: "webui",
    count: 1,
    degraded: false,
    pending: [wire],
    ...over,
  };
}

describe("ApprovalsView", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("renders the executor payload byte for byte", () => {
    render(view());

    // The default matcher collapses whitespace. The payload is the thing the digest covers, so
    // this match keeps every byte, including the line breaks and the column alignment.
    expect(screen.getByText(PAYLOAD, { normalizer: (value) => value })).toBeInTheDocument();
  });

  it("shows every resolved host name and the count", () => {
    render(view());

    expect(screen.getByText("Hosts: 14")).toBeInTheDocument();
    for (const host of ["web-01", "web-07", "web-14"]) {
      expect(screen.getAllByText(new RegExp(host)).length).toBeGreaterThan(0);
    }
  });

  it("names the origin path, the approval path, and the session", () => {
    render(view());

    expect(screen.getByText("telegram")).toBeInTheDocument();
    expect(screen.getByText("webui (this session, authenticated)")).toBeInTheDocument();
    expect(screen.getByText("telegram:chat-1")).toBeInTheDocument();
  });

  it("keeps the context lines outside the approved payload", () => {
    render(view());

    expect(
      screen.getByText("The lines below are context. The digest does not cover them."),
    ).toBeInTheDocument();
  });

  it("counts the remaining time down and says what happens at zero", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-15T00:00:00Z"));

    render(view({ pending: [pendingAction({ expiresAt: Date.now() + 107_000 })] }));

    expect(screen.getByText("1:47 left")).toBeInTheDocument();
    expect(
      screen.getByText("At zero the executor refuses this action. The agent gets no retry."),
    ).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(screen.getByText("1:46 left")).toBeInTheDocument();
  });

  it("refuses an approval after the deadline passes", () => {
    render(view({ pending: [pendingAction({ expiresAt: Date.now() - 1_000 })] }));

    expect(screen.getByText("This action expired. The executor refused it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("approves with the digest of the payload it displayed", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();

    render(view({ onAnswer }));
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(onAnswer).toHaveBeenCalledWith({
      decision: "approve",
      requestId: "req-1",
      targetDigest: DIGEST,
    });
  });

  it("denies in one click and asks for nothing else", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();

    render(view({ onAnswer }));
    await user.click(screen.getByRole("button", { name: "Deny" }));

    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith({ decision: "deny", requestId: "req-1" });
  });

  it("offers no approve control for a request from this path, and says why", () => {
    render(view({ pending: [pendingAction({ originPath: "webui", samePath: true })] }));

    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled();
    expect(
      screen.getByText(/This request came from the path you are on/),
    ).toBeInTheDocument();
  });

  it("says a denial is terminal", () => {
    render(view());

    expect(screen.getByText(/A denial is terminal for this session/)).toBeInTheDocument();
  });

  it("reports an empty queue without reading as an open gate", () => {
    render(view({ pending: [] }));

    expect(screen.getByText(/No action waits for an answer/)).toBeInTheDocument();
    expect(screen.getByText(/Policy still refuses/)).toBeInTheDocument();
  });

  it("reports an unreachable executor instead of an empty queue", () => {
    render(view({ degraded: true, pending: [] }));

    expect(screen.getByText(/The gateway cannot reach the executor/)).toBeInTheDocument();
  });

  it("reports a gateway with no inbox", () => {
    render(view({ pending: [], unavailable: true }));

    expect(screen.getByText(/This gateway holds no approvals inbox/)).toBeInTheDocument();
  });

  it("says the executor ran the action after an approval", () => {
    render(view({ outcome: answer(), pending: [] }));

    expect(screen.getByText(/You approved this action/)).toBeInTheDocument();
  });

  it("says the denial is terminal after a denial", () => {
    render(view({ outcome: answer({ decision: "deny" }), pending: [] }));

    expect(screen.getByText(/You denied this action/)).toBeInTheDocument();
  });

  it.each([
    ["same_path", /Answer it from another authenticated path/],
    ["digest_mismatch", /Your answer covers other bytes/],
    ["already_answered", /One action takes one answer/],
    ["expired", /This action expired before your answer arrived/],
    ["unknown_request", /The executor does not hold this request/],
    ["not_an_approver", /gates.approvers/],
    ["unauthenticated_path", /gates.approvalPaths/],
    ["no_second_path", /Add a second path/],
    ["unknown_origin_path", /The request names no origin path/],
  ])("reads the %s refusal as text an operator can act on", (refusal, sentence) => {
    render(view({ outcome: answer({ ok: false, refusal }) }));

    expect(screen.getByText(sentence)).toBeInTheDocument();
  });

  it("falls back to the executor sentence for a refusal it does not know", () => {
    render(
      view({ outcome: answer({ error: "the executor failed this answer", ok: false, refusal: "brand_new" }) }),
    );

    expect(screen.getByText(/the executor failed this answer/)).toBeInTheDocument();
  });

  it("says the action still waits when the gateway sent no answer", () => {
    render(view({ outcome: answer({ degraded: true, ok: false }) }));

    expect(screen.getByText(/The action still waits/)).toBeInTheDocument();
  });
});

// -- approve and add (nanoinfraorg/nanoinfra#220) --------------------------------------------

/** Open the caret of the split button. Radix opens a menu on pointerdown, not on click. */
async function openGrantMenu() {
  fireEvent.pointerDown(
    screen.getByRole("button", { name: "Approve and add a standing grant" }),
    { button: 0 },
  );
  return screen.findByRole("menu");
}

describe("ApprovalsView: approve and add", () => {
  it("keeps the bare click a plain approve that grants nothing", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();

    render(view({ onAnswer }));
    await user.click(screen.getByRole("button", { name: "Approve" }));

    // The default action of a split button is the one people press without reading.
    expect(onAnswer).toHaveBeenCalledWith({
      decision: "approve",
      requestId: "req-1",
      targetDigest: DIGEST,
    });
  });

  it.each([
    ["Approve and add — expires in 24 hours", "24h"],
    ["Approve and add — expires in 7 days", "7d"],
  ])("approves and adds a grant that expires (%s)", async (label, expires) => {
    const onAnswer = vi.fn();

    render(view({ onAnswer }));
    await openGrantMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: label }));

    expect(onAnswer).toHaveBeenCalledWith({
      decision: "approve",
      grant: { expires },
      requestId: "req-1",
      targetDigest: DIGEST,
    });
  });

  it("asks once more before a grant that never expires", async () => {
    const onAnswer = vi.fn();

    render(view({ onAnswer }));
    await openGrantMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "Approve and add — never expires" }),
    );

    // Nothing was answered yet. This is the only option a click makes permanent.
    expect(onAnswer).not.toHaveBeenCalled();
    expect(await screen.findByText(/Add a grant that never expires\?/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Yes, never expires" }));

    expect(onAnswer).toHaveBeenCalledWith({
      decision: "approve",
      grant: { expires: "never", permanentAcknowledged: true },
      requestId: "req-1",
      targetDigest: DIGEST,
    });
  });

  it("answers nothing when the permanent confirmation is cancelled", async () => {
    const onAnswer = vi.fn();

    render(view({ onAnswer }));
    await openGrantMenu();
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "Approve and add — never expires" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("offers no grant control where it offers no approve control", () => {
    render(view({ pending: [pendingAction({ originPath: "webui", samePath: true })] }));

    expect(
      screen.queryByRole("button", { name: "Approve and add a standing grant" }),
    ).not.toBeInTheDocument();
  });

  it("offers no grant control after the deadline passes", () => {
    render(view({ pending: [pendingAction({ expiresAt: Date.now() - 1_000 })] }));

    expect(
      screen.queryByRole("button", { name: "Approve and add a standing grant" }),
    ).not.toBeInTheDocument();
  });

  it("names the grant it wrote and the date it stops", () => {
    render(
      view({
        outcome: answer({
          grant: {
            expiresAt: "2026-09-04T10:00:00Z",
            id: "approval-2026-09-03-systemctl-1a2b3c",
            ok: true,
            reason: null,
          },
        }),
        pending: [],
      }),
    );

    expect(screen.getByText(/approval-2026-09-03-systemctl-1a2b3c/)).toBeInTheDocument();
  });

  it("says a permanent grant never expires", () => {
    render(
      view({
        outcome: answer({
          grant: { expiresAt: null, id: "approval-2026-09-03-systemctl-1a2b3c", ok: true, reason: null },
        }),
        pending: [],
      }),
    );

    expect(screen.getByText(/never expires/)).toBeInTheDocument();
  });

  it("reports an approval that stands beside a grant that was not saved", () => {
    render(
      view({
        outcome: answer({
          grant: {
            expiresAt: null,
            id: null,
            ok: false,
            reason: "The grant was not saved: Read-only file system",
          },
        }),
        pending: [],
      }),
    );

    // Both facts, because they happened in two processes and either one can fail alone.
    expect(screen.getByText(/You approved this action/)).toBeInTheDocument();
    expect(screen.getByText(/Read-only file system/)).toBeInTheDocument();
  });

  it("says nothing about a grant when the answer asked for none", () => {
    render(view({ outcome: answer(), pending: [] }));

    expect(screen.queryByText(/Standing grant/)).not.toBeInTheDocument();
    expect(screen.queryByText(/was not saved/)).not.toBeInTheDocument();
  });
});

// -- the acting agent (nanoinfraorg/nanoinfra#258) --------------------------------------------

/**
 * An approval prompt must say which agent will act.
 *
 * Without it an operator approving a command reads the request and cannot tell whether the
 * manager or one of its peers runs it, and with delegation those are two different blast radii.
 *
 * The harder half is the absence. Every deployment today names no agent, so a card with no
 * attribution has to look exactly as it looked before this field existed -- and a blank name has
 * to read as no name rather than as one.
 */
describe("ApprovalsView: the acting agent", () => {
  const AGENT_LABEL = "Acting agent";
  const ASSERTED = /The agent named itself/;

  it("names the peer that will act and the agent that delegated to it", () => {
    render(view({ pending: [pendingAction({ actingAgent: "sre-prod", delegatedBy: "manager" })] }));

    expect(screen.getByText(AGENT_LABEL)).toBeInTheDocument();
    expect(screen.getByText("sre-prod — delegated by manager")).toBeInTheDocument();
  });

  it("names the agent alone when nothing delegated to it", () => {
    render(view({ pending: [pendingAction({ actingAgent: "sre-prod" })] }));

    expect(screen.getByText("sre-prod")).toBeInTheDocument();
    expect(screen.queryByText(/delegated by/)).not.toBeInTheDocument();
  });

  it("says the name is a claim of the request rather than an authenticated identity", () => {
    render(view({ pending: [pendingAction({ actingAgent: "sre-prod", delegatedBy: "manager" })] }));

    expect(screen.getByText(ASSERTED)).toBeInTheDocument();
  });

  it("renders exactly today's card when no agent is named", () => {
    render(view());

    // The default fixture is a deployment that does not delegate, which is every deployment.
    expect(screen.queryByText(AGENT_LABEL)).not.toBeInTheDocument();
    expect(screen.queryByText(ASSERTED)).not.toBeInTheDocument();
    // And nothing else moved: the rows that were there are still there.
    expect(screen.getByText("Requested on")).toBeInTheDocument();
    expect(screen.getByText("telegram:chat-1")).toBeInTheDocument();
  });

  it.each([null, undefined, "", "   "])("reads %p as no agent at all", (claimed) => {
    render(view({ pending: [pendingAction({ actingAgent: claimed, delegatedBy: "manager" })] }));

    // Absent attribution renders nothing rather than a guess, and a manager with no peer named
    // is not a peer.
    expect(screen.queryByText(AGENT_LABEL)).not.toBeInTheDocument();
    expect(screen.queryByText(/manager/)).not.toBeInTheDocument();
  });

  it("keeps a blank coordinator from reading as a delegation", () => {
    render(view({ pending: [pendingAction({ actingAgent: "sre-prod", delegatedBy: "  " })] }));

    expect(screen.getByText("sre-prod")).toBeInTheDocument();
    expect(screen.queryByText(/delegated by/)).not.toBeInTheDocument();
  });
});

describe("useApprovals", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads the queue and reports the unread count", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(payload())));

    const { result } = renderHook(() => useApprovals(() => "tok"));

    await waitFor(() => expect(result.current.count).toBe(1));
    expect(result.current.pending[0].requestId).toBe("req-1");
    expect(fetch).toHaveBeenCalledWith(
      READ_PATH,
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
    );
  });

  it("turns the remaining time into a deadline the view counts down", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(payload())));

    const { result } = renderHook(() => useApprovals(() => "tok"));

    await waitFor(() => expect(result.current.pending.length).toBe(1));
    expect(result.current.pending[0].expiresAt).toBeGreaterThan(Date.now());
  });

  it("answers on the operator route and then reads the queue again", async () => {
    const calls: { url: string; values: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        calls.push({ url, values: headers["X-Nanoinfra-Approval-Values"] ?? null });
        if (url === ANSWER_PATH) return jsonResponse(answer());
        return jsonResponse(payload({ count: calls.length > 1 ? 0 : 1, pending: [] }));
      }),
    );

    const { result } = renderHook(() => useApprovals(() => "tok"));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await act(async () => {
      await result.current.answer({
        decision: "approve",
        requestId: "req-1",
        targetDigest: DIGEST,
      });
    });

    const answerCall = calls.find((call) => call.url === ANSWER_PATH);
    expect(answerCall?.values).toBe(
      JSON.stringify({ decision: "approve", requestId: "req-1", targetDigest: DIGEST }),
    );
    expect(result.current.outcome?.ok).toBe(true);
    expect(calls.filter((call) => call.url === READ_PATH).length).toBeGreaterThan(1);
  });

  it("reports a gateway with no inbox instead of an empty queue", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ error: "no" }, 503)));

    const { result } = renderHook(() => useApprovals(() => "tok"));

    await waitFor(() => expect(result.current.unavailable).toBe(true));
    expect(result.current.count).toBe(0);
    expect(result.current.degraded).toBe(false);
  });

  it("reports a failed answer that never reached the executor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === ANSWER_PATH) throw new Error("network down");
        return jsonResponse(payload());
      }),
    );

    const { result } = renderHook(() => useApprovals(() => "tok"));
    await waitFor(() => expect(result.current.count).toBe(1));
    await act(async () => {
      await result.current.answer({ decision: "deny", requestId: "req-1" });
    });

    expect(result.current.outcome?.ok).toBe(false);
    expect(result.current.outcome?.degraded).toBe(true);
  });
});
