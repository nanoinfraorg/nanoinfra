/**
 * The Agents area, and the name it took over -- nanoinfraorg/nanoinfra#253.
 *
 * `Agents` was already a settings section, and it was not about agents: it held one number, the
 * maximum number of subagents that may run at once, which is parallelism inside a single agent.
 * The roster takes the name, and that row becomes what it always was -- one setting inside the
 * area rather than the whole of it.
 *
 * Two things have to be true at once. The row keeps working in its new home, and a deployment that
 * names no agents sees the section exactly where it has always been, because for them nothing has
 * changed.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import type { NamedAgentRosterEntry, SettingsPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

beforeEach(() => {
  // The panel refreshes itself and its neighbours from the gateway on mount. Nothing here is
  // about those reads, so they answer 404 rather than reaching for a socket that is not there.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }) as Response),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function rosterEntry(over: Partial<NamedAgentRosterEntry> = {}): NamedAgentRosterEntry {
  return {
    name: "sre",
    description: "hands-on checks on one host",
    model_preset: "primary",
    tool_group_count: 3,
    skill_count: 2,
    delegate_count: 1,
    has_addendum: true,
    ...over,
  };
}

function settingsPayload(namedAgents: NamedAgentRosterEntry[]): SettingsPayload {
  return {
    agent: {
      model: "openai/gpt-4o",
      provider: "auto",
      resolved_provider: "openai",
      has_api_key: true,
      model_preset: "primary",
      max_tokens: 8192,
      context_window_tokens: 200_000,
      temperature: 0.1,
      reasoning_effort: null,
      timezone: "UTC",
      max_concurrent_subagents: 3,
      tool_hint_max_length: 40,
    },
    model_presets: [
      {
        name: "primary",
        label: "Primary",
        active: true,
        is_default: false,
        model: "openai/gpt-4o",
        provider: "auto",
        resolved_provider: "openai",
        max_tokens: 8192,
        context_window_tokens: 200_000,
        temperature: 0.1,
        reasoning_effort: null,
      },
    ],
    model_call_order: ["primary"],
    model_call_order_editable: true,
    providers: [],
    web_search: {
      provider: "duckduckgo",
      api_key_hint: null,
      base_url: null,
      max_results: 5,
      timeout: 30,
      providers: [{ name: "duckduckgo", label: "DuckDuckGo", credential: "none" }],
    },
    web: {
      enable: true,
      proxy: null,
      user_agent: null,
      search: { max_results: 5, timeout: 30 },
      fetch: { use_jina_reader: true },
    },
    api: { host: "127.0.0.1", port: 8900, timeout: 120, api_key_hint: null },
    observability: {
      provider: "langfuse",
      configured: false,
      base_url: "https://cloud.langfuse.com",
    },
    image_generation: {
      enabled: false,
      provider: "openrouter",
      provider_configured: false,
      model: "openai/gpt-image",
      default_aspect_ratio: "1:1",
      default_image_size: "1K",
      max_images_per_turn: 4,
      save_dir: "generated",
      providers: [],
    },
    runtime: {
      config_path: "/tmp/config.json",
      workspace_path: "/tmp/workspace",
      gateway_host: "127.0.0.1",
      gateway_port: 18790,
      heartbeat: { enabled: true, interval_s: 1800, keep_recent_messages: 8 },
      dream: { schedule: "every 2h" },
      unified_session: false,
    },
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
    },
    version: { current: "0.0.0-test" },
    docs: {
      version: "0.0.0-test",
      base_url: "https://docs.nanoinfra.org",
      chat_apps_url: "https://docs.nanoinfra.org/chat-apps/",
      latest_url: "https://docs.nanoinfra.org",
    },
    named_agents: namedAgents,
    requires_restart: false,
  } as unknown as SettingsPayload;
}

function renderAgentsArea(namedAgents: NamedAgentRosterEntry[], showSidebar = false) {
  return render(
    <ClientProvider client={{} as never} token="tok">
      <SettingsView
        theme="light"
        initialSection="agents"
        initialSettings={settingsPayload(namedAgents)}
        showSidebar={showSidebar}
        onToggleTheme={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
      />
    </ClientProvider>,
  );
}

describe("the Agents area", () => {
  it("keeps the subagent-concurrency row reachable in its new home", async () => {
    renderAgentsArea([rosterEntry()]);

    await waitFor(() => {
      expect(screen.getByTestId("agent-roster")).toBeInTheDocument();
    });
    const input = screen.getByDisplayValue("3");
    expect(input).toBeInTheDocument();
    expect(screen.getByText("Subagents at once")).toBeInTheDocument();
  });

  it("shows the roster above the row, because the roster is what the name means now", async () => {
    const { container } = renderAgentsArea([rosterEntry()]);

    await waitFor(() => {
      expect(screen.getByTestId("agent-roster")).toBeInTheDocument();
    });
    const text = container.textContent ?? "";
    expect(text.indexOf("Named agents")).toBeLessThan(text.indexOf("Subagents"));
  });

  it("is the same panel it has always been when no agent is named", async () => {
    renderAgentsArea([]);

    await waitFor(() => {
      expect(screen.getByText("Subagents at once")).toBeInTheDocument();
    });
    // No roster, no empty-state paragraph, no heading for a list of nothing.
    expect(screen.queryByTestId("agent-roster")).toBeNull();
  });
});

describe("the settings sidebar", () => {
  it("still lists Agents when there is no top-level destination for it", async () => {
    renderAgentsArea([], true);

    const nav = await screen.findByRole("navigation");
    expect(within(nav).getAllByText("Agents").length).toBeGreaterThan(0);
  });

  it("stops listing it once the deployment names agents", async () => {
    // The same panel under one name in two places is the confusion the rename was meant to end.
    renderAgentsArea([rosterEntry()], true);

    const nav = await screen.findByRole("navigation");
    expect(within(nav).queryByText("Agents")).toBeNull();
  });
});
