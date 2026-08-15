import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatesSettings } from "@/components/settings/GatesSettings";
import type { GatesPayload, GatesPolicy, SettingsPayload } from "@/lib/types";

/**
 * The maintainer meant to widen the interactive decision for one host. They changed the unattended
 * decision instead, saved it, and a cron job could then run a remote command with no person
 * present. They wrote the design, and the layout still misled them.
 *
 * The cause: the `default` marker sat between the two controls with no column separation, so a row
 * read as one control per column, shifted one place.
 */

const MARKER_PATHS = [
  "approvers",
  "approvalPaths",
  "approvalTimeoutS",
  "interactive.mutate.remote.host",
  "interactive.mutate.remote.group",
  "interactive.mutate.remote.all",
  "interactive.mutate.inventory",
  "interactive.credential.access",
  "unattended.mutate.remote.host",
  "unattended.mutate.remote.group",
  "unattended.mutate.remote.all",
  "unattended.mutate.inventory",
  "unattended.credential.access",
  "standingGrants",
  "audit.retentionDays",
  "audit.recordCommandText",
];

function policy(): GatesPolicy {
  return {
    approvers: [],
    approvalPaths: ["webui"],
    approvalTimeoutS: 120,
    interactive: {
      "mutate.remote": { host: "approve", group: "approve", all: "deny" },
      "mutate.inventory": "allow",
      "credential.access": "approve",
    },
    unattended: {
      "mutate.remote": { host: "deny", group: "deny", all: "deny" },
      "mutate.inventory": "deny",
      "credential.access": "deny",
    },
    standingGrants: [],
    audit: { retentionDays: 90, recordCommandText: false },
  } as GatesPolicy;
}

function payload(p: GatesPolicy = policy()): GatesPayload {
  const markers: Record<string, boolean> = {};
  for (const path of MARKER_PATHS) markers[path] = true;
  return {
    policy: p,
    from_default: markers,
    choices: {
      "mutate.remote": ["allow", "approve", "grant", "deny"],
      "mutate.inventory": ["allow", "deny"],
      "credential.access": ["allow", "approve", "grant", "deny"],
      all: ["deny"],
    },
  } as GatesPayload;
}

function renderPanel(gates: GatesPayload = payload()) {
  const settings = {
    advanced: {
      restrict_to_workspace: false,
      webui_allow_local_service_access: true,
      webui_default_access_mode: "default",
      private_service_protection_enabled: true,
      ssrf_whitelist_count: 0,
      mcp_server_count: 0,
      exec_enabled: true,
      exec_sandbox: null,
      exec_path_prepend_set: false,
      exec_path_append_set: false,
      gates,
    },
  } as unknown as SettingsPayload;
  return render(<GatesSettings token="tok" settings={settings} onSaved={() => {}} />);
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the decision matrix", () => {
  it("keeps each control in the cell of its own column", () => {
    renderPanel();

    const row = screen.getByTestId("gates-scope-row-host");
    const interactive = within(row).getByTestId("gates-cell-interactive");
    const unattended = within(row).getByTestId("gates-cell-unattended");

    // One control per cell, and the marker inside the cell it describes. A marker between two
    // controls reads as a label for the one on its right.
    expect(within(interactive).getAllByRole("combobox")).toHaveLength(1);
    expect(within(unattended).getAllByRole("combobox")).toHaveLength(1);
    expect(within(interactive).getByText("default")).toBeInTheDocument();
    expect(within(unattended).getByText("default")).toBeInTheDocument();
  });

  it("says that the unattended column has no person in it", () => {
    renderPanel();

    expect(screen.getByText(/no person is present/i)).toBeInTheDocument();
  });

  it("asks once before it widens an unattended decision to allow", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ advanced: { gates: payload() } }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const row = screen.getByTestId("gates-scope-row-host");
    const unattended = within(row).getByTestId("gates-cell-unattended");
    fireEvent.change(within(unattended).getByRole("combobox"), { target: { value: "allow" } });
    fireEvent.click(screen.getByRole("button", { name: /save policy/i }));

    // The dialog names the row it would widen, so the operator reads what they are about to permit.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/widen an unattended decision/i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /widen it/i })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("saves after the operator confirms the widening", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ advanced: { gates: payload() } }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const row = screen.getByTestId("gates-scope-row-host");
    const unattended = within(row).getByTestId("gates-cell-unattended");
    fireEvent.change(within(unattended).getByRole("combobox"), { target: { value: "allow" } });
    fireEvent.click(screen.getByRole("button", { name: /save policy/i }));
    fireEvent.click(await screen.findByRole("button", { name: /widen it/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("saves a narrowed decision with no confirmation", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ advanced: { gates: payload() } }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const row = screen.getByTestId("gates-scope-row-host");
    const interactive = within(row).getByTestId("gates-cell-interactive");
    fireEvent.change(within(interactive).getByRole("combobox"), { target: { value: "deny" } });
    fireEvent.click(screen.getByRole("button", { name: /save policy/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });
});
