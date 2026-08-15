import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LatchBanner } from "@/components/LatchBanner";
import { fmtDateTime } from "@/lib/format";

const READ_PATH = "/api/webui/gates/latches";
const CLEAR_PATH = "/api/webui/gates/latches/clear";
const DENIED_AT = "2026-08-14T14:32:55+00:00";

interface LatchAttempt {
  at: string | null;
  digest: string | null;
  tool: string | null;
}

function latch(overrides: Record<string, unknown> = {}) {
  return {
    attempts: [] as LatchAttempt[],
    capabilityClass: "mutate.remote",
    deniedAt: DENIED_AT,
    deniedBy: "operator-1",
    reason: "unattended mutate.remote at group scope is denied",
    refusals: 6,
    sessionId: "websocket:chat-1",
    ...overrides,
  };
}

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

/** One stub for both routes. ``latches`` is what the read route answers next. */
function stubGateway(latches: unknown[], options: { clearOk?: boolean } = {}) {
  const state = { latches };
  const calls: { body: string | null; url: string }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    calls.push({ body: headers["X-Nanoinfra-Latch-Values"] ?? null, url });
    if (url === CLEAR_PATH) {
      if (options.clearOk === false) return jsonResponse({ error: "no" }, false);
      state.latches = [];
      return jsonResponse({ cleared: true });
    }
    return jsonResponse({ degraded: false, latches: state.latches, summary: "" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

describe("LatchBanner", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("names the latched class, the denial, the refusal count, and the one way out", async () => {
    stubGateway([latch()]);

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);

    expect(
      await screen.findByText("Remote execution is latched for this session."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        `Denied ${fmtDateTime(DENIED_AT)} by operator-1. 6 attempts refused since.`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The agent cannot ask again until you clear this."),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        READ_PATH,
        expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
      );
    });
  });

  it("stays out of the way when this session holds no latch", async () => {
    stubGateway([latch({ sessionId: "websocket:chat-2" })]);

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the refused attempts and the denial reason on request", async () => {
    stubGateway([
      latch({
        attempts: [{ at: DENIED_AT, digest: "sha256:abc", tool: "execute_on_server" }],
        refusals: 1,
      }),
    ]);
    const user = userEvent.setup();

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);
    await user.click(await screen.findByRole("button", { name: "View attempts" }));

    expect(screen.getByText(/execute_on_server/)).toBeInTheDocument();
    expect(screen.getByText(/sha256:abc/)).toBeInTheDocument();
    expect(
      screen.getByText(/unattended mutate.remote at group scope is denied/),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 attempt refused since\./)).toBeInTheDocument();
  });

  it("clears the latch through the operator route and then goes away", async () => {
    const { calls } = stubGateway([latch()]);
    const user = userEvent.setup();

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);
    await user.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    const clearCall = calls.find((call) => call.url === CLEAR_PATH);
    expect(clearCall?.body).toBe(
      JSON.stringify({ capabilityClass: "mutate.remote", sessionId: "websocket:chat-1" }),
    );
  });

  it("says the block still holds when the clear fails", async () => {
    stubGateway([latch()], { clearOk: false });
    const user = userEvent.setup();

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);
    await user.click(await screen.findByRole("button", { name: "Clear" }));

    expect(
      await screen.findByText("The gateway did not clear this latch. The block still holds."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Remote execution is latched for this session."),
    ).toBeInTheDocument();
  });

  it("reports an unreadable audit log instead of an empty banner", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          degraded: true,
          latches: [],
          summary:
            "gates: the audit log could not be read, so every session stays latched. "
            + "An operator must clear each one after the log is readable again.",
        }),
      ),
    );

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);

    expect(await screen.findByText(/every session stays latched/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("shows nothing when the gateway has no gate runtime", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, false)));

    render(<LatchBanner sessionKey="websocket:chat-1" token="tok" />);

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
