import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import { AgentPluginsSettings } from "@/components/settings/AgentPluginsSettings";
import type { AgentPluginInfo, AgentPluginsPayload } from "@/lib/types";

const fetchAgentPlugins = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchAgentPlugins: (...args: unknown[]) => fetchAgentPlugins(...args),
}));

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ getToken: () => "test-token" }),
}));

function plugin(overrides: Partial<AgentPluginInfo> = {}): AgentPluginInfo {
  return {
    name: "acme.toolkit",
    display_name: "Deploy Toolkit",
    description: "Deploy helpers.",
    repository: "",
    version: "1.2.0",
    category: "Ops",
    accent_color: null,
    logo: null,
    permissions: [],
    skills: ["deploy-check"],
    mcp_servers: ["api"],
    state: "active",
    declared: true,
    ...overrides,
  };
}

function payload(overrides: Partial<AgentPluginsPayload> = {}): AgentPluginsPayload {
  return {
    plugins: [plugin()],
    unknown: [],
    editable: false,
    authority: "tools.agentPlugins",
    ...overrides,
  };
}

describe("AgentPluginsSettings", () => {
  beforeEach(() => {
    fetchAgentPlugins.mockReset();
  });

  it("renders a package with its components and version", async () => {
    fetchAgentPlugins.mockResolvedValue(payload());

    render(<AgentPluginsSettings />);

    expect(await screen.findByText("Deploy Toolkit")).toBeInTheDocument();
    expect(screen.getByText("acme.toolkit")).toBeInTheDocument();
    expect(screen.getByText("v1.2.0")).toBeInTheDocument();
    expect(screen.getByText("deploy-check")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("names the launch route for an MCP server", async () => {
    fetchAgentPlugins.mockResolvedValue(payload());

    render(<AgentPluginsSettings />);

    expect(await screen.findByText(/mcp-host/)).toBeInTheDocument();
  });

  it("offers no control that could change activation", async () => {
    fetchAgentPlugins.mockResolvedValue(payload());

    render(<AgentPluginsSettings />);
    await screen.findByText("Deploy Toolkit");

    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    for (const button of screen.queryAllByRole("button")) {
      expect(button.textContent ?? "").not.toMatch(/enable|disable|activate/i);
    }
  });

  it("says where activation actually comes from", async () => {
    fetchAgentPlugins.mockResolvedValue(payload());

    render(<AgentPluginsSettings />);

    expect(await screen.findByText(/tools\.agentPlugins/)).toBeInTheDocument();
    expect(screen.getByText(/not editable here/i)).toBeInTheDocument();
  });

  it("explains a modified package rather than showing it as merely off", async () => {
    fetchAgentPlugins.mockResolvedValue(
      payload({ plugins: [plugin({ state: "modified" })] }),
    );

    render(<AgentPluginsSettings />);

    expect(await screen.findByText("modified")).toBeInTheDocument();
    expect(screen.getByText(/deactivated itself/i)).toBeInTheDocument();
  });

  it("tells an operator how to activate an inactive package", async () => {
    fetchAgentPlugins.mockResolvedValue(
      payload({ plugins: [plugin({ state: "inactive", declared: false })] }),
    );

    render(<AgentPluginsSettings />);

    expect(await screen.findByText("inactive")).toBeInTheDocument();
    expect(screen.getByText(/config\.json/)).toBeInTheDocument();
  });

  it("surfaces a config name that no package provides", async () => {
    fetchAgentPlugins.mockResolvedValue(payload({ unknown: ["ghost"] }));

    render(<AgentPluginsSettings />);

    expect(await screen.findByText(/ghost/)).toBeInTheDocument();
  });

  it("reports an empty workspace", async () => {
    fetchAgentPlugins.mockResolvedValue(payload({ plugins: [] }));

    render(<AgentPluginsSettings />);

    expect(await screen.findByText(/No Agent Plugins are installed/i)).toBeInTheDocument();
  });

  it("shows a retry path when the request fails", async () => {
    fetchAgentPlugins.mockRejectedValue(new Error("boom"));

    render(<AgentPluginsSettings />);

    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
