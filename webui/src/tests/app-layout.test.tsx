import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import type { ChatSummary, SessionAutomationJob } from "@/lib/types";

const connectSpy = vi.fn();
const refreshSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
const deleteChatSpy = vi.fn();
const getSessionAutomationsSpy = vi.fn<(key: string) => Promise<SessionAutomationJob[]>>();
const toggleThemeSpy = vi.fn();
const updateUrlSpy = vi.fn();
const attachSpy = vi.fn();
const setSidebarStateSpy = vi.fn();
const runStatusHandlers = new Set<(chatId: string, startedAt: number | null) => void>();
const sessionUpdateHandlers = new Set<(chatId: string, scope?: string) => void>();
let mockSessions: ChatSummary[] = [];
const HERO_GREETING_PATTERN =
  /What should we work on\?|Where should we start\?|What are we building today\?|What should we tackle together\?/;

function setNavigatorPlatform(platform: string): void {
  Object.defineProperty(window.navigator, "platform", {
    configurable: true,
    value: platform,
  });
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function mockFetchRoutes(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const route = routes[String(input)];
      const body =
        typeof route === "function"
          ? await (route as () => unknown | Promise<unknown>)()
          : route;
      return body === undefined
        ? ({ ok: false, status: 404, json: async () => ({}) } as Response)
        : jsonResponse(body);
    }),
  );
}

/** One action that waits for an answer, in the shape the inbox route sends (#27). */
function pendingApproval(requestId: string) {
  return {
    capabilityClass: "mutate.remote",
    executionContext: "interactive",
    expiresInS: 107,
    hostCount: 1,
    hosts: ["web-01"],
    originPath: "telegram",
    payload: "nanoinfra approval request v1\nHosts: 1\n   1. web-01",
    requestId,
    samePath: false,
    scope: "host",
    sessionId: "telegram:chat-1",
    targetDigest: "sha256:abc",
  };
}

function baseSettingsPayload() {
  return {
    agent: {
      model: "openai/gpt-4o",
      provider: "auto",
      resolved_provider: "openai",
      has_api_key: true,
      model_preset: "default",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
      timezone: "UTC",
      tool_hint_max_length: 40,
    },
    model_presets: [{
      name: "default",
      label: "Default",
      active: true,
      is_default: true,
      model: "openai/gpt-4o",
      provider: "auto",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
    }],
    model_call_order: [],
    model_call_order_editable: false,
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
    image_generation: {
      enabled: false,
      provider: "openrouter",
      provider_configured: false,
      model: "openai/gpt-5.4-image-2",
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
      heartbeat: {
        enabled: true,
        interval_s: 1800,
        keep_recent_messages: 8,
      },
      dream: {
        schedule: "every 2h",
      },
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
    requires_restart: false,
  };
}

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions, setSessions] = React.useState(mockSessions);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: refreshSpy,
        createChat: createChatSpy,
        forkChat: async () => "fork-chat",
        getSessionAutomations: getSessionAutomationsSpy,
        deleteChat: async (key: string, options?: { deleteAutomations?: boolean }) => {
          if (options === undefined) await deleteChatSpy(key);
          else await deleteChatSpy(key, options);
          setSessions((prev: ChatSummary[]) => prev.filter((s) => s.key !== key));
          return { deleted: true };
        },
      };
    },
  };
});

vi.mock("@/hooks/useTheme", async () => {
  const React = await import("react");
  return {
    ThemeProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useTheme: () => ({
      theme: "light" as const,
      toggle: toggleThemeSpy,
    }),
    useThemeValue: () => "light" as const,
  };
});

vi.mock("@/lib/bootstrap", () => ({
  BootstrapAuthRequiredError: class BootstrapAuthRequiredError extends Error {
    constructor(message = "bootstrap authentication required") {
      super(message);
      this.name = "BootstrapAuthRequiredError";
    }
  },
  fetchBootstrap: vi.fn().mockResolvedValue({
    token: "tok",
    api_token: "api-tok",
    ws_path: "/",
    expires_in: 300,
  }),
  deriveWsUrl: vi.fn(() => "ws://test"),
  consumeUrlBootstrapSecret: vi.fn(() => ""),
  loadSavedSecret: vi.fn(() => ""),
  saveSecret: vi.fn(),
  clearSavedSecret: vi.fn(),
}));

vi.mock("@/lib/nanoinfra-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = connectSpy;
    onStatus = () => () => {};
    // The actor the gateway resolved (#70). The badge subscribes to it, so the double owes the
    // method: a double that answers fewer members than the class is a passing test of nothing.
    operatorActor: string | null = null;
    onOperatorActor = () => () => {};
    onRuntimeModelUpdate = () => () => {};
    onError = () => () => {};
    onChat = () => () => {};
    onSessionUpdate = (handler: (chatId: string, scope?: string) => void) => {
      sessionUpdateHandlers.add(handler);
      return () => sessionUpdateHandlers.delete(handler);
    };
    onRunStatus = (handler: (chatId: string, startedAt: number | null) => void) => {
      runStatusHandlers.add(handler);
      return () => runStatusHandlers.delete(handler);
    };
    getRunStartedAt = () => null;
    getGoalState = () => undefined;
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = attachSpy;
    setSidebarState = setSidebarStateSpy;
    close = vi.fn();
    updateUrl = updateUrlSpy;
    updateMaxFrameBytes = vi.fn();
  }

  return { NanoinfraClient: MockClient };
});

import {
  BootstrapAuthRequiredError,
  deriveWsUrl,
  fetchBootstrap,
} from "@/lib/bootstrap";
import App from "@/App";

