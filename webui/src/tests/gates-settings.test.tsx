import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatesSettings } from "@/components/settings/GatesSettings";
import type { GatesPayload, GatesPolicy, SettingsPayload } from "@/lib/types";

const DEFAULT_MARKER_PATHS = [
  "approvers",
  "approvalPaths",
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

function shippedPolicy(): GatesPolicy {
  return {
    approvers: [],
    approvalPaths: ["webui"],
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
  };
}

function withApprovers(approvers: Array<{ channel: string; sender: string }>): GatesPolicy {
  return { ...shippedPolicy(), approvers };
}

function gatesPayload(
  policy: GatesPolicy = shippedPolicy(),
  fromDefault: Record<string, boolean> = {},
): GatesPayload {
  const markers: Record<string, boolean> = {};
  for (const path of DEFAULT_MARKER_PATHS) markers[path] = fromDefault[path] ?? true;
  return {
    policy,
    from_default: markers,
    choices: {
      "mutate.remote": ["allow", "approve", "grant", "deny"],
      "mutate.inventory": ["allow", "deny"],
      "credential.access": ["approve", "deny"],
      all: ["deny"],
    },
  };
}

function settingsWith(gates: GatesPayload | undefined): SettingsPayload {
  return {
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
      ...(gates ? { gates } : {}),
    },
  } as unknown as SettingsPayload;
}

