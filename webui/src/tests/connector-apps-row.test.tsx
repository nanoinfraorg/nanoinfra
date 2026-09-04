import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConnectorAppsCatalogRow } from "@/components/settings/ConnectorAppsCatalogRow";
import type { ConnectorInfo, ConnectorOperationInfo } from "@/lib/types";

/**
 * The row exists to say the posture: who the connector acts as, and what it may do. Each test
 * here is one line of that, because a row that only said "enabled" would send an operator to a
 * log at 03:00 to find out why an automation is refused.
 */

function operation(overrides: Partial<ConnectorOperationInfo> = {}): ConnectorOperationInfo {
  return {
    name: "list_events",
    tool: "google_calendar_list_events",
    capability_class: "read",
    method: "GET",
    description: "Events on a calendar in a time range.",
    returns: ["id", "summary"],
    enabled: true,
    interactive: { outcome: "allow", reason: "it is a read", grant_id: "" },
    unattended: { outcome: "allow", reason: "it is a read", grant_id: "" },
    ...overrides,
  };
}

function connector(overrides: Partial<ConnectorInfo> = {}): ConnectorInfo {
  return {
    name: "google-calendar",
    display_name: "Google Calendar",
    description: "Read and write events on a Google Calendar.",
    state: "active",
    problem: "",
    credential: "google_calendar_credential",
    max_class: "",
    settings: {},
    defaults: { calendarId: "primary" },
    official_url: "https://console.cloud.google.com/apis/credentials",
    setup_fields: [
      { name: "clientId", kind: "string", default: "", required: true, choices: [], secret: false },
      {
        name: "clientSecret",
        kind: "secret",
        default: "",
        required: true,
        choices: [],
        secret: true,
      },
      {
        name: "calendarId",
        kind: "string",
        default: "primary",
        required: false,
        choices: [],
        secret: false,
      },
    ],
    operations: [
      operation(),
      operation({
        name: "create_event",
        tool: "google_calendar_create_event",
        capability_class: "mutate.remote",
        method: "POST",
        interactive: { outcome: "approve", reason: "a person answers", grant_id: "" },
        unattended: { outcome: "deny", reason: "no grant names it", grant_id: "" },
      }),
    ],
    scopes: [
      {
        scope: "https://www.googleapis.com/auth/calendar.readonly",
        short: "calendar.readonly",
        capability_class: "read",
        granted: true,
      },
      {
        scope: "https://www.googleapis.com/auth/calendar.events",
        short: "calendar.events",
        capability_class: "mutate.remote",
        granted: true,
      },
    ],
    classes: ["read", "mutate.remote"],
    acts_as: "alberto@example.test",
    refreshed_at: new Date(Date.now() - 12 * 60 * 1000).toISOString(),
    tested_at: "",
    test_summary: "",
    last_error: "",
    last_error_at: "",
    authorize_command: "nanoinfra connectors authorize google-calendar --client-id <client-id>",
    testable: true,
    ...overrides,
  };
}

function renderRow(info: ConnectorInfo, onTest = vi.fn()) {
  render(
    <ConnectorAppsCatalogRow connector={info} busy={false} testResult={null} onTest={onTest} />,
  );
  return onTest;
}

