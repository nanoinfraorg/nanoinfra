/**
 * Settings -> Tool groups (#210). One test per rule the panel exists to keep.
 *
 * The rules, not the rendering: each of these fails if its rule is dropped, and none of them
 * asserts a class name or a layout. The write is inspected through the headers the gateway reads,
 * because "the whole map, replaced" is the contract and a partial map is the failure that would
 * silently un-declare a group nobody touched.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ToolGroupsSettings,
  type SettingsWithToolGroups,
  type ToolGroupInfo,
  type ToolGroupToolRow,
} from "@/components/settings/ToolGroupsSettings";

const DIAGRAM_TOOLS = [
  "create_diagram",
  "update_diagram",
  "get_diagram",
  "list_diagrams",
  "list_diagram_components",
];
const SERVER_TOOLS = [
  "create_server",
  "update_server",
  "delete_server",
  "get_server",
  "list_servers",
  "execute_on_server",
  "device_notes",
];

function builtinRow(
  tools: string[],
  description: string,
  over: Partial<ToolGroupInfo> = {},
): ToolGroupInfo {
  return {
    attach: "always",
    declared: false,
    builtin: true,
    description: "",
    builtin_description: description,
    tools: [],
    builtin_tools: tools,
    effective_tools: tools,
    missing_tools: [],
    ...over,
  };
}

/** The default deployment: two groups nanoinfra defines, neither declared in config. */
function undeclaredSlice(): Record<string, ToolGroupInfo> {
  return {
    diagrams: builtinRow(DIAGRAM_TOOLS, "read, create and update saved infrastructure diagrams"),
    servers: builtinRow(SERVER_TOOLS, "register SSH servers and run commands on them"),
  };
}

function registeredTools(): ToolGroupToolRow[] {
  return [...DIAGRAM_TOOLS, ...SERVER_TOOLS, "read_file", "exec"].map((name) => ({
    name,
    source: "builtin",
    groups: [],
  }));
}

function settingsWith(
  slice: Record<string, ToolGroupInfo> | undefined,
  over: Partial<SettingsWithToolGroups> = {},
): SettingsWithToolGroups {
  return {
    ...(slice ? { tool_groups: slice } : {}),
    registered_tools: registeredTools(),
    requires_restart: false,
    ...over,
  } as unknown as SettingsWithToolGroups;
}