describe("App layout", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    mockSessions = [];
    connectSpy.mockClear();
    updateUrlSpy.mockClear();
    refreshSpy.mockReset();
    createChatSpy.mockClear();
    deleteChatSpy.mockReset();
    getSessionAutomationsSpy.mockReset().mockResolvedValue([]);
    toggleThemeSpy.mockReset();
    attachSpy.mockReset();
    setSidebarStateSpy.mockReset();
    runStatusHandlers.clear();
    sessionUpdateHandlers.clear();
    window.history.replaceState(null, "", "/");
    setNavigatorPlatform("Linux x86_64");
    localStorage.removeItem("nanoinfra-webui.sidebar");
    localStorage.removeItem("nanoinfra-webui.sidebar.completed-runs.v1");
    localStorage.removeItem("nanoinfra-webui.sidebar.session-updates.v1");
    localStorage.removeItem("nanoinfra-webui.restartStartedAt");
    localStorage.removeItem("nanoinfra-webui.restartRoute");
    vi.mocked(fetchBootstrap).mockReset().mockResolvedValue({
      token: "tok",
      api_token: "api-tok",
      ws_path: "/",
      expires_in: 300,
    });
    vi.mocked(deriveWsUrl).mockReset().mockReturnValue("ws://test");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
      }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the auth form without an invalid-password error on first load", async () => {
    vi.mocked(fetchBootstrap).mockRejectedValueOnce(
      new Error("bootstrap failed: HTTP 401"),
    );

    render(<App />);

    expect(await screen.findByText("Authentication required")).toBeInTheDocument();
    expect(screen.queryByText("Invalid password. Try again.")).not.toBeInTheDocument();
    expect(connectSpy).not.toHaveBeenCalled();
  });

  it("shows the auth form when bootstrap does not issue an API token", async () => {
    vi.mocked(fetchBootstrap).mockRejectedValueOnce(
      new BootstrapAuthRequiredError(
        "bootstrap authentication required: missing api_token",
      ),
    );

    render(<App />);

    expect(await screen.findByText("Authentication required")).toBeInTheDocument();
    expect(screen.queryByText("Invalid password. Try again.")).not.toBeInTheDocument();
    expect(connectSpy).not.toHaveBeenCalled();
  });

  it("shows an invalid-password error after a submitted password is rejected", async () => {
    vi.mocked(fetchBootstrap).mockRejectedValue(
      new Error("bootstrap failed: HTTP 401"),
    );

    render(<App />);

    const password = await screen.findByPlaceholderText("Password");
    fireEvent.change(password, { target: { value: "wrong-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Invalid password. Try again.")).toBeInTheDocument();
    expect(fetchBootstrap).toHaveBeenLastCalledWith("", "wrong-password");
    expect(connectSpy).not.toHaveBeenCalled();
  });

  it("keeps sidebar layout out of the main thread width contract", async () => {
    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const main = container.querySelector("main");
    expect(main).toBeInTheDocument();
    expect(main).not.toHaveAttribute("style");

    const asideClassNames = Array.from(container.querySelectorAll("aside")).map(
      (el) => el.className,
    );
    expect(asideClassNames.some((cls) => cls.includes("lg:block"))).toBe(true);
  });

  it("places Automations after Skills in the main sidebar", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const appsButton = within(sidebar).getByRole("button", { name: "Apps" });
    const skillsButton = within(sidebar).getByRole("button", { name: "Skills" });
    const automationsButton = within(sidebar).getByRole("button", { name: "Automations" });

    expect(appsButton.compareDocumentPosition(skillsButton) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(
      skillsButton.compareDocumentPosition(automationsButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("highlights the blank new-topic destination immediately", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const newTopicButton = within(sidebar).getByRole("button", { name: "New topic" });

    expect(newTopicButton).toHaveAttribute("aria-current", "page");
    expect(newTopicButton).not.toHaveClass("bg-sidebar-accent");
    expect(newTopicButton).toHaveClass("transition-[width,padding,color]");
    expect(within(sidebar).getByTestId("actions-selection-highlight")).toHaveAttribute(
      "data-active-id",
      "new-chat",
    );
  });

  it("restores the Settings route after a restart fallback hash", async () => {
    localStorage.setItem("nanoinfra-webui.restartStartedAt", String(Date.now()));
    localStorage.setItem("nanoinfra-webui.restartRoute", "#/settings?section=channels");
    window.history.replaceState(null, "", "/#/new");
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/settings/nanoinfra-features": {
        features: [{
          name: "websocket",
          display_name: "Websocket",
          type: "channel",
          enabled: true,
          installed: true,
          ready: true,
          status: "enabled",
          install_supported: true,
          requires_restart: true,
        }],
        enabled_count: 1,
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Channels" })).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=channels");
  });

  it("opens the approvals inbox from the main sidebar and counts the unread actions", async () => {
    // nanoinfraorg/nanoinfra#27. An operator answers an approval during work, so the inbox has
    // its own route and the navigation carries the count.
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/gates/approvals": {
        approvalPath: "webui",
        count: 1,
        degraded: false,
        pending: [{
          capabilityClass: "mutate.remote",
          executionContext: "interactive",
          expiresInS: 107,
          hostCount: 2,
          hosts: ["web-01", "web-02"],
          originPath: "telegram",
          payload: "nanoinfra approval request v1\nHosts: 2\n   1. web-01\n   2. web-02",
          requestId: "req-1",
          samePath: false,
          scope: "group",
          sessionId: "telegram:chat-1",
          targetDigest: "sha256:abc",
        }],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const inbox = await within(sidebar).findByRole("button", {
      name: "Approvals waiting for an answer: 1",
    });

    fireEvent.click(inbox);

    expect(await screen.findByText("Pending: 1")).toBeInTheDocument();
    expect(screen.getByText("web-01")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(document.title).toBe("Approvals · nanoinfra");
  });

  it("carries the approvals count into Settings and opens the inbox from there", async () => {
    // nanoinfraorg/nanoinfra#87. The Security section of Overview reads the count the sidebar
    // already receives, and its row leaves Settings for the Approvals view. That row is about
    // work waiting for an answer, not about configuration.
    window.history.replaceState(null, "", "/#/settings?section=overview");
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/gates/approvals": {
        approvalPath: "webui",
        count: 2,
        degraded: false,
        pending: [pendingApproval("req-1"), pendingApproval("req-2")],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const row = await screen.findByTestId("overview-security-approvals");
    await waitFor(() => expect(row).toHaveTextContent("2 waiting"));

    fireEvent.click(row);

    await waitFor(() => expect(document.title).toBe("Approvals · nanoinfra"));
    expect(window.location.hash).toBe("#/approvals");
  });

  it("opens Skills from the main sidebar", async () => {
    const longSkillDescription = [
      "Work with GitHub repositories, issues, pull requests, releases, workflows,",
      "and code search through the GitHub CLI.",
      "Use this skill for repository maintenance, review automation, release preparation,",
      "and other GitHub workflows that need authenticated command-line access.",
    ].join(" ");
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/settings/cli-apps": { apps: [], installed_count: 0, catalog_updated_at: "2026-04-18" },
      "/api/settings/mcp-presets": { presets: [], installed_count: 0 },
      "/api/webui/skills": {
        skills: [
          {
            name: "cron",
            description: "Schedule reminders.",
            source: "builtin",
            enabled: true,
            deletable: false,
            available: true,
          },
          {
            name: "github",
            description: "Work with GitHub.",
            source: "builtin",
            enabled: true,
            deletable: false,
            available: false,
            unavailable_reason: "CLI: gh",
          },
          {
            name: "custom-skill",
            description: "A workspace skill.",
            source: "workspace",
            enabled: true,
            deletable: true,
            available: true,
          },
        ],
      },
      "/api/webui/skills/github": {
        name: "github",
        description: longSkillDescription,
        source: "builtin",
        enabled: true,
        deletable: false,
        available: false,
        unavailable_reason: "CLI: gh",
        requirements: {
          bins: ["gh"],
          env: [],
          missing_bins: ["gh"],
          missing_env: [],
        },
        install_options: [{
          id: "brew",
          kind: "brew",
          label: "Install GitHub CLI (brew)",
          command: "brew install gh",
        }],
        raw_markdown: "---\nname: github\n---\nUse GitHub CLI.",
      },
      "/api/webui/skills/update?name=github&enabled=false": {
        skills: [
          {
            name: "cron",
            description: "Schedule reminders.",
            source: "builtin",
            enabled: true,
            deletable: false,
            available: true,
          },
          {
            name: "github",
            description: "Work with GitHub.",
            source: "builtin",
            enabled: false,
            deletable: false,
            available: false,
            unavailable_reason: "CLI: gh",
          },
          {
            name: "custom-skill",
            description: "A workspace skill.",
            source: "workspace",
            enabled: true,
            deletable: true,
            available: true,
          },
        ],
        last_action: {
          name: "github",
          enabled: false,
          deleted: false,
        },
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const skillsButton = within(sidebar).getByRole("button", { name: "Skills" });

    fireEvent.click(skillsButton);

    expect(await screen.findByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search installed skills" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Custom" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Built-in" })).toBeInTheDocument();
    expect(screen.getByText("cron")).toBeInTheDocument();
    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("Needs setup")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sidebar navigation" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Settings sections" })).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Skills" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("Skills · nanoinfra");

    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
    expect(await screen.findByText(HERO_GREETING_PATTERN)).toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "Skills" }));
    expect(await screen.findByRole("heading", { name: "Skills" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open details for github" }));

    expect(await screen.findByRole("heading", { name: "github" })).toBeInTheDocument();
    const showMore = await screen.findByRole("button", { name: "Show more" });
    expect(showMore).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(showMore);
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Setup required")).toBeInTheDocument();
    expect(screen.getByText("brew install gh")).toBeInTheDocument();
    expect(screen.queryByText("Unavailable reason")).not.toBeInTheDocument();
    expect(screen.queryByText("Missing CLI")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Skill instructions"));
    expect(screen.getByText(/Use GitHub CLI/)).toBeInTheDocument();
    const enabledSwitch = screen.getByRole("switch", { name: "Disable github" });
    expect(enabledSwitch).toHaveAttribute("aria-checked", "true");
    fireEvent.click(enabledSwitch);
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "Enable github" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
    });
  });

  it("deletes a custom skill from its detail sheet", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/settings/cli-apps": { apps: [], installed_count: 0, catalog_updated_at: "2026-04-18" },
      "/api/settings/mcp-presets": { presets: [], installed_count: 0 },
      "/api/webui/skills": {
        skills: [
          {
            name: "custom-skill",
            description: "A workspace skill.",
            source: "workspace",
            enabled: true,
            deletable: true,
            available: true,
          },
        ],
      },
      "/api/webui/skills/custom-skill": {
        name: "custom-skill",
        description: "A workspace skill.",
        source: "workspace",
        enabled: true,
        deletable: true,
        available: true,
        requirements: {
          bins: [],
          env: [],
          missing_bins: [],
          missing_env: [],
        },
        raw_markdown: "---\nname: custom-skill\n---\nWorkspace instructions.",
      },
      "/api/webui/skills/delete?name=custom-skill": {
        skills: [],
        last_action: {
          name: "custom-skill",
          enabled: false,
          deleted: true,
        },
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Skills" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Open details for custom-skill" }),
    );
    expect(await screen.findByRole("heading", { name: "custom-skill" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("heading", { name: "Delete custom-skill?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete skill" }));

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Open details for custom-skill" }),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByText("No matching skills.")).toBeInTheDocument();
  });

  it("discovers and installs a skill from skills.sh", async () => {
    let finishInstall!: (value: unknown) => void;
    const pendingInstall = new Promise<unknown>((resolve) => {
      finishInstall = resolve;
    });
    const installedPayload = {
      skills: [
        {
          name: "react-testing",
          description: "Test React apps.",
          source: "workspace",
          available: true,
        },
        { name: "cron", description: "Schedule reminders.", source: "builtin", available: true },
      ],
      last_action: {
        installed: true,
        already_installed: false,
        name: "react-testing",
      },
    };
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/settings/cli-apps": { apps: [], installed_count: 0, catalog_updated_at: "2026-04-18" },
      "/api/settings/mcp-presets": { presets: [], installed_count: 0 },
      "/api/webui/skills": {
        skills: [
          { name: "cron", description: "Schedule reminders.", source: "builtin", available: true },
        ],
      },
      "/api/webui/skills/trending?provider=all": {
        period: "mixed",
        provider: "all",
        install_supported: true,
        skills: [
          {
            id: "vercel-labs/skills/find-skills",
            skill_id: "find-skills",
            name: "find-skills",
            source: "vercel-labs/skills",
            provider: "skills_sh",
            installs: 14_481,
            url: "https://skills.sh/vercel-labs/skills/find-skills",
            installed: false,
            install_supported: true,
            metric: "installs_24h",
            rank: 18,
          },
          {
            id: "nanoinfra:ima-skills",
            skill_id: "ima-skills",
            name: "ima-skills",
            source: "nanoinfra",
            provider: "nanoinfra",
            installs: 142_525,
            url: "https://skills.nanoinfra.org/skills/ima-skills",
            installed: false,
            install_supported: true,
            metric: "installs_total",
            version: "3",
            rank: 1,
          },
        ],
      },
      "/api/webui/skills/trends?id=vercel-labs%2Fskills%2Ffind-skills": {
        trends: {
          "vercel-labs/skills/find-skills": [20, 32, 28, 45, 41, 50, 62, 58],
        },
      },
      "/api/webui/skills/search?q=React&provider=all": {
        query: "React",
        provider: "all",
        install_supported: true,
        skills: [
          {
            id: "acme/agent-skills/react-testing",
            skill_id: "react-testing",
            name: "React Testing",
            source: "acme/agent-skills",
            provider: "skills_sh",
            installs: 42,
            url: "https://skills.sh/acme/agent-skills/react-testing",
            installed: false,
            install_supported: true,
            metric: "installs_total",
          },
          {
            id: "nanoinfra:react",
            skill_id: "react",
            name: "React",
            source: "nanoinfra",
            provider: "nanoinfra",
            installs: 7_718,
            url: "https://skills.nanoinfra.org/skills/react",
            installed: false,
            install_supported: true,
            metric: "installs_total",
            version: "2",
          },
        ],
      },
      "/api/webui/skills/trends?id=acme%2Fagent-skills%2Freact-testing": {
        trends: { "acme/agent-skills/react-testing": [] },
      },
      "/api/webui/skills/install?provider=skills_sh&source=acme%2Fagent-skills&skill=react-testing":
        () => pendingInstall,
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Skills" }));
    const discoverTab = await screen.findByRole("tab", { name: "Discover" });
    expect(discoverTab.querySelector("svg")).toBeNull();
    fireEvent.click(discoverTab);
    expect(
      await screen.findByRole("heading", { name: "Trending by marketplace" }),
    ).toBeInTheDocument();
    expect(screen.getByText("find-skills")).toBeInTheDocument();
    expect(screen.getByText("ima-skills")).toBeInTheDocument();
    expect(screen.getAllByText("nanoinfra")).toHaveLength(2);
    expect(screen.getAllByText("skills.sh")).toHaveLength(2);
    expect(screen.getByText(/14,481 installs \/ 24h/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "nanoinfra" }));
    expect(screen.getByText("ima-skills")).toBeInTheDocument();
    expect(screen.queryByText("find-skills")).not.toBeInTheDocument();
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([input]) =>
          String(input) === "/api/webui/skills/trending?provider=nanoinfra",
      ),
    ).toBe(false);
    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    expect(screen.getByText("find-skills")).toBeInTheDocument();
    expect(screen.getByText("ima-skills")).toBeInTheDocument();
    expect(
      await screen.findByRole("img", { name: "8-week install trend" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Search skills" }), {
      target: { value: "React" },
    });

    expect(await screen.findByText("React Testing")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Install React Testing" }));
    expect(
      await screen.findByRole("heading", { name: "Install React Testing?" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Install skill" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/webui/skills/install?provider=skills_sh&source=acme%2Fagent-skills&skill=react-testing",
        expect.objectContaining({
          headers: { Authorization: expect.any(String) },
        }),
      );
    });
    fireEvent.click(screen.getByRole("tab", { name: "Installed" }));
    fireEvent.click(screen.getByRole("tab", { name: "Discover" }));
    expect(
      await screen.findByRole("button", { name: "Install find-skills" }),
    ).toBeDisabled();

    await act(async () => {
      finishInstall(installedPayload);
      await pendingInstall;
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Install find-skills" })).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("tab", { name: "Installed" }));
    expect(screen.getByText("react-testing")).toBeInTheDocument();
  });

  it("opens Automations from the main sidebar", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "job-1",
            name: "Daily repo check",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 86_400_000 },
            payload: {
              message: "Check the repo status",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: [],
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "Release prep",
              preview: "Check release blockers",
            },
          },
          {
            id: "external-quiz",
            name: "Slack quiz",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "cron", expr: "30 9-23 * * *", tz: "Asia/Shanghai" },
            payload: {
              message: "Send a quiz",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 11, 30, 0),
              last_status: "ok",
              pending: false,
              run_history: [],
            },
            origin: {
              channel: "slack",
              title: "",
              preview: "",
            },
          },
          {
            id: "heartbeat",
            name: "heartbeat",
            enabled: true,
            protected: true,
            schedule: { kind: "every", every_ms: 60_000 },
            payload: { message: "", kind: "system_event" },
            state: { next_run_at_ms: null, pending: false, run_history: [] },
            origin: null,
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const automationsButton = within(sidebar).getByRole("button", {
      name: "Automations",
    });

    fireEvent.click(automationsButton);

    const heading = await screen.findByRole("heading", { name: "Automations" });
    expect(heading).toBeInTheDocument();
    const automationsMain = heading.closest("main");
    expect(automationsMain).not.toBeNull();
    expect(within(automationsMain as HTMLElement).queryByText("Settings")).not.toBeInTheDocument();
    expect(screen.getAllByText("Daily repo check").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Check the repo status").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Release prep").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Slack quiz")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();
    expect(screen.queryByText("slack:wx-chat")).not.toBeInTheDocument();
    expect(screen.queryByText("memory with dream state")).not.toBeInTheDocument();
    expect(screen.getByText("heartbeat")).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Automations" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("Automations · nanoinfra");

    const searchInput = within(automationsMain as HTMLElement).getByPlaceholderText(
      "Search task, message, linked chat, or schedule",
    );
    fireEvent.change(searchInput, { target: { value: "Slack" } });
    await waitFor(() => expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument());
    expect(screen.getAllByText("Slack quiz").length).toBeGreaterThanOrEqual(1);

    fireEvent.change(searchInput, { target: { value: "09-23" } });
    await waitFor(() => expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument());
    expect(screen.getAllByText("Slack quiz").length).toBeGreaterThanOrEqual(1);
  });

  it("edits a past one-time automation without resubmitting its old schedule", async () => {
    const pastOneShot = {
      id: "past-one-shot",
      name: "Past one-shot",
      enabled: true,
      protected: false,
      delete_after_run: true,
      schedule: { kind: "at", at_ms: 1 },
      payload: {
        message: "Old one-shot message",
        kind: "agent_turn",
      },
      state: {
        next_run_at_ms: null,
        last_status: "ok",
        pending: false,
        run_history: [],
      },
      origin: {
        session_key: "websocket:chat-a",
        channel: "websocket",
        chat_id: "chat-a",
        title: "Release prep",
        preview: "Check release blockers",
      },
    };
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": { jobs: [pastOneShot] },
      "/api/webui/automations/update?id=past-one-shot": {
        jobs: [
          {
            ...pastOneShot,
            payload: { ...pastOneShot.payload, message: "Updated one-shot message" },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    expect((await screen.findAllByText("Past one-shot")).length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByText("Run time must be in the future.")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Update the prompt and schedule. The linked chat stays unchanged."),
    ).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Old one-shot message")).toHaveClass(
      "min-h-[160px]",
      "resize-none",
    );

    fireEvent.change(screen.getByDisplayValue("Old one-shot message"), {
      target: { value: "Updated one-shot message" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/webui/automations/update?id=past-one-shot",
        expect.any(Object),
      );
    });
    const updateCall = vi.mocked(fetch).mock.calls.find(
      ([url]) => String(url) === "/api/webui/automations/update?id=past-one-shot",
    );
    expect(updateCall).toBeTruthy();
    const headers = updateCall?.[1]?.headers as Record<string, string>;
    expect(JSON.parse(decodeURIComponent(headers["X-Nanoinfra-Automation-Values"]))).toEqual({
      name: "Past one-shot",
      message: "Updated one-shot message",
    });
  });

  it("shows what an automation remembers and lets an operator reset it", async () => {
    const resetCalls: string[] = [];
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "with-state",
            name: "Blockers digest",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "cron", expr: "0 9 * * 1", tz: "UTC" },
            payload: { message: "Report new blockers", kind: "agent_turn" },
            state: { next_run_at_ms: Date.UTC(2026, 3, 20, 9, 0, 0), pending: false, run_history: [] },
            origin: null,
          },
        ],
      },
      "/api/webui/automations/with-state/state": {
        id: "with-state",
        values: { reported_issues: [47, 51], phase: "idle" },
      },
      "/api/webui/automations/with-state/state/reset": () => {
        resetCalls.push("with-state");
        return { id: "with-state", cleared: true };
      },
    });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const heading = await screen.findByRole("heading", { name: "Blockers digest" });
    const panel = heading.closest("article") as HTMLElement;

    expect(await within(panel).findByText("Remembered state")).toBeInTheDocument();
    expect(await within(panel).findByText("reported_issues")).toBeInTheDocument();
    expect(within(panel).getByText("[47,51]")).toBeInTheDocument();
    // A stored string renders bare rather than quoted.
    expect(within(panel).getByText("idle")).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Reset" }));

    await waitFor(() => expect(resetCalls).toEqual(["with-state"]));
    expect(
      await within(panel).findByText("This automation remembers nothing yet"),
    ).toBeInTheDocument();
  });

  it("does not ask a system automation for state it cannot have", async () => {
    const stateCalls: string[] = [];
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "heartbeat",
            name: "heartbeat",
            enabled: true,
            protected: true,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 60_000 },
            payload: { message: "", kind: "system_event" },
            state: { next_run_at_ms: Date.UTC(2026, 3, 18, 3, 0, 0), pending: false, run_history: [] },
            origin: null,
          },
        ],
      },
      "/api/webui/automations/heartbeat/state": () => {
        stateCalls.push("heartbeat");
        return { id: "heartbeat", values: {} };
      },
    });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const heading = await screen.findByRole("heading", { name: "heartbeat" });
    const panel = heading.closest("article") as HTMLElement;

    expect(await within(panel).findByText("Recent runs")).toBeInTheDocument();
    expect(within(panel).queryByText("Remembered state")).not.toBeInTheDocument();
    expect(stateCalls).toEqual([]);
  });

  it("renders recent runs in the automation detail pane", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "with-runs",
            name: "Nightly backup check",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "cron", expr: "0 3 * * *", tz: "UTC" },
            payload: { message: "Check the backup", kind: "agent_turn" },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 18, 3, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: [
                { run_at_ms: Date.UTC(2026, 3, 16, 3, 0, 0), status: "error", duration_ms: 1500, error: "host unreachable" },
                { run_at_ms: Date.UTC(2026, 3, 17, 3, 0, 0), status: "ok", duration_ms: 420 },
              ],
            },
            origin: null,
          },
        ],
      },
    });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const heading = await screen.findByRole("heading", { name: "Nightly backup check" });
    const panel = heading.closest("article") as HTMLElement;
    expect(panel).not.toBeNull();

    expect(within(panel).getByText("Recent runs")).toBeInTheDocument();
    // Newest first: the question this pane answers is "what happened last time".
    const statuses = within(panel)
      .getAllByText(/^(Succeeded|Failed|Skipped)$/)
      .map((node) => node.textContent);
    expect(statuses).toEqual(["Succeeded", "Failed"]);
    expect(within(panel).getByText("host unreachable")).toBeInTheDocument();
    expect(within(panel).getByText("420ms")).toBeInTheDocument();
    expect(within(panel).getByText("1.5s")).toBeInTheDocument();
  });

  it("says the history is empty rather than omitting it", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "never-ran",
            name: "Fresh automation",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 3_600_000 },
            payload: { message: "Do the thing", kind: "agent_turn" },
            state: { next_run_at_ms: Date.UTC(2026, 3, 18, 3, 0, 0), pending: false, run_history: [] },
            origin: null,
          },
        ],
      },
    });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const heading = await screen.findByRole("heading", { name: "Fresh automation" });
    const panel = heading.closest("article") as HTMLElement;
    expect(within(panel).getByText("Recent runs")).toBeInTheDocument();
    expect(within(panel).getByText("No runs recorded yet")).toBeInTheDocument();
  });

  it("counts an intermittently failing automation as needing attention", async () => {
    const job = (
      id: string,
      name: string,
      history: Array<{ run_at_ms: number; status: string; duration_ms?: number }>,
    ) => ({
      id,
      name,
      enabled: true,
      protected: false,
      delete_after_run: false,
      schedule: { kind: "every", every_ms: 3_600_000 },
      payload: { message: name, kind: "agent_turn" },
      state: {
        next_run_at_ms: Date.UTC(2026, 3, 18, 3, 0, 0),
        last_status: "ok",
        pending: false,
        run_history: history,
      },
      origin: null,
    });
    const at = (day: number) => Date.UTC(2026, 3, day, 3, 0, 0);

    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          // Alternating: currently succeeding, but failing every other run.
          job("flaky", "Flaky automation", [
            { run_at_ms: at(13), status: "error" },
            { run_at_ms: at(14), status: "ok" },
            { run_at_ms: at(15), status: "error" },
            { run_at_ms: at(16), status: "ok" },
            { run_at_ms: at(17), status: "ok" },
          ]),
          // One failure that recovered is the "failed once" case and must not be surfaced.
          job("recovered", "Recovered automation", [
            { run_at_ms: at(13), status: "error" },
            { run_at_ms: at(14), status: "ok" },
            { run_at_ms: at(15), status: "ok" },
            { run_at_ms: at(16), status: "ok" },
            { run_at_ms: at(17), status: "ok" },
          ]),
        ],
      },
    });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const needsAttention = await screen.findByRole("button", { name: /Needs attention/ });
    expect(needsAttention).toHaveTextContent("1");

    fireEvent.click(needsAttention);
    // The name shows in both the queue row and the detail heading, so count rather than expect one.
    await waitFor(() => expect(screen.getAllByText("Flaky automation").length).toBeGreaterThan(0));
    expect(screen.queryByText("Recovered automation")).not.toBeInTheDocument();
  });

  it("keeps long automation details expandable without nested scrolling", async () => {
    const longMessage = [
      "Review the release plan and prepare a concise status update for the channel.",
      "Include blockers, owners, follow-up dates, and any risky assumptions that changed since yesterday.",
      "Keep the output actionable and avoid repeating context that the team already confirmed in the thread.",
      "If a dependency looks stale, call it out explicitly and ask for a fresh owner update.",
      "This message is intentionally long enough to require progressive disclosure in the automation details panel.",
      "The full content should remain available without forcing the user into a small nested scroll area.",
    ].join("\n");
    const history = [
      { run_at_ms: Date.UTC(2026, 3, 12, 10, 0, 0), status: "error", duration_ms: 900, error: "oldest failure" },
      { run_at_ms: Date.UTC(2026, 3, 13, 10, 0, 0), status: "error", duration_ms: 800, error: "second oldest failure" },
      { run_at_ms: Date.UTC(2026, 3, 14, 10, 0, 0), status: "ok", duration_ms: 700 },
      { run_at_ms: Date.UTC(2026, 3, 15, 10, 0, 0), status: "ok", duration_ms: 600 },
      { run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0), status: "ok", duration_ms: 500 },
      { run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0), status: "ok", duration_ms: 400 },
    ];
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "long-details",
            name: "Long detail automation",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 3_600_000 },
            payload: {
              message: longMessage,
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 18, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: history,
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "Release prep",
              preview: "Check release blockers",
            },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Automations" }));

    const detailHeading = await screen.findByRole("heading", { name: "Long detail automation" });
    const detailPanel = detailHeading.closest("article") as HTMLElement;
    expect(detailPanel).not.toBeNull();
    const message = Array.from(detailPanel.querySelectorAll("section div")).find(
      (node) => node.textContent === longMessage,
    ) as HTMLElement | undefined;
    expect(message).toBeTruthy();
    expect(message!).toHaveClass("line-clamp-6");

    fireEvent.click(within(detailPanel).getByRole("button", { name: "Show full message" }));
    expect(within(detailPanel).getByRole("button", { name: "Show less" })).toBeInTheDocument();
    expect(message!).not.toHaveClass("line-clamp-6");

    // 87aaf899 removed a run-history UI with a "Recent health" summary line, a collapsible
    // expander and per-run dots. These three assertions keep that version out: #154 renders a
    // flat list, so a summary, an expander button and a per-run "no error" filler would all be
    // the complexity coming back.
    expect(within(detailPanel).queryByText("Recent health")).not.toBeInTheDocument();
    expect(within(detailPanel).queryByRole("button", { name: /Run history/ })).not.toBeInTheDocument();
    expect(within(detailPanel).queryByText("No error recorded")).not.toBeInTheDocument();
    // The errors themselves are now shown, newest first, which is the point of #154.
    expect(within(detailPanel).getByText("second oldest failure")).toBeInTheDocument();
  });

  it("localizes the Automations surface", async () => {
    await i18n.changeLanguage("ja");
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "job-zh",
            name: "每日检查",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 86_400_000 },
            payload: {
              message: "检查仓库状態",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0),
              last_run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: [
                {
                  run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0),
                  status: "ok",
                  duration_ms: 500,
                },
              ],
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "发布准备",
              preview: "检查发布阻塞项",
            },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "サイドバーのナビゲーション" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "自動タスク" }));

    const heading = await screen.findByRole("heading", { name: "自動タスク" });
    expect(heading).toBeInTheDocument();
    const automationsMain = heading.closest("main");
    expect(automationsMain).not.toBeNull();
    expect(within(automationsMain as HTMLElement).queryByText("設定")).not.toBeInTheDocument();
    expect(screen.getByText("キュー")).toBeInTheDocument();
    expect(screen.getAllByText("每日检查").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("检查仓库状態").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("1 日 ごと")).toBeInTheDocument();
    expect(screen.queryByText("最近健康状態")).not.toBeInTheDocument();
    expect(screen.queryByText("近期无问题")).not.toBeInTheDocument();
    expect(screen.queryByText("Workspace automations")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新" })).not.toBeInTheDocument();
    expect(document.title).toBe("自動タスク · nanoinfra");
  });

  it("fully collapses the native host sidebar and previews it on hover", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Desktop chat",
      },
    ];
    vi.mocked(fetchBootstrap).mockResolvedValue({
      token: "tok",
      api_token: "api-tok",
      ws_path: "/",
      expires_in: 300,
      runtime_surface: "native",
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const flowSidebar = screen.getByTestId("host-sidebar-flow");
    const toggle = screen.getByTestId("host-sidebar-toggle");
    expect(flowSidebar).toHaveStyle({ width: "272px" });
    expect(
      screen.getByRole("navigation", { name: "Sidebar navigation" }),
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() => expect(flowSidebar).toHaveStyle({ width: "0px" }));
    expect(
      screen.queryByRole("navigation", { name: "Sidebar navigation" }),
    ).not.toBeInTheDocument();

    fireEvent.mouseEnter(toggle);
    const previewSidebar = await screen.findByTestId("host-sidebar-preview");
    expect(flowSidebar).toHaveStyle({ width: "0px" });
    expect(previewSidebar).toHaveStyle({ width: "272px" });
    expect(
      within(previewSidebar).getByRole("navigation", {
        name: "Sidebar navigation",
      }),
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.queryByTestId("host-sidebar-preview")).not.toBeInTheDocument(),
    );
    expect(flowSidebar).toHaveStyle({ width: "272px" });
    expect(
      screen.getByRole("navigation", { name: "Sidebar navigation" }),
    ).toBeInTheDocument();
  });

  it("switches to the next session when deleting the active chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText("Topic actions for First chat"), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(screen.getByText("Delete this topic?")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a"),
    );
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Second chat$/ }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Delete this topic?")).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  }, 15_000);

  it("shows localized bound automations in the first delete confirmation", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    getSessionAutomationsSpy.mockResolvedValue([
      {
        id: "job-1",
        name: "Daily repo check",
        enabled: true,
        schedule: { kind: "every", every_ms: 86_400_000 },
        payload: { message: "Check the repo" },
        state: { next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0) },
      },
    ]);
    await i18n.changeLanguage("ja");

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "サイドバーのナビゲーション" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText(/First chat.*トピック操作/), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "削除" }));

    await waitFor(() =>
      expect(screen.getByText("Daily repo check")).toBeInTheDocument(),
    );
    expect(getSessionAutomationsSpy).toHaveBeenCalledWith("websocket:chat-a");
    expect(
      screen.getByText("このチャットにはスケジュール済みの自動タスクがあります。削除するとそれらも削除されます。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("This chat has scheduled automations. Deleting it will also delete them."),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "削除" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a", {
        deleteAutomations: true,
      }),
    );
    expect(deleteChatSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument();
  }, 15_000);

  it("keeps the mobile session action menu inside the sidebar sheet", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: !query.includes("1024px"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Toggle sidebar" }));

    const sheet = await screen.findByRole("dialog");
    const mobileSidebar = within(sheet).getByRole("navigation", {
      name: "Sidebar navigation",
    });
    await waitFor(() =>
      expect(
        within(mobileSidebar).getByRole("button", { name: /^Existing chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(
      within(mobileSidebar).getByLabelText("Topic actions for Existing chat"),
      { button: 0 },
    );

    const deleteItem = await within(sheet).findByRole("menuitem", {
      name: "Delete",
    });
    expect(deleteItem).toBeInTheDocument();

    fireEvent.click(deleteItem);
    await waitFor(() =>
      expect(screen.getByText("Delete this topic?")).toBeInTheDocument(),
    );
  }, 15_000);

  it("applies persisted sidebar workspace state from the gateway", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: ["websocket:chat-b"],
      archived_keys: ["websocket:chat-a"],
      title_overrides: { "websocket:chat-b": "Roadmap" },
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "updated_desc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByText("Pinned")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^Roadmap$/ })).toBeInTheDocument();
    expect(within(sidebar).queryByRole("button", { name: /^First chat$/ })).not.toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "Show archived" }));
    await waitFor(() =>
      expect(within(sidebar).getByText("Archived")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^First chat$/ })).toBeInTheDocument();
    expect(setSidebarStateSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        view: expect.objectContaining({ show_archived: true }),
      }),
    );

    expect(within(sidebar).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });

  it("sorts chats by displayed title when A-Z is persisted", async () => {
    mockSessions = [
      {
        key: "websocket:zulu",
        channel: "websocket",
        chatId: "zulu",
        createdAt: "2026-04-16T12:00:00Z",
        updatedAt: "2026-04-16T12:00:00Z",
        title: "Zulu work",
        preview: "later",
      },
      {
        key: "websocket:new",
        channel: "websocket",
        chatId: "new",
        createdAt: "2026-04-15T12:00:00Z",
        updatedAt: "2026-04-15T12:00:00Z",
        preview: "hi nanoinfra",
      },
      {
        key: "websocket:alpha",
        channel: "websocket",
        chatId: "alpha",
        createdAt: "2026-04-14T12:00:00Z",
        updatedAt: "2026-04-14T12:00:00Z",
        title: "Alpha plan",
        preview: "earlier",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: [],
      archived_keys: [],
      title_overrides: {},
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "title_asc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByText("Topics")).toBeInTheDocument(),
    );
    const group = within(sidebar).getByText("Topics").closest("section");
    expect(group).toBeTruthy();
    const labels = within(group as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim())
      .filter(Boolean);

    expect(labels).toEqual(["Alpha plan", "New topic", "Zulu work"]);
  });

  it("shows running and completed session indicators in the sidebar", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Working chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Quiet chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Working chat$/ }),
      ).toBeInTheDocument(),
    );

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", 12_345);
    });
    expect(within(sidebar).getByTitle("Agent running")).toBeInTheDocument();

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", null);
    });
    expect(within(sidebar).queryByTitle("Agent running")).not.toBeInTheDocument();
    expect(within(sidebar).getByTitle("New activity")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Working chat$/ }));
    });
    expect(within(sidebar).queryByTitle("New activity")).not.toBeInTheDocument();
  });

  it("does not show an updated dot later when the active session finishes", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Active work",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Other chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Active work$/ }),
      ).toBeInTheDocument(),
    );

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Active work$/ }));
    });
    await waitFor(() => expect(document.title).toContain("Active work"));

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", 12_345);
    });
    expect(within(sidebar).getByTitle("Agent running")).toBeInTheDocument();

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", null);
    });
    expect(within(sidebar).queryByTitle("Agent running")).not.toBeInTheDocument();
    expect(within(sidebar).queryByTitle("New activity")).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Other chat$/ }));
    });
    expect(within(sidebar).queryByTitle("New activity")).not.toBeInTheDocument();
  });

  it("marks inactive sessions when a thread update arrives", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Open chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Scheduled update target",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Open chat$/ }));
    });

    act(() => {
      for (const handler of sessionUpdateHandlers) handler("chat-b", "thread");
    });

    expect(within(sidebar).getByTitle("New activity")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Scheduled update target$/ }));
    });

    expect(within(sidebar).queryByTitle("New activity")).not.toBeInTheDocument();
  });

  it("restores sidebar run indicators after a page reload", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Running after reload",
        runStartedAt: 12_345,
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Completed after reload",
      },
    ];
    localStorage.setItem(
      "nanoinfra-webui.sidebar.session-updates.v1",
      JSON.stringify(["chat-b"]),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByTitle("Agent running")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByTitle("New activity")).toBeInTheDocument();
    expect(attachSpy).toHaveBeenCalledWith("chat-a");
  });

  it("restores the active chat from the URL hash after a page reload", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Active after reload",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Other chat",
      },
    ];
    window.history.replaceState(
      null,
      "",
      `/#/chat/${encodeURIComponent("websocket:chat-a")}`,
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    await waitFor(() => expect(document.title).toBe("Active after reload · nanoinfra"));
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(
      within(sidebar).getByRole("button", { name: /^Active after reload$/ }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe(
      `#/chat/${encodeURIComponent("websocket:chat-a")}`,
    );
  });

  it("opens the settings view from the sidebar footer", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const href = String(input);
        if (href === "/api/settings/provider-models?provider=openai") {
          return jsonResponse({
            provider: "openai",
            label: "OpenAI",
            status: "available",
            catalog_kind: "official",
            models: [
              { id: "openai/gpt-4o", owned_by: "openai", context_window: 128000 },
              { id: "openai/gpt-4o-mini", owned_by: "openai", context_window: 128000 },
            ],
            model_count: 2,
            fetched_at: 1,
          });
        }
        if (href.includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "auto",
                resolved_provider: "openai",
                has_api_key: true,
                model_preset: "primary",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                timezone: "UTC",
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
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
                {
                  name: "deep",
                  label: "deep",
                  active: false,
                  is_default: false,
                  model: "anthropic/claude-opus-4-5",
                  provider: "anthropic",
                  max_tokens: 8192,
                  context_window_tokens: 200000,
                  temperature: 0.1,
                  reasoning_effort: "high",
                },
              ],
              model_call_order: ["primary", "deep"],
              model_call_order_editable: true,
              providers: [
                {
                  name: "openai",
                  label: "OpenAI",
                  configured: true,
                  api_key_hint: "open••••-key",
                },
                {
                  name: "openrouter",
                  label: "OpenRouter",
                  configured: false,
                  api_key_required: true,
                  default_api_base: "https://openrouter.ai/api/v1",
                },
                {
                  name: "ant_ling",
                  label: "Ant Ling",
                  configured: false,
                  api_key_required: true,
                  default_api_base: "https://api.ant-ling.com/v1",
                },
                {
                  name: "azure_openai",
                  label: "Azure OpenAI",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "huggingface",
                  label: "Hugging Face",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "siliconflow",
                  label: "SiliconFlow",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "volcengine",
                  label: "VolcEngine",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "byteplus",
                  label: "BytePlus",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "qianfan",
                  label: "Qianfan",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "atomic_chat",
                  label: "Atomic Chat",
                  configured: false,
                  api_key_required: false,
                  default_api_base: "http://localhost:1337/v1",
                },
              ],
              web_search: {
                provider: "brave",
                api_key_hint: "BSAo••••ew20",
                base_url: null,
                max_results: 5,
                timeout: 30,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                  { name: "tavily", label: "Tavily", credential: "api_key" },
                ],
              },
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                search: { max_results: 5, timeout: 30 },
                fetch: { use_jina_reader: true },
              },
              image_generation: {
                enabled: false,
                provider: "openrouter",
                provider_configured: true,
                model: "openai/gpt-5.4-image-2",
                default_aspect_ratio: "1:1",
                default_image_size: "1K",
                max_images_per_turn: 4,
                save_dir: "generated",
                providers: [
                  {
                    name: "openrouter",
                    label: "OpenRouter",
                    configured: true,
                    api_key_hint: "sk-o••••test",
                    api_base: "https://openrouter.ai/api/v1",
                    default_api_base: "https://openrouter.ai/api/v1",
                  },
                  {
                    name: "gemini",
                    label: "Gemini",
                    configured: false,
                    api_key_hint: null,
                    api_base: null,
                    default_api_base: "https://generativelanguage.googleapis.com/v1beta/openai/",
                  },
                ],
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 18790,
                heartbeat: {
                  enabled: true,
                  interval_s: 1800,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "every 2h",
                },
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
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    localStorage.setItem(
      "nanoinfra-webui.settings-preferences",
      JSON.stringify({ brandLogos: true }),
    );
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const searchButton = within(sidebar).getByRole("button", { name: "Search" });
    const appsButton = within(sidebar).getByRole("button", { name: "Apps" });
    expect(searchButton.compareDocumentPosition(appsButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));

    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Overview" })).not.toBeInTheDocument();
    expect(document.title).toBe("Settings · nanoinfra");
    expect(screen.getByTestId("overview-logo-openai")).toBeInTheDocument();
    expect(screen.getByTestId("overview-logo-brave")).toBeInTheDocument();
    expect(screen.getByTestId("overview-logo-openrouter")).toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-nanoinfra-gateway")).not.toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-nanoinfra-workspace")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Sidebar navigation" })).not.toBeInTheDocument();
    const settingsNav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(settingsNav.className).not.toContain("overflow-x-auto");
    expect(within(settingsNav).getByRole("button", { name: "Settings: Overview" })).toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(settingsNav).getByRole("button", { name: "Models" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "Providers" })).not.toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Image" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "Files" })).not.toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Web" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "Apps" })).not.toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Security" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    fireEvent.pointerDown(within(settingsNav).getByRole("button", { name: "Settings: Overview" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Appearance" }));
    expect(screen.getByText("Brand logos")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Brand logos" })).toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Settings: Appearance" })).toBeInTheDocument();
    fireEvent.pointerDown(within(settingsNav).getByRole("button", { name: "Settings: Appearance" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Models" }));
    expect(screen.queryByText("AI")).not.toBeInTheDocument();
    expect(screen.getByText("Model presets")).toBeInTheDocument();
    expect(screen.queryByText("Model call order")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New model preset" }));
    expect(screen.queryByRole("dialog", { name: "New model preset" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Fast writing"), {
      target: { value: "Fast writing" },
    });
    expect(
      screen
        .getAllByRole("button", { name: /OpenAI/ })
        .some((button) => button.getAttribute("aria-haspopup") === "menu"),
    ).toBe(true);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select model" }));
    fireEvent.click(await screen.findByText("openai/gpt-4o-mini"));
    expect(screen.getByRole("button", { name: "Save preset" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Up to date.")).not.toBeInTheDocument();
    fireEvent.click(
      within(screen.getByTestId("model-call-order-row-primary")).getAllByRole("button")[0],
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /Auto/ }));
    expect(screen.getAllByTestId("provider-picker-logo-openai").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("menuitem", { name: /Auto/ }));
    const openModelPicker = () => {
      const modelButtons = screen.getAllByRole("button", { name: /openai\/gpt-4o/ });
      fireEvent.pointerDown(modelButtons[modelButtons.length - 1]);
    };
    openModelPicker();
    await screen.findByText("openai/gpt-4o-mini");
    fireEvent.click(screen.getAllByText("openai/gpt-4o-mini")[0]);
    expect(screen.queryByText("Unsaved changes.")).not.toBeInTheDocument();
    expect(screen.getByText("Model providers")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add your own model provider" })).toBeInTheDocument();
    expect(screen.queryByText("OpenRouter")).not.toBeInTheDocument();
    expect(screen.queryByText("Ant Ling")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "Bring your own provider keys. Nanoinfra reads these values from the current config and only configured providers can be used in model presets.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("azure_openai")).not.toBeInTheDocument();
    expect(screen.getByTestId("provider-logo-openai")).toBeInTheDocument();
    expect(screen.queryByText(/Product names, logos, and brands/)).not.toBeInTheDocument();
    expect(screen.queryByText("Not configured")).not.toBeInTheDocument();
    const clickProviderRow = async (label: string) => {
      const providerLabel = (await screen.findAllByText(label))
        .find((element) => element.className.includes("font-semibold"));
      expect(providerLabel).toBeTruthy();
      fireEvent.click(providerLabel!);
    };
    const chooseProvider = async (label: string) => {
      fireEvent.pointerDown(
        screen.getByRole("button", { name: "Add your own model provider" }),
      );
      fireEvent.click(await screen.findByRole("menuitem", { name: label }));
    };
    await clickProviderRow("OpenAI");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByPlaceholderText("Leave blank to keep the current key"), {
      target: { value: "unsaved-openai-key" },
    });
    await clickProviderRow("OpenAI");
    await chooseProvider("OpenRouter");
    await clickProviderRow("OpenRouter");
    await clickProviderRow("OpenAI");
    expect(screen.getByText("open••••-key")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("unsaved-openai-key")).not.toBeInTheDocument();
    await clickProviderRow("OpenAI");
    await chooseProvider("Ant Ling");
    expect(screen.getByDisplayValue("https://api.ant-ling.com/v1")).toBeInTheDocument();
    await clickProviderRow("Ant Ling");
    await chooseProvider("Atomic Chat");
    expect(screen.getByDisplayValue("http://localhost:1337/v1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save provider" })).toBeEnabled();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "Image" }));
    expect(screen.queryByRole("heading", { name: "Image" })).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Image generation" })).toBeInTheDocument();
    expect(screen.getByText("Provider status")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "openai/gpt-5.4-image-2" })).toBeInTheDocument();
    expect(screen.getByText("Save directory")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "Web" }));
    expect(screen.getByText("Search provider")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Jina reader" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Brave Search/ })).toBeInTheDocument();
    expect(screen.getByTestId("provider-picker-logo-brave")).toBeInTheDocument();
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByPlaceholderText("Leave blank to keep the current key"), {
      target: { value: "unsaved-brave-key" },
    });
    fireEvent.pointerDown(screen.getByRole("button", { name: /Brave Search/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Tavily" }));
    fireEvent.pointerDown(screen.getByRole("button", { name: /Tavily/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Brave Search" }));
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("unsaved-brave-key")).not.toBeInTheDocument();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "System" }));
    expect(screen.getByText("Regional")).toBeInTheDocument();
    expect(screen.getByText("Timezone")).toBeInTheDocument();
    expect(screen.queryByText("Bot name")).not.toBeInTheDocument();
    expect(screen.queryByText("Bot icon")).not.toBeInTheDocument();
    expect(screen.queryByText("Tool hint length")).not.toBeInTheDocument();
    expect(screen.queryByText("Heartbeat")).not.toBeInTheDocument();
    expect(screen.queryByText("Dream")).not.toBeInTheDocument();
    expect(screen.queryByText("Unified session")).not.toBeInTheDocument();
    expect(screen.getByText("Default workspace")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.pointerDown(screen.getByRole("button", { name: "UTC" }));
    expect(screen.getByPlaceholderText("Search timezone")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search timezone"), {
      target: { value: "Shanghai" },
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Asia\/Shanghai/ }));
    expect(screen.getByRole("button", { name: "Asia/Shanghai" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("restores the settings section from the URL hash after a page reload", async () => {
    mockFetchRoutes({ "/api/settings": baseSettingsPayload() });
    window.history.replaceState(null, "", "/#/settings?section=voice");

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    expect(await screen.findByRole("heading", { name: "Voice input" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=voice");
  });

  it("falls back to Overview for the retired Files settings URL", async () => {
    mockFetchRoutes({ "/api/settings": baseSettingsPayload() });
    window.history.replaceState(null, "", "/#/settings?section=files");

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("updates the URL hash when switching settings sections", async () => {
    mockFetchRoutes({ "/api/settings": baseSettingsPayload() });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));
    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings");

    const settingsNav = screen.getByRole("navigation", { name: "Settings sections" });
    const overviewButton = within(settingsNav).getByRole("button", {
      name: "Overview",
      exact: true,
    });
    const modelsButton = within(settingsNav).getByRole("button", {
      name: "Models",
      exact: true,
    });
    const settingsHighlight = within(settingsNav).getByTestId(
      "settings-selection-highlight",
    );

    expect(overviewButton).toHaveAttribute("aria-current", "page");
    expect(overviewButton).not.toHaveClass("bg-sidebar-accent");
    expect(overviewButton).toHaveClass("transition-[color]");
    expect(settingsHighlight).toHaveAttribute("data-active-id", "overview");

    fireEvent.click(modelsButton);

    expect(await screen.findByText("Model presets")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Models" })).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=models");
    expect(modelsButton).toHaveAttribute("aria-current", "page");
    expect(settingsHighlight).toHaveAttribute("data-active-id", "models");

    const voiceButton = within(settingsNav).getByRole("button", {
      name: "Voice",
      exact: true,
    });
    fireEvent.click(voiceButton);

    expect(await screen.findByRole("heading", { name: "Voice input" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=voice");
    expect(voiceButton).toHaveAttribute("aria-current", "page");
    expect(settingsHighlight).toHaveAttribute("data-active-id", "voice");
  });

  it("transitions between Apps and Skills without replacing the sidebar", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/settings/cli-apps": { apps: [], installed_count: 0, catalog_updated_at: "2026-04-18" },
      "/api/settings/mcp-presets": { presets: [], installed_count: 0 },
      "/api/webui/skills": { skills: [] },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const appsButton = within(sidebar).getByRole("button", { name: "Apps" });

    fireEvent.click(appsButton);

    expect(await screen.findByRole("heading", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sidebar navigation" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Settings sections" })).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Apps" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(sidebar).getByTestId("actions-selection-highlight")).toHaveAttribute(
      "data-active-id",
      "utility:apps",
    );
    expect(within(sidebar).queryAllByRole("button", { current: "page" })).toHaveLength(1);
    expect(screen.getByTestId("settings-section-transition")).toHaveAttribute(
      "data-settings-section",
      "apps",
    );
    expect(screen.getByTestId("settings-section-transition")).toHaveClass(
      "animate-in",
      "fade-in-0",
      "slide-in-from-bottom-1",
      "duration-200",
      "motion-reduce:animate-none",
    );
    expect(document.title).toBe("Apps · nanoinfra");

    fireEvent.click(within(sidebar).getByRole("button", { name: "Skills" }));

    expect(await screen.findByRole("heading", { name: "Skills" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("settings-section-transition")).toHaveAttribute(
        "data-settings-section",
        "skills",
      );
    });
    expect(screen.getByRole("navigation", { name: "Sidebar navigation" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Skills" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(sidebar).getByTestId("actions-selection-highlight")).toHaveAttribute(
      "data-active-id",
      "utility:skills",
    );
    expect(document.title).toBe("Skills · nanoinfra");
  });

  it("returns from settings to the blank start page when no session was active", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "openai",
                resolved_provider: "openai",
                has_api_key: true,
                model_preset: "default",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                timezone: "UTC",
                tool_hint_max_length: 40,
              },
              model_presets: [
                {
                  name: "default",
                  label: "Default",
                  active: true,
                  is_default: true,
                  model: "openai/gpt-4o",
                  provider: "openai",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
              ],
              providers: [{ name: "openai", label: "OpenAI", configured: true }],
              web_search: {
                provider: "duckduckgo",
                api_key_hint: null,
                base_url: null,
                max_results: 5,
                timeout: 30,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                ],
              },
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                search: { max_results: 5, timeout: 30 },
                fetch: { use_jina_reader: true },
              },
              image_generation: {
                enabled: false,
                provider: "openrouter",
                provider_configured: false,
                model: "openai/gpt-5.4-image-2",
                default_aspect_ratio: "1:1",
                default_image_size: "1K",
                max_images_per_turn: 4,
                save_dir: "generated",
                providers: [
                  {
                    name: "openrouter",
                    label: "OpenRouter",
                    configured: false,
                    api_key_hint: null,
                    api_base: null,
                    default_api_base: "https://openrouter.ai/api/v1",
                  },
                ],
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 18790,
                heartbeat: {
                  enabled: true,
                  interval_s: 1800,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "every 2h",
                },
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
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New topic" }));
    await waitFor(() => expect(document.title).toBe("nanoinfra"));

    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));
    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));

    await waitFor(() => expect(document.title).toBe("nanoinfra"));
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
  });

  it("filters sessions in the centered search dialog", async () => {
    mockSessions = [
      {
        key: "websocket:chat-alpha",
        channel: "websocket",
        chatId: "chat-alpha",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        title: "Q2 roadmap",
        preview: "Project planning notes",
      },
      {
        key: "websocket:chat-beta",
        channel: "websocket",
        chatId: "chat-beta",
        createdAt: "2026-04-15T10:00:00Z",
        updatedAt: "2026-04-15T10:00:00Z",
        preview: "Travel ideas",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();
    const newChatButton = within(sidebar).getByRole("button", { name: "New topic" });
    const searchButton = within(sidebar).getByRole("button", { name: "Search" });
    expect(
      newChatButton.compareDocumentPosition(searchButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(searchButton);
    const dialog = await screen.findByRole("dialog", { name: "Search" });
    expect(dialog).toHaveClass("origin-center");
    expect(dialog.className).not.toContain("translate-x");
    expect(dialog.className).not.toContain("translate-y");
    expect(dialog.querySelector("kbd")).toBeNull();
    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).getByText("Travel ideas")).toBeInTheDocument();
    expect(within(dialog).queryByText("websocket")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("#1")).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search" }), {
      target: { value: "planning" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search" }), {
      target: { value: "road q2" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /Q2 roadmap/ }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Search" })).not.toBeInTheDocument(),
    );
  });

  it("opens search from the keyboard shortcut", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "k", metaKey: true });

    const dialog = await screen.findByRole("dialog", { name: "Search" });
    expect(within(dialog).queryByText("Global actions")).not.toBeInTheDocument();
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    const textbox = within(dialog).getByRole("textbox", { name: "Search" });
    fireEvent.change(textbox, { target: { value: "missing" } });
    expect(within(dialog).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.change(textbox, { target: { value: "existing" } });
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    fireEvent.keyDown(textbox, { key: "Enter" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Search" })).not.toBeInTheDocument(),
    );
    expect(createChatSpy).not.toHaveBeenCalled();
  });

  it.each([
    ["Command", { metaKey: true }],
    ["Control", { ctrlKey: true }],
  ])("starts a new chat from the %s keyboard shortcut", async (_label, modifier) => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "O", shiftKey: true, ...modifier });

    expect(window.location.hash).toBe("#/new");
  });

  it("closes search when starting a new chat from the keyboard shortcut", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "Search" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "O", shiftKey: true, metaKey: true });

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Search" })).not.toBeInTheDocument(),
    );
    expect(window.location.hash).toBe("#/new");
  });

  it("exposes the new chat keyboard shortcut in the sidebar title", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });

    const newChatButton = within(sidebar).getByRole("button", { name: "New topic" });
    expect(newChatButton).toHaveAttribute(
      "title",
      "New topic (Ctrl+Shift+O)",
    );
    expect(newChatButton).toHaveAttribute(
      "aria-keyshortcuts",
      "Meta+Shift+O Control+Shift+O",
    );
  });

  it("uses macOS shortcut glyphs in the sidebar title", async () => {
    setNavigatorPlatform("MacIntel");
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });

    expect(within(sidebar).getByRole("button", { name: "New topic" })).toHaveAttribute(
      "title",
      "New topic (⌘⇧O)",
    );
  });

  it("keeps large sidebars light while search still covers every chat", async () => {
    mockSessions = Array.from({ length: 170 }, (_, index) => {
      const chatId = `chat-${index}`;
      return {
        key: `websocket:${chatId}`,
        channel: "websocket" as const,
        chatId,
        createdAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        updatedAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        title: index === 169 ? "Hidden target" : `Bulk chat ${index}`,
        preview: "",
      };
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByRole("button", { name: "Bulk chat 0" })).toBeInTheDocument(),
    );
    expect(within(sidebar).queryByText("Hidden target")).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Show 10 more" })).toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "Search" }));
    const dialog = await screen.findByRole("dialog", { name: "Search" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search" }), {
      target: { value: "hidden" },
    });
    expect(within(dialog).getByText("Hidden target")).toBeInTheDocument();
  });

  it("opens a blank start page without creating an empty chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    const matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("1024px"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", matchMedia);

    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Toggle theme from header" }));
    expect(toggleThemeSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    const sidebarAside = container.querySelector("aside.lg\\:block") as HTMLElement;
    await waitFor(() => expect(sidebarAside.style.width).toBe("56px"));

    expect(screen.queryByRole("button", { name: "Start a new topic" })).not.toBeInTheDocument();
    const rail = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(within(rail).getByRole("button", { name: "New topic" })).toBeInTheDocument();
    expect(within(rail).getByRole("button", { name: "Search" })).toBeInTheDocument();
    expect(within(rail).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
    expect(within(rail).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.click(within(rail).getByRole("button", { name: "Toggle sidebar" }));
    await waitFor(() => expect(sidebarAside.style.width).toBe("272px"));

    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New topic" }));
    expect(createChatSpy).not.toHaveBeenCalled();
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a new topic" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Toggle theme from header" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Settings" })).toBeInTheDocument();

    expect(within(sidebar).getByText("Existing chat")).toBeInTheDocument();
  });

  it("refreshes the bootstrap token before REST settings auth expires", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchBootstrap)
      .mockResolvedValueOnce({
        token: "tok-1",
        api_token: "api-tok-1",
        ws_path: "/",
        expires_in: 30,
      })
      .mockResolvedValueOnce({
        token: "tok-2",
        api_token: "api-tok-2",
        ws_path: "/",
        expires_in: 300,
      });
    vi.mocked(deriveWsUrl).mockImplementation(
      (_wsPath: string, token: string) => `ws://test?token=${token}`,
    );

    const { unmount } = render(<App />);
    await act(async () => {});

    expect(connectSpy).toHaveBeenCalled();
    expect(fetchBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(fetchBootstrap).toHaveBeenCalledTimes(2);
    expect(updateUrlSpy).toHaveBeenCalledWith("ws://test?token=tok-2");
    unmount();
  });

  it("reuses an in-flight pairing poll when the page becomes visible again", async () => {
    let resolvePairing!: (response: Response) => void;
    const pendingPairing = new Promise<Response>((resolve) => {
      resolvePairing = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => (
      String(input) === "/api/settings/pairing"
        ? pendingPairing
        : Promise.resolve({ ok: false, status: 404 } as Response)
    ));
    vi.stubGlobal("fetch", fetchMock);
    const visibilityDescriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");

    const setVisibility = (state: DocumentVisibilityState) => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: state,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    };

    try {
      render(<App />);
      await waitFor(() => {
        expect(fetchMock.mock.calls.filter(([input]) => (
          String(input) === "/api/settings/pairing"
        ))).toHaveLength(1);
      });

      act(() => setVisibility("hidden"));
      act(() => setVisibility("visible"));

      expect(fetchMock.mock.calls.filter(([input]) => (
        String(input) === "/api/settings/pairing"
      ))).toHaveLength(1);
      await act(async () => {
        resolvePairing(jsonResponse({ requests: [] }));
        await pendingPairing;
      });
    } finally {
      if (visibilityDescriptor) {
        Object.defineProperty(document, "visibilityState", visibilityDescriptor);
      } else {
        delete (document as Document & {
          visibilityState?: DocumentVisibilityState;
        }).visibilityState;
      }
    }
  });
});
