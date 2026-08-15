import { afterEach, describe, expect, it, vi } from "vitest";

import { answerGatesApproval, clearGatesLatch } from "@/lib/api";

/**
 * The HTTP layer of the WebSocket channel serves GET alone. A POST reaches no route: the server
 * closes the connection with no response, and the operator reads "the gateway did not answer".
 *
 * Both gate writes shipped as a POST, so the latch clear and the approval answer could not work in
 * any deployment. 87 tests passed over them, because each one called the Python dispatch function
 * in process and never crossed the transport.
 *
 * Every write in this client therefore travels as a GET and carries its body in a values header.
 */

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ cleared: true, ok: true }),
    text: async () => "{}",
  }) as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("gate writes on this transport", () => {
  it("clears a latch with a GET", async () => {
    const fetchMock = stubFetch();

    await clearGatesLatch("tok", { sessionId: "s1", capabilityClass: "mutate.remote" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("answers an approval with a GET", async () => {
    const fetchMock = stubFetch();

    await answerGatesApproval("tok", { requestId: "r1", decision: "deny", reason: "no" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("keeps the values in a header, because a GET carries no body", async () => {
    const fetchMock = stubFetch();

    await clearGatesLatch("tok", { sessionId: "s1", capabilityClass: "mutate.remote" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(Object.keys(headers).some((k) => k.toLowerCase().includes("values"))).toBe(true);
    expect(init.body ?? null).toBeNull();
  });
});