function renderPanel(
  slice: Record<string, ToolGroupInfo> | undefined,
  onSaved: (payload: SettingsWithToolGroups) => void = () => {},
  over: Partial<SettingsWithToolGroups> = {},
) {
  return render(
    <ToolGroupsSettings token="tok" settings={settingsWith(slice, over)} onSaved={onSaved} />,
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

/** The map the panel sent, reassembled from the chunk headers the gateway reads. */
function sentGroups(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  const headers = (init.headers ?? {}) as Record<string, string>;
  const count = Number(headers["X-Nanoinfra-Tool-Groups-Chunks"]);
  expect(Number.isFinite(count) && count >= 1).toBe(true);
  let encoded = "";
  for (let index = 0; index < count; index += 1) {
    encoded += headers[`X-Nanoinfra-Tool-Groups-${index}`];
  }
  const parsed = JSON.parse(decodeURIComponent(encoded)) as { groups: Record<string, unknown> };
  return parsed.groups;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ToolGroupsSettings", () => {
  it("shows a deployment that declares no group the built-ins it could declare", () => {
    renderPanel(undeclaredSlice());

    // Nothing is declared, and that is said rather than left as an empty list.
    expect(screen.getByTestId("tool-groups-none-declared")).toBeInTheDocument();
    // But both groups nanoinfra defines are offered, with their members, because an operator
    // cannot choose to put `servers` in mention mode without being told `servers` exists.
    expect(screen.getByTestId("tool-group-row-servers")).toBeInTheDocument();
    expect(screen.getByTestId("tool-group-row-diagrams")).toBeInTheDocument();
    expect(screen.getByTestId("tool-group-members-diagrams")).toHaveTextContent("create_diagram");
    expect(screen.getByTestId("tool-group-members-servers")).toHaveTextContent("execute_on_server");
    expect(screen.getByTestId("tool-group-declare-servers")).toBeInTheDocument();
  });

  it("sends the whole map when a group is created, not only the new group", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(settingsWith(undeclaredSlice())));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel({
      ...undeclaredSlice(),
      diagrams: builtinRow(DIAGRAM_TOOLS, "diagrams", {
        declared: true,
        attach: "mention",
      }),
    });

    fireEvent.click(screen.getByTestId("tool-groups-create"));
    fireEvent.change(screen.getByTestId("tool-group-editor-name"), {
      target: { value: "reporting" },
    });
    fireEvent.click(screen.getByLabelText("read_file"));
    fireEvent.click(screen.getByTestId("tool-group-editor-save"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/settings/tool-groups");
    const groups = sentGroups(fetchMock);
    // The group that was already declared travels with the new one: `tools.groups` is replaced
    // whole, so anything left out of this map is un-declared by the save.
    expect(Object.keys(groups).sort()).toEqual(["diagrams", "reporting"]);
    expect(groups.reporting).toEqual({ attach: "mention", tools: ["read_file"] });
  });

  it("says a group that names no tools inherits the built-in members, in the row and in the form", () => {
    renderPanel({
      ...undeclaredSlice(),
      // Exactly what `{"diagrams": {"attach": "mention"}}` looks like once read back.
      diagrams: builtinRow(DIAGRAM_TOOLS, "diagrams", { declared: true, attach: "mention" }),
    });

    const rowNote = screen.getByTestId("tool-group-inherits-diagrams");
    expect(rowNote).toHaveTextContent("inherits the members nanoinfra defines");
    expect(rowNote).toHaveTextContent("list_diagram_components");

    fireEvent.click(screen.getByTestId("tool-group-edit-diagrams"));

    // Nothing ticked in the form, and the form says what that means rather than reading as
    // "no tools" -- the difference between five diagram tools and none.
    expect(screen.getByTestId("tool-group-editor-count")).toHaveTextContent("0");
    const formNote = screen.getByTestId("tool-group-editor-inherits");
    expect(formNote).toHaveTextContent("inherits the members nanoinfra defines");
    expect(formNote).toHaveTextContent("create_diagram");
  });

  it("reflects a group switched to mention once the save comes back", async () => {
    const saved = settingsWith({
      ...undeclaredSlice(),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "mention" }),
    });
    const fetchMock = vi.fn(async () => jsonResponse(saved));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = renderPanel({
      ...undeclaredSlice(),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "always" }),
    });

    expect(screen.getByTestId("tool-group-row-servers")).toHaveTextContent("In every prompt");

    const control = screen.getByTestId("tool-group-attach-servers");
    fireEvent.click(segmentButton(control, "Only when mentioned"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(sentGroups(fetchMock)).toEqual({ servers: { attach: "mention" } });

    // The saved payload is what the panel renders next, so the badge and the mode follow the
    // server's answer rather than the click.
    rerender(
      <ToolGroupsSettings token="tok" settings={saved} onSaved={() => {}} />,
    );
    expect(screen.getByTestId("tool-group-row-servers")).toHaveTextContent("Not in the prompt");
    expect(
      screen.getByRole("button", { name: "Only when mentioned", pressed: true }),
    ).toBeInTheDocument();
  });

  it("renders the gateway's refusal verbatim", async () => {
    const fetchMock = vi.fn(async () =>
      ({
        ok: false,
        status: 400,
        text: async () => "Input should be 'always' or 'mention'",
        json: async () => ({}),
      }) as Response
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel({
      ...undeclaredSlice(),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "always" }),
    });

    const control = screen.getByTestId("tool-group-attach-servers");
    fireEvent.click(segmentButton(control, "Only when mentioned"));

    await waitFor(() =>
      expect(screen.getByTestId("tool-groups-status")).toHaveTextContent(
        "Input should be 'always' or 'mention'",
      ),
    );
  });

  it("asks before deleting, and sends the map without the group only after confirming", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(settingsWith(undeclaredSlice())));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel({
      ...undeclaredSlice(),
      diagrams: builtinRow(DIAGRAM_TOOLS, "diagrams", { declared: true, attach: "mention" }),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "mention" }),
    });

    await userEvent.click(screen.getByTestId("tool-group-delete-diagrams"));

    // Asked, not done: nothing has been sent yet.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("tool-groups-delete-description")).toHaveTextContent(
      "every one of its schemas in every prompt",
    );

    await userEvent.click(screen.getByTestId("tool-groups-delete-confirm"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // The other declared group survives; only the deleted one is missing from the map.
    expect(sentGroups(fetchMock)).toEqual({ servers: { attach: "mention" } });
  });

  it("edits a group in the list rather than over it", async () => {
    renderPanel({
      ...undeclaredSlice(),
      diagrams: builtinRow(DIAGRAM_TOOLS, "diagrams", { declared: true, attach: "mention" }),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "always" }),
    });

    fireEvent.click(screen.getByTestId("tool-group-edit-diagrams"));

    const editor = screen.getByTestId("tool-group-editor");
    const row = screen.getByTestId("tool-group-row-diagrams");
    // A sibling of the row it edits, so the mode of every other group stays on screen while the
    // operator decides. A dialog would render in a portal and fail this.
    expect(row.parentElement?.contains(editor)).toBe(true);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("tool-group-row-servers")).toBeVisible();

    // Delete is the one thing that still asks in a dialog, because it is the one thing that
    // cannot be read off the form before it happens.
    await userEvent.click(screen.getByTestId("tool-group-delete-servers"));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("says which mode saves tokens and which keeps the tools discoverable", () => {
    renderPanel(undeclaredSlice());

    const cost = screen.getByTestId("tool-groups-cost");
    expect(cost).toHaveTextContent("This is the mode that saves tokens");
    expect(cost).toHaveTextContent("This is the mode that keeps the tools discoverable");
    // And says what triggers the attach, since that is what an operator has to tell their users.
    expect(cost).toHaveTextContent("@group");
  });

  it("says a restart is needed when the save says so", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(settingsWith(undeclaredSlice(), { requires_restart: true }))
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel({
      ...undeclaredSlice(),
      servers: builtinRow(SERVER_TOOLS, "servers", { declared: true, attach: "always" }),
    });

    expect(screen.queryByTestId("tool-groups-restart")).not.toBeInTheDocument();

    const control = screen.getByTestId("tool-group-attach-servers");
    fireEvent.click(segmentButton(control, "Only when mentioned"));

    await waitFor(() =>
      expect(screen.getByTestId("tool-groups-restart")).toHaveTextContent(
        "Restart the gateway to apply this",
      ),
    );
  });

  it("refuses a group of its own with no tools before sending it", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(settingsWith(undeclaredSlice())));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel(undeclaredSlice());

    fireEvent.click(screen.getByTestId("tool-groups-create"));
    fireEvent.change(screen.getByTestId("tool-group-editor-name"), {
      target: { value: "reporting" },
    });

    // `set_tool_groups` drops a group with no members rather than advertising one that can never
    // load a tool, so a save that looked successful would leave nothing behind.
    expect(screen.getByTestId("tool-group-editor-members-problem")).toBeInTheDocument();
    expect(screen.getByTestId("tool-group-editor-save")).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a name that could never be typed as @name", () => {
    renderPanel(undeclaredSlice());

    fireEvent.click(screen.getByTestId("tool-groups-create"));
    fireEvent.change(screen.getByTestId("tool-group-editor-name"), {
      target: { value: "My Reports" },
    });

    expect(screen.getByTestId("tool-group-editor-name-problem")).toHaveTextContent(
      "lower case letters, digits, dash and underscore",
    );
    expect(screen.getByTestId("tool-group-editor-save")).toBeDisabled();
  });

  it("names the members a group holds that this deployment never registered", () => {
    renderPanel({
      reporting: {
        attach: "mention",
        declared: true,
        builtin: false,
        description: "the weekly report",
        builtin_description: "",
        tools: ["read_file", "mcp_sheets_append_row"],
        builtin_tools: [],
        effective_tools: ["read_file", "mcp_sheets_append_row"],
        missing_tools: ["mcp_sheets_append_row"],
      },
    });

    expect(screen.getByTestId("tool-group-missing-reporting")).toHaveTextContent(
      "mcp_sheets_append_row",
    );
  });

  it("stays out of the way when the gateway reports no tool groups at all", () => {
    renderPanel(undefined);

    expect(screen.getByTestId("tool-groups-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-groups-create")).not.toBeInTheDocument();
  });
});

/** One button inside a segmented control, by its label. */
function segmentButton(container: HTMLElement, label: string): HTMLElement {
  const match = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent === label,
  );
  if (!match) throw new Error(`no button labelled ${label}`);
  return match as HTMLElement;
}