function renderPanel(
  gates: GatesPayload | undefined,
  onSaved: (payload: SettingsPayload) => void = () => {},
) {
  return render(
    <GatesSettings token="tok" settings={settingsWith(gates)} onSaved={onSaved} />,
  );
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

async function addGrant(host: string, command: string) {
  fireEvent.click(screen.getByRole("button", { name: "Add a grant" }));
  fireEvent.change(await screen.findByLabelText("Host 1"), { target: { value: host } });
  fireEvent.change(screen.getByLabelText("Command 1"), { target: { value: command } });
  fireEvent.click(screen.getByRole("button", { name: "Add grant" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GatesSettings", () => {
  it("stays away when the gateway sends no gate policy", () => {
    renderPanel(undefined);

    expect(screen.queryByTestId("gates-settings")).not.toBeInTheDocument();
  });

  it("marks every value that comes from a shipped default", () => {
    renderPanel(gatesPayload());

    expect(screen.getAllByText("default")).toHaveLength(13);
    expect(screen.getAllByText("default")[0]).toHaveAttribute(
      "title",
      "This value comes from a shipped default. No operator set it.",
    );
  });

  it("keeps the marker off a value the operator set", () => {
    const policy = shippedPolicy();
    policy.unattended["mutate.remote"].host = "grant";
    renderPanel(gatesPayload(policy, { "unattended.mutate.remote.host": false }));

    const row = screen.getByTestId("gates-scope-row-host");
    expect(within(row).getAllByText("default")).toHaveLength(1);
    expect(screen.getAllByText("default")).toHaveLength(12);
  });

  it("drops the marker as soon as a decision changes", () => {
    renderPanel(gatesPayload());
    const row = screen.getByTestId("gates-scope-row-host");
    expect(within(row).getAllByText("default")).toHaveLength(2);

    fireEvent.change(
      screen.getByLabelText("Remote execution, one host, Unattended"),
      { target: { value: "grant" } },
    );

    expect(within(row).getAllByText("default")).toHaveLength(1);
  });

  it("offers no control that widens the all-hosts scope", () => {
    renderPanel(gatesPayload());

    for (const context of ["Interactive", "Unattended"]) {
      const cell = screen.getByLabelText(`Remote execution, all hosts, ${context}`);
      expect(cell.tagName).toBe("SPAN");
      expect(cell).toHaveTextContent("Deny (fixed)");
    }
    expect(screen.getAllByRole("combobox")).toHaveLength(8);
    expect(
      screen.getByText(/All hosts has one value only/),
    ).toBeInTheDocument();
  });

  it("states what an empty grants table refuses", () => {
    renderPanel(gatesPayload());

    expect(screen.getByTestId("gates-grants-empty")).toHaveTextContent(
      "No grants. No automation may run a remote command.",
    );
  });

  it("names the fix when fewer than two authenticated paths exist", () => {
    renderPanel(gatesPayload());

    const warning = screen.getByTestId("gates-single-path-warning");
    expect(warning).toHaveTextContent("no runtime approval path");
    expect(warning).toHaveTextContent("Add a path below, or declare a standing grant.");
  });

  it("drops the single-path warning once a second path is configured", () => {
    const policy = shippedPolicy();
    policy.approvalPaths = ["webui", "telegram"];
    renderPanel(gatesPayload(policy, { approvalPaths: false }));

    expect(screen.queryByTestId("gates-single-path-warning")).not.toBeInTheDocument();
  });

  it("warns that full command text holds secrets", () => {
    renderPanel(gatesPayload());

    expect(
      screen.getByText("Resolved commands often hold secrets. The log then holds them too."),
    ).toBeInTheDocument();
  });

  it("says the command field is an exact string and not a pattern", async () => {
    renderPanel(gatesPayload());

    fireEvent.click(screen.getByRole("button", { name: "Add a grant" }));

    expect(await screen.findByText(/Exact match only\. This field is not a pattern\./))
      .toBeInTheDocument();
  });

  it("refuses a grant with an empty command", async () => {
    renderPanel(gatesPayload());

    fireEvent.click(screen.getByRole("button", { name: "Add a grant" }));
    fireEvent.change(await screen.findByLabelText("Host 1"), {
      target: { value: "staging-web-01" },
    });

    expect(screen.getByTestId("gates-grant-editor-missing")).toHaveTextContent(
      "Name one command or more.",
    );
    expect(screen.getByRole("button", { name: "Add grant" })).toBeDisabled();
  });

  it("keeps the save control inactive until the policy changes", () => {
    renderPanel(gatesPayload());

    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();

    fireEvent.change(
      screen.getByLabelText("Read a secret, Unattended"),
      { target: { value: "approve" } },
    );

    expect(screen.getByRole("button", { name: "Save policy" })).toBeEnabled();
  });

  it("sends the whole policy to the gate route and reports the save", async () => {
    const savedPayload = settingsWith(gatesPayload());
    const fetchMock = vi.fn(async () => jsonResponse(savedPayload));
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    renderPanel(gatesPayload(), onSaved);

    await addGrant("staging-web-01", "systemctl reload nginx");
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/settings/gates/update");
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok");
    const sent = JSON.parse(
      decodeURIComponent(headers["X-Nanoinfra-Gates-Values"]),
    ) as GatesPolicy;
    expect(sent.standingGrants).toEqual([
      {
        contexts: ["unattended"],
        hosts: ["staging-web-01"],
        commands: ["systemctl reload nginx"],
      },
    ]);
    expect(sent.interactive["mutate.remote"].all).toBe("deny");
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedPayload));
    expect(screen.getByTestId("gates-status")).toHaveTextContent(
      "Saved. The gateway reads the new policy after a restart.",
    );
  });

  it("shows the refusal that names the key", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      text: async () => "gates.interactive.mutate.remote.all: Input should be 'deny'",
      json: async () => ({}),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel(gatesPayload());

    fireEvent.change(
      screen.getByLabelText("Inventory writes, Unattended"),
      { target: { value: "allow" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
    // This save widens an unattended decision to allow, so #44 asks once before it leaves. An
    // operator changed that column while they meant the interactive one, and a cron job could then
    // reach a host with no person present.
    fireEvent.click(await screen.findByRole("button", { name: /widen it/i }));

    await waitFor(() =>
      expect(screen.getByTestId("gates-status")).toHaveTextContent(
        "gates.interactive.mutate.remote.all: Input should be 'deny'",
      ),
    );
  });

  it("discards a draft policy back to the saved policy", async () => {
    renderPanel(gatesPayload());

    await addGrant("staging-web-01", "systemctl reload nginx");
    expect(screen.getByTestId("gates-grant-row-0")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.queryByTestId("gates-grant-row-0")).not.toBeInTheDocument();
    expect(screen.getByTestId("gates-grants-empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  });

  it("adds an approver row and states that allowFrom grants nothing", () => {
    renderPanel(gatesPayload());

    expect(screen.getByTestId("gates-approvers-empty")).toHaveTextContent("No approvers.");
    fireEvent.click(screen.getByRole("button", { name: "Add an approver" }));

    expect(screen.getByLabelText("Channel 1")).toHaveValue("webui");
    expect(
      screen.getByText(/Membership in a channel allowFrom list grants nothing here/),
    ).toBeInTheDocument();
  });

  /*
    The approver forms (#71). A WebUI approver is ``webui`` or ``webui:<claim>``, and a chat
    approver is the account id that channel gives. The gate compares the whole string, so the
    panel adds no prefix: an operator has to be able to read the list and predict the match.
  */
  it("names the form each channel takes", () => {
    renderPanel(
      gatesPayload(
        withApprovers([
          { channel: "webui", sender: "webui:alberto@example.com" },
          { channel: "telegram", sender: "123456789" },
        ]),
      ),
    );

    expect(screen.getByTestId("gates-approver-shape-0")).toHaveTextContent(
      "The WebUI form is webui, or webui: and then the claim.",
    );
    expect(screen.getByTestId("gates-approver-shape-1")).toHaveTextContent(
      "The chat form is the account id of one person.",
    );
  });

  it("flags a bare claim before it is saved", () => {
    renderPanel(gatesPayload(withApprovers([{ channel: "webui", sender: "" }])));

    fireEvent.change(screen.getByLabelText("Sender 1"), {
      target: { value: "alberto@example.com" },
    });

    const flag = screen.getByTestId("gates-approver-shape-0");
    expect(flag).toHaveTextContent(
      "This sender is a bare claim, so it matches nobody. Write webui: and then the claim.",
    );
    expect(flag).toHaveAttribute("data-tone", "warning");
  });

  it("adds no prefix of its own", () => {
    // The panel that silently prefixed a claim would save a value the operator never read.
    const saved = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(settingsWith(gatesPayload()))),
    );
    renderPanel(gatesPayload(withApprovers([{ channel: "webui", sender: "" }])), saved);

    fireEvent.change(screen.getByLabelText("Sender 1"), {
      target: { value: "alberto@example.com" },
    });

    expect(screen.getByLabelText("Sender 1")).toHaveValue("alberto@example.com");
  });

  it("flags a chat approver that is not an account id", () => {
    renderPanel(
      gatesPayload(withApprovers([{ channel: "telegram", sender: "webui:alberto@example.com" }])),
    );

    expect(screen.getByTestId("gates-approver-shape-0")).toHaveTextContent(
      "A chat approver is the numeric account id the channel gives.",
    );
  });

  it("flags a row that names no sender", () => {
    renderPanel(gatesPayload());

    fireEvent.click(screen.getByRole("button", { name: "Add an approver" }));

    expect(screen.getByTestId("gates-approver-shape-0")).toHaveTextContent(
      "This row names no sender, so it matches nobody.",
    );
  });

  it("accepts the path actor of a deployment with no proxy", () => {
    // ``webui`` is the whole actor there, and it is not an unfinished ``webui:<claim>``.
    renderPanel(gatesPayload(withApprovers([{ channel: "webui", sender: "webui" }])));

    expect(screen.getByTestId("gates-approver-shape-0")).toHaveAttribute("data-tone", "info");
  });
});
