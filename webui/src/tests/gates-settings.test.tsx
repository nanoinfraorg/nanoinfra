import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatesSettings } from "@/components/settings/GatesSettings";
import type {
  GatesIdentity,
  GatesPayload,
  GatesPolicy,
  SettingsPayload,
} from "@/lib/types";

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
  identity?: GatesIdentity,
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
    ...(identity ? { identity } : {}),
  };
}

/** The identity block the gateway sends, in the shape of one deployment (#85). */
function identityBlock(over: Partial<GatesIdentity> = {}): GatesIdentity {
  return {
    posture: "no_proxy",
    issuer: "",
    identityClaim: "",
    workspaceKeyClaim: "",
    workspace: "",
    workspacePersonal: false,
    signOutPath: "",
    assertionHeader: "",
    actor: "webui",
    assertionMissing: false,
    ...over,
  };
}

function renderIdentity(over: Partial<GatesIdentity> = {}) {
  return renderPanel(gatesPayload(shippedPolicy(), {}, identityBlock(over)));
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

  it("renders without the default markers when the gateway sends no from_default map", () => {
    // #86. `gatesPayloadFrom` guarded `policy` and `choices` and not this third field, so a
    // partial block threw inside the render instead of hiding the panel. Hiding it would be the
    // wrong trade: this map only drives a marker, and an operator who cannot read their own
    // policy has lost more than a marker. So the reader treats an absent map as "no row is a
    // default", and the type makes every reader handle that.
    const payload = gatesPayload();
    delete (payload as { from_default?: unknown }).from_default;

    renderPanel(payload);

    expect(screen.getByTestId("gates-settings")).toBeInTheDocument();
    expect(screen.queryAllByText("default")).toHaveLength(0);
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

  it("sends an existing grant's expiry back unchanged", async () => {
    // The hole this closes: a panel that dropped `expiresAt` on save would turn a 24-hour grant
    // into a permanent one, and nobody would have chosen that.
    const savedPayload = settingsWith(gatesPayload());
    const fetchMock = vi.fn(async () => jsonResponse(savedPayload));
    vi.stubGlobal("fetch", fetchMock);
    const policy = shippedPolicy();
    policy.standingGrants = [
      {
        commands: ["systemctl reload nginx"],
        contexts: ["unattended"],
        expiresAt: "2099-01-01T00:00:00Z",
        hosts: ["staging-web-01"],
        id: "approval-2026-09-03-systemctl-1a2b3c",
        note: "Added by approve and add on 2026-09-03.",
      },
    ];
    renderPanel(gatesPayload(policy));

    fireEvent.change(
      screen.getByLabelText("Read a secret, Unattended"),
      { target: { value: "approve" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    const sent = JSON.parse(
      decodeURIComponent(headers["X-Nanoinfra-Gates-Values"]),
    ) as GatesPolicy;
    expect(sent.standingGrants[0].expiresAt).toBe("2099-01-01T00:00:00Z");
    expect(sent.standingGrants[0].note).toBe("Added by approve and add on 2026-09-03.");
  });

  it("marks an expired grant and keeps the row (nanoinfraorg/nanoinfra#218)", () => {
    const policy = shippedPolicy();
    policy.standingGrants = [
      {
        commands: ["systemctl reload nginx"],
        contexts: ["unattended"],
        expiresAt: "2020-01-01T00:00:00Z",
        hosts: ["staging-web-01"],
        id: "approval-2020-01-01-systemctl-1a2b3c",
      },
    ];

    renderPanel(gatesPayload(policy));

    // Nothing prunes it. A file the application edits is not the authority config is.
    const row = screen.getByTestId("gates-grant-row-0");
    expect(within(row).getByText(/Expired/)).toBeInTheDocument();
    expect(within(row).getByText(/nothing removed the row/)).toBeInTheDocument();
  });

  it("shows the date a live grant stops", () => {
    const policy = shippedPolicy();
    policy.standingGrants = [
      {
        commands: ["systemctl reload nginx"],
        contexts: ["unattended"],
        expiresAt: "2099-01-01T00:00:00Z",
        hosts: ["staging-web-01"],
      },
    ];

    renderPanel(gatesPayload(policy));

    const row = screen.getByTestId("gates-grant-row-0");
    expect(within(row).getByText(/Expires/)).toBeInTheDocument();
    expect(within(row).queryByText(/Expired/)).not.toBeInTheDocument();
  });

  it("says nothing about expiry for a grant that never expires", () => {
    const policy = shippedPolicy();
    policy.standingGrants = [
      {
        commands: ["systemctl reload nginx"],
        contexts: ["unattended"],
        hosts: ["staging-web-01"],
      },
    ];

    renderPanel(gatesPayload(policy));

    const row = screen.getByTestId("gates-grant-row-0");
    expect(within(row).queryByText(/Expire/)).not.toBeInTheDocument();
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

  /*
    The identity block (#85). The badge of #70 answers "who am I" and nothing else, so this panel
    answers the two questions beside it: which authentication this deployment installed, and
    whether it worked for this request.
  */
  it("reads a deployment with no proxy as a shared token, and warns about nothing", () => {
    renderIdentity();

    expect(screen.getByTestId("gates-identity-posture")).toHaveTextContent(
      "Shared token. No proxy names a person.",
    );
    expect(screen.getByTestId("gates-identity-actor")).toHaveTextContent("webui");
    expect(screen.queryByTestId("gates-identity-assertion-missing")).not.toBeInTheDocument();
    expect(screen.queryByTestId("gates-identity-plain")).not.toBeInTheDocument();
  });

  it("names the issuer and the claim of a verified deployment", () => {
    renderIdentity({
      posture: "verified",
      issuer: "accounts.google.com",
      identityClaim: "email",
      actor: "webui:alberto@example.com",
    });

    const block = screen.getByTestId("gates-identity");
    expect(screen.getByTestId("gates-identity-posture")).toHaveTextContent(
      "Verified assertion (JWT)",
    );
    expect(within(block).getByText("accounts.google.com")).toBeInTheDocument();
    expect(within(block).getByText("email")).toBeInTheDocument();
    expect(screen.getByTestId("gates-identity-actor")).toHaveTextContent(
      "webui:alberto@example.com",
    );
  });

  it("says a plain assertion is not verified, and names the header it reads", () => {
    renderIdentity({
      posture: "plain",
      assertionHeader: "X-Access-Token",
      actor: "webui:alberto@example.com",
    });

    expect(screen.getByTestId("gates-identity-posture")).toHaveTextContent(
      "Assertion header, not verified",
    );
    expect(within(screen.getByTestId("gates-identity")).getByText("X-Access-Token"))
      .toBeInTheDocument();
    const caution = screen.getByTestId("gates-identity-plain");
    expect(caution).toHaveTextContent("The proxy alone decides who reaches the agent.");
    expect(caution).toHaveAttribute("data-tone", "warning");
  });

  it("names allowAnyVerifiedIdentity, because somebody turns it on and forgets", () => {
    renderIdentity({
      posture: "any_verified",
      issuer: "accounts.google.com",
      identityClaim: "email",
      actor: "webui:alberto@example.com",
    });

    expect(screen.getByTestId("gates-identity-posture")).toHaveTextContent(
      "Verified assertion (JWT), open to every identity",
    );
    expect(screen.getByTestId("gates-identity-any-verified")).toHaveTextContent(
      "Every identity the provider signs for may reach the agent.",
    );
  });

  it("warns when a proxy is configured and no verified identity arrived", () => {
    // The state this block exists for. Every approval here names nobody, and nothing said so.
    renderIdentity({
      posture: "verified",
      issuer: "accounts.google.com",
      identityClaim: "email",
      actor: "webui",
      assertionMissing: true,
    });

    const warning = screen.getByTestId("gates-identity-assertion-missing");
    expect(warning).toHaveTextContent("every approval here names nobody");
    expect(warning).toHaveAttribute("data-tone", "warning");
  });

  it("tells the operator to write the whole actor in an approver row", () => {
    // ``gates.approvers`` compares the whole string and strips no prefix (#66).
    renderIdentity({ posture: "verified", actor: "webui:alberto@example.com" });

    expect(screen.getByTestId("gates-identity-approver-row")).toHaveTextContent(
      "Write this exact value in an approver row.",
    );
  });

  it("stays away when the gateway sends no identity block", () => {
    // An older gateway answers no block. Absent is not a posture, so the panel invents none.
    renderPanel(gatesPayload());

    expect(screen.queryByTestId("gates-identity")).not.toBeInTheDocument();
    expect(screen.getByTestId("gates-settings")).toBeInTheDocument();
  });

  it("shows an unknown posture as the value it received", () => {
    // A newer gateway can name a fifth posture. The panel must not claim one it does not know.
    renderIdentity({ posture: "sealed_room" as GatesIdentity["posture"] });

    expect(screen.getByTestId("gates-identity-posture")).toHaveTextContent("sealed_room");
  });
});
