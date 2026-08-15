import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
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