describe("the attach control", () => {
  it("shows the mode in force and cycles to the next when clicked", () => {
    // Three states, so the button names the one in force and a click advances it:
    // always -> mention -> search -> always. The label is the current mode, not the target.
    const onAttachChange = vi.fn();
    render(
      <ConnectorAppsCatalogRow
        connector={connector()}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
        onAttachChange={onAttachChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load every turn" }));

    expect(onAttachChange).toHaveBeenCalledWith("google-calendar", "mention");
  });

  it("advances from mention to search", () => {
    const onAttachChange = vi.fn();
    render(
      <ConnectorAppsCatalogRow
        connector={connector({ attach: "mention" })}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
        onAttachChange={onAttachChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load on mention" }));

    expect(onAttachChange).toHaveBeenCalledWith("google-calendar", "search");
  });

  it("wraps from search back to loading every turn", () => {
    const onAttachChange = vi.fn();
    render(
      <ConnectorAppsCatalogRow
        connector={connector({ attach: "search" })}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
        onAttachChange={onAttachChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load on search" }));

    expect(onAttachChange).toHaveBeenCalledWith("google-calendar", "always");
  });

  it("does not offer it on a connector that is not active", () => {
    // Naming an inactive connector attaches nothing: it registered no operations. A control here
    // would be a switch with no wire behind it.
    render(
      <ConnectorAppsCatalogRow
        connector={connector({ state: "inactive" })}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
        onAttachChange={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /Load on mention|Load every turn/ })).toBeNull();
  });
});

describe("connector row", () => {
  it("keeps the connector's name, which is the one thing a row cannot lose", () => {
    // It lost it: the badge held `shrink-0` and the name held `truncate`, so in a half-width grid
    // cell the badge kept its width and "Google Calendar" rendered as a single character.
    const { container } = render(
      <ConnectorAppsCatalogRow
        connector={connector()}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
      />,
    );

    const heading = screen.getByRole("heading", { level: 3 });
    expect(heading.textContent).toBe("Google Calendar");
    expect(heading.className).toContain("min-w-0");
    const badge = container.querySelector(".uppercase");
    expect(badge?.className).not.toContain("shrink-0");
  });

  it("spans both columns of the Apps grid", () => {
    // A CLI or MCP row is an icon, a name and two buttons. This one carries who it acts as, the
    // gate's answer per capability class, ungranted scopes and its last error -- so it is given
    // the full width rather than being squeezed into half of it.
    const { container } = render(
      <ConnectorAppsCatalogRow
        connector={connector()}
        busy={false}
        testResult={null}
        onTest={vi.fn()}
        className="md:col-span-2"
      />,
    );

    expect(container.querySelector("article")?.className).toContain("md:col-span-2");
  });

  it("says who it acts as and when the token last refreshed", () => {
    renderRow(connector());

    expect(screen.getByText(/acts as alberto@example.test/)).toBeTruthy();
    expect(screen.getByText(/refreshed/)).toBeTruthy();
  });

  it("says the class and what the gate answers, per context", () => {
    renderRow(connector());

    // The asymmetry the kind exists for, on screen: a read runs, a write asks, and unattended
    // needs a grant.
    expect(screen.getByText("read")).toBeTruthy();
    expect(screen.getByText("mutate.remote")).toBeTruthy();
    expect(screen.getByText(/needs a grant/)).toBeTruthy();
  });

  it("names a grant when one covers the unattended write", () => {
    renderRow(
      connector({
        operations: [
          operation({
            name: "create_event",
            tool: "google_calendar_create_event",
            capability_class: "mutate.remote",
            interactive: { outcome: "approve", reason: "", grant_id: "" },
            unattended: { outcome: "allow", reason: "grant cal covers it", grant_id: "cal" },
          }),
        ],
      }),
    );

    expect(screen.getByText(/allowed by a grant/)).toBeTruthy();
  });

  it("warns about a scope that was never granted, with the consequence", () => {
    renderRow(
      connector({
        scopes: [
          {
            scope: "https://www.googleapis.com/auth/calendar.readonly",
            short: "calendar.readonly",
            capability_class: "read",
            granted: true,
          },
          {
            scope: "https://www.googleapis.com/auth/calendar.events",
            short: "calendar.events",
            capability_class: "mutate.remote",
            granted: false,
          },
        ],
      }),
    );

    expect(screen.getByText(/calendar.events/)).toBeTruthy();
    expect(screen.getByText(/unavailable/)).toBeTruthy();
  });

  it("says an inactive connector is a config decision rather than a failure", () => {
    renderRow(connector({ state: "inactive", acts_as: "", refreshed_at: "", testable: false }));

    expect(screen.getByText(/connectors.active/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Test/ })).toBeNull();
  });

  it("shows the reason a connector the operator asked for did not activate", () => {
    renderRow(
      connector({
        state: "not_activated",
        problem: "credential 'google_lab' was not granted ['calendar.events']",
        testable: false,
      }),
    );

    expect(screen.getByText(/was not granted/)).toBeTruthy();
  });

  it("tests through the executor rather than offering an enable toggle", () => {
    const onTest = renderRow(connector());

    // No toggle: activation is declared in config and applied when the agent starts, so a
    // switch here would be a second authority.
    expect(screen.queryByRole("switch")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Test/ }));
    expect(onTest).toHaveBeenCalledWith("google-calendar");
  });

  it("renders the re-authorise command and holds no secret", () => {
    renderRow(connector());
    fireEvent.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText(/connectors authorize google-calendar/)).toBeTruthy();
    expect(screen.getByText(/held in the secret store/)).toBeTruthy();
    expect(screen.queryByText(/clientSecret=/)).toBeNull();
  });

  it("shows a failed test in the words the executor used", () => {
    render(
      <ConnectorAppsCatalogRow
        connector={connector()}
        busy={false}
        testResult={{ ok: false, message: "the credential no longer works" }}
        onTest={vi.fn()}
      />,
    );

    expect(screen.getByText("the credential no longer works")).toBeTruthy();
  });
});
