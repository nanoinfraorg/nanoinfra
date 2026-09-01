import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { McpAppsCatalogRow } from "@/components/settings/SettingsView";
import type { McpPresetInfo } from "@/lib/types";

/**
 * The row's job is one question: are this server's tool schemas in the prompt?
 *
 * Worth stating why this file exists at all. `enabled: false` shipped with the control as an item
 * inside the menu behind the row's checkmark — and the whole suite stayed green, because nothing
 * here rendered the row. A checkmark with no chevron reads as a status icon, so the feature was
 * present and invisible, and a test that only calls the action would not have caught that either.
 * So these assert on what a reader can *see*: a switch, and a line saying what it costs.
 */

function preset(overrides: Partial<McpPresetInfo> = {}): McpPresetInfo {
  return {
    name: "github-bet0x",
    display_name: "github-bet0x",
    category: "custom",
    description: "Custom MCP server from nanoinfra config.",
    docs_url: "",
    transport: "stdio",
    requires: "",
    note: "",
    install_supported: true,
    installed: true,
    configured: true,
    available: true,
    status: "configured",
    required_fields: [],
    connection_summary: "",
    tool_names: ["create_issue", "get_issue", "list_issues"],
    enabled_tools: ["*"],
    source: "custom",
    ...overrides,
  };
}

function renderRow(info: McpPresetInfo, onAction = vi.fn()) {
  render(
    <McpAppsCatalogRow
      preset={info}
      values={{}}
      actionKey={null}
      showBrandLogos={false}
      onFieldChange={vi.fn()}
      onAction={onAction}
      onToolsChange={vi.fn()}
    />,
  );
  return onAction;
}

describe("an installed MCP row", () => {
  it("offers a switch, not a menu item", () => {
    renderRow(preset());

    expect(screen.getByRole("switch")).toBeTruthy();
  });

  it("shows the switch on, because the schemas are being sent", () => {
    renderRow(preset());

    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  });

  it("says what the row costs rather than where it came from", () => {
    renderRow(preset());

    expect(screen.getByText("3 tools · in every prompt")).toBeTruthy();
  });

  it("pauses when the switch is turned off", () => {
    const onAction = renderRow(preset());

    fireEvent.click(screen.getByRole("switch"));

    expect(onAction).toHaveBeenCalledWith("pause", "github-bet0x");
  });
});

describe("a mention-only MCP row", () => {
  it("says the tools wait to be asked for", () => {
    renderRow(preset({ attach: "mention" }));

    expect(screen.getByText("3 tools · sent only when you say @github-bet0x")).toBeTruthy();
  });

  it("keeps the switch on, because the server is still connected", () => {
    // Mention-only is not pausing: the server is live and one word away. A row that showed this as
    // "off" would be claiming the config lost something it did not.
    renderRow(preset({ attach: "mention" }));

    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  });
});

describe("a paused MCP row", () => {
  it("shows the switch off", () => {
    renderRow(preset({ paused: true }));

    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });

  it("says the schemas are in no prompt", () => {
    renderRow(preset({ paused: true }));

    expect(screen.getByText("3 tools · paused, not in any prompt")).toBeTruthy();
  });

  it("resumes from the same switch", () => {
    const onAction = renderRow(preset({ paused: true }));

    fireEvent.click(screen.getByRole("switch"));

    expect(onAction).toHaveBeenCalledWith("resume", "github-bet0x");
  });

  it("keeps the row, because a paused server is still configured", () => {
    renderRow(preset({ paused: true }));

    // The name is still readable and the trash icon is still its own control: pausing and removing
    // are the two different things this row has to keep apart.
    expect(screen.getByText("github-bet0x")).toBeTruthy();
    expect(screen.getByLabelText("Remove")).toBeTruthy();
  });
});

describe("a paused server that never connected", () => {
  it("counts its allowlist, because there is no live tool list to count", () => {
    // It reported `0 tools` for a server holding fifteen: pausing means never connecting, so
    // `tool_names` is empty and the allowlist is the only honest number left.
    renderRow(preset({ paused: true, tool_names: [], enabled_tools: ["a", "b", "c", "d"] }));

    expect(screen.getByText("4 tools · paused, not in any prompt")).toBeTruthy();
  });

  it("says nothing about a count it cannot know", () => {
    renderRow(preset({ paused: true, tool_names: [], enabled_tools: ["*"] }));

    expect(screen.getByText("Paused — not in any prompt")).toBeTruthy();
  });
});

describe("the tool list", () => {
  it("opens from the count, because the count alone does not say which", () => {
    const onAction = renderRow(preset());

    fireEvent.click(screen.getByText("3 tools · in every prompt"));

    expect(screen.getByText("create_issue")).toBeTruthy();
    expect(onAction).not.toHaveBeenCalled();
  });

  it("lists the allowlist when the server never connected", () => {
    renderRow(preset({ paused: true, tool_names: [], enabled_tools: ["get_issue"] }));

    fireEvent.click(screen.getByText("1 tools · paused, not in any prompt"));

    expect(screen.getByText("get_issue")).toBeTruthy();
    expect(screen.getByText(/not connected/)).toBeTruthy();
  });
});

describe("a row that is not installed", () => {
  it("has no switch, because there is nothing to send", () => {
    renderRow(preset({ installed: false, configured: false, status: "not_installed" }));

    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("keeps its description, since there is no cost to report yet", () => {
    renderRow(preset({ installed: false, configured: false, status: "not_installed" }));

    expect(screen.getByText("Custom MCP server from nanoinfra config.")).toBeTruthy();
  });
});
