import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatesAuditLog, type AuditPage, type AuditRecord } from "@/components/settings/GatesAuditLog";

function record(over: Partial<AuditRecord> = {}): AuditRecord {
  return {
    ts: "2026-08-14T14:38:02+00:00",
    sessionId: "s1",
    executionContext: "automation",
    originPath: "telegram",
    approvalPath: null,
    samePath: false,
    actor: null,
    capabilityClass: "mutate.remote",
    scope: "group",
    hosts: ["10.0.2.11", "10.0.2.12"],
    hostCount: 2,
    commandDigest: "sha256:aa11bb22cc33",
    commandText: null,
    holdsCommandText: false,
    decision: "deny",
    reason: "unattended mutate.remote at group scope is denied",
    grantId: null,
    tokenNonce: null,
    exitCode: null,
    durationMs: null,
    tool: "execute_on_server",
    ...over,
  };
}

function page(records: AuditRecord[], over: Partial<AuditPage> = {}): AuditPage {
  return {
    records,
    total: records.length,
    limit: 50,
    offset: 0,
    recordsCommandText: false,
    choices: {
      decision: ["allow", "grant", "approve", "deny", "refused", "expired"],
      capabilityClass: ["read", "mutate.local", "mutate.inventory", "mutate.remote", "credential.access"],
      executionContext: ["interactive", "automation", "subagent"],
    },
    ...over,
  };
}

function stubFetch(body: AuditPage | null, status = 200) {
  const fetchMock = vi.fn(async () => ({
    ok: status === 200,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }) as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GatesAuditLog", () => {
  it("shows a denial with its reason", async () => {
    stubFetch(page([record()]));

    render(<GatesAuditLog token="tok" />);

    const row = await screen.findByTestId("audit-row-0");
    expect(within(row).getByText("deny")).toBeInTheDocument();
    fireEvent.click(row);
    expect(
      await screen.findByText("unattended mutate.remote at group scope is denied"),
    ).toBeInTheDocument();
  });

  it("shows a latched refusal", async () => {
    stubFetch(page([record({ decision: "refused", reason: "a denial is terminal for this session" })]));

    render(<GatesAuditLog token="tok" />);

    const row = await screen.findByTestId("audit-row-0");
    expect(within(row).getByText("refused")).toBeInTheDocument();
  });

  it("asks the route for one execution context when the operator picks one", async () => {
    const fetchMock = stubFetch(page([record()]));
    render(<GatesAuditLog token="tok" />);
    await screen.findByTestId("audit-row-0");

    fireEvent.change(screen.getByLabelText("Context"), { target: { value: "automation" } });

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(urls.some((url) => url.includes("executionContext=automation"))).toBe(true);
    });
  });

  it("shows the digest and no command text under the default settings", async () => {
    stubFetch(page([record()]));

    render(<GatesAuditLog token="tok" />);
    fireEvent.click(await screen.findByTestId("audit-row-0"));

    expect(await screen.findByText("sha256:aa11bb22cc33")).toBeInTheDocument();
    expect(screen.queryByTestId("audit-command-text")).not.toBeInTheDocument();
  });

  it("marks a record that holds the full command text", async () => {
    stubFetch(
      page([record({ commandText: "mysql -p hunter2", holdsCommandText: true })], {
        recordsCommandText: true,
      }),
    );

    render(<GatesAuditLog token="tok" />);
    fireEvent.click(await screen.findByTestId("audit-row-0"));

    expect(await screen.findByTestId("audit-command-text")).toHaveTextContent("mysql -p hunter2");
    expect(screen.getByText(/may hold a secret/i)).toBeInTheDocument();
  });

  it("marks a shared channel even when the gate allowed the action", async () => {
    stubFetch(
      page([
        record({
          decision: "allow",
          samePath: true,
          originPath: "webui",
          approvalPath: "webui",
        }),
      ]),
    );

    render(<GatesAuditLog token="tok" />);

    const row = await screen.findByTestId("audit-row-0");
    expect(within(row).getByTestId("audit-same-path")).toBeInTheDocument();
  });

  it("shows the resolved targets beside the grant id", async () => {
    stubFetch(page([record({ decision: "grant", grantId: "reload-web" })]));

    render(<GatesAuditLog token="tok" />);
    fireEvent.click(await screen.findByTestId("audit-row-0"));

    const detail = await screen.findByTestId("audit-detail");
    expect(within(detail).getByText("reload-web")).toBeInTheDocument();
    expect(within(detail).getByText(/10\.0\.2\.11/)).toBeInTheDocument();
  });

  it("offers no control that changes a record", async () => {
    stubFetch(page([record()]));

    render(<GatesAuditLog token="tok" />);
    fireEvent.click(await screen.findByTestId("audit-row-0"));

    for (const button of screen.queryAllByRole("button")) {
      expect(button.textContent ?? "").not.toMatch(/delete|remove|prune|clear the log|edit/i);
    }
  });

  it("says the log is unreachable rather than empty when the gateway has no gate runtime", async () => {
    stubFetch(null, 503);

    render(<GatesAuditLog token="tok" />);

    expect(await screen.findByText(/cannot reach the audit log/i)).toBeInTheDocument();
  });

  it("states what an empty log means", async () => {
    stubFetch(page([]));

    render(<GatesAuditLog token="tok" />);

    expect(await screen.findByText(/no decision matches/i)).toBeInTheDocument();
  });
});
