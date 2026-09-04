import type {
  AgentPluginsPayload,
  ApiServicePayload,
  AutomationsPayload,
  AutomationUpdatePayload,
  CommissioningResult,
  GrantPromotionResult,
  ChannelConfigurePayload,
  ChannelConnectPayload,
  ChannelValidationPayload,
  ChatSummary,
  CliAppsPayload,
  ConnectorConsentStart,
  ConnectorObjectsPayload,
  ConnectorTestResult,
  ConnectorsPayload,
  FilePreviewPayload,
  GatesApprovalAnswer,
  GatesApprovalAnswerValues,
  GatesApprovalsPayload,
  GatesAuditPage,
  GatesAuditQuery,
  GatesLatchClearPayload,
  GatesLatchClearValues,
  GatesLatchPayload,
  GatesPolicy,
  ImageGenerationSettingsUpdate,
  KnowledgeSettingsUpdate,
  McpPresetsPayload,
  MarketplaceProvider,
  NanoinfraFeaturesPayload,
  ModelConfigurationCreate,
  ModelConfigurationUpdate,
  NamedAgentsSaveRequest,
  AgentDefaultsSaveRequest,
  NetworkSafetySettingsUpdate,
  PairingPayload,
  ProviderCreationUpdate,
  ProviderModelsPayload,
  ProviderOAuthCompletionResult,
  ProviderOAuthLoginResult,
  ProviderSettingsUpdate,
  SessionDeleteResult,
  SessionAutomationsPayload,
  SettingsPayload,
  SettingsUpdate,
  SidebarStatePayload,
  SkillDetail,
  SkillActionPayload,
  SkillInstallPayload,
  SkillsPayload,
  SkillsSearchPayload,
  SkillsTrendsPayload,
  SkillsTrendingPayload,
  SlashCommand,
  SlashCommandLifecycle,
  TranscriptionSettingsUpdate,
  WebSearchSettingsUpdate,
  WorkspacesPayload,
  WebuiThreadPersistedPayload,
  WorkspaceListingPayload,
  WorkspaceProjectsPayload,
  WorkspaceScopePayload,
} from "./types";
// Diagram payload shapes live with the feature (componentCatalog.ts,
// diagramTypes.ts) rather than in ./types — `types.ts` is a deliberately
// standalone leaf module with no cross-imports, and the diagram/catalog
// record shapes already have a single canonical home in that feature
// folder, used throughout webui/src/components/diagrams/.
import type { DiagramCatalogPayload } from "@/components/diagrams/componentCatalog";
import type { Diagram, DiagramSummary } from "@/components/diagrams/diagramTypes";
import { DEFAULT_HTTP_TIMEOUT_MS, fetchWithTimeout } from "./http";

const API_READ_TIMEOUT_MS = 20_000;
const SLASH_COMMAND_LIFECYCLES = new Set<SlashCommandLifecycle>([
  "side_channel",
  "finalize_active_turn",
  "stop_active_turn",
  "agent_turn",
  "agent_turn_with_args",
]);

function isSlashCommandLifecycle(value: unknown): value is SlashCommandLifecycle {
  return (
    typeof value === "string"
    && SLASH_COMMAND_LIFECYCLES.has(value as SlashCommandLifecycle)
  );
}
const CHANNEL_VALUES_HEADER = "X-Nanoinfra-Channel-Values";
const API_SERVICE_VALUES_HEADER = "X-Nanoinfra-API-Service-Values";
const OAUTH_CODE_HEADER = "X-Nanoinfra-OAuth-Code";
const OAUTH_CALLBACK_HEADER = "X-Nanoinfra-OAuth-Callback";
const PROVIDER_VALUES_HEADER = "X-Nanoinfra-Provider-Values";
const DIAGRAM_VALUES_HEADER = "X-Nanoinfra-Diagram-Values";
const DIAGRAM_CHUNK_COUNT_HEADER = "X-Nanoinfra-Diagram-Chunks";

/**
 * Bytes per chunk of a diagram body. Must equal `_DIAGRAM_CHUNK_BYTES` in
 * `nanoinfra/webui/ws_http.py`, and a Python test asserts that rather than trusting this comment.
 *
 * The whole body used to travel in one header on a GET, and the gateway drops a request line over
 * 8192 bytes with no status code and no error body — the connection closes and this client reported
 * "Failed to save: <network error>". The limit is bytes and not nodes, so an operator could not
 * predict where their canvas stopped being savable.
 *
 * `websockets`' `process_request` exposes the request line and headers and never a body, so a POST
 * body is not available on this transport. Chunked headers are what it can carry.
 */
const DIAGRAM_CHUNK_BYTES = 6000;

/**
 * One JSON payload as numbered header chunks -- the mirror of `_chunked_headers` in
 * `nanoinfra/webui/ws_http.py`, which the diagram body, a server's notes and the agent roster all
 * travel through.
 *
 * One function rather than one per payload, for the reason the Python side gives for its own
 * extraction: three copies of a split cannot be kept in step with the reader, and the failure mode
 * of drifting is a dropped connection with no status code.
 */
function chunkedValuesHeaders(
  header: string,
  countHeader: string,
  payload: unknown,
): Record<string, string> {
  const encoded = encodeURIComponent(JSON.stringify(payload));
  const chunks: string[] = [];
  for (let index = 0; index < encoded.length; index += DIAGRAM_CHUNK_BYTES) {
    chunks.push(encoded.slice(index, index + DIAGRAM_CHUNK_BYTES));
  }
  if (chunks.length === 0) chunks.push("");
  const headers: Record<string, string> = { [countHeader]: String(chunks.length) };
  chunks.forEach((chunk, index) => {
    headers[`${header}-${index}`] = chunk;
  });
  return headers;
}

function diagramValuesHeaders(diagram: Diagram): Record<string, string> {
  return chunkedValuesHeaders(DIAGRAM_VALUES_HEADER, DIAGRAM_CHUNK_COUNT_HEADER, diagram);
}
const SERVER_NOTES_HEADER = "X-Nanoinfra-Server-Notes";
const SERVER_NOTES_CHUNK_COUNT_HEADER = "X-Nanoinfra-Server-Notes-Chunks";

/** Same split as a diagram body, and for the same transport reason (#229). */
function serverNotesHeaders(payload: unknown): Record<string, string> {
  return chunkedValuesHeaders(SERVER_NOTES_HEADER, SERVER_NOTES_CHUNK_COUNT_HEADER, payload);
}
const AGENTS_HEADER = "X-Nanoinfra-Agents";
const WORKSPACE_PROMPT_HEADER = "X-Nanoinfra-Workspace-Prompt";
const WORKSPACE_PROMPT_CHUNK_COUNT_HEADER = "X-Nanoinfra-Workspace-Prompt-Chunks";
const AGENTS_CHUNK_COUNT_HEADER = "X-Nanoinfra-Agents-Chunks";
const SECRET_VALUES_HEADER = "X-Nanoinfra-Secret-Values";
const WORKSPACE_VALUES_HEADER = "X-Nanoinfra-Workspace-Values";
const SERVER_VALUES_HEADER = "X-Nanoinfra-Server-Values";
const GATES_VALUES_HEADER = "X-Nanoinfra-Gates-Values";
const LATCH_VALUES_HEADER = "X-Nanoinfra-Latch-Values";
const APPROVAL_VALUES_HEADER = "X-Nanoinfra-Approval-Values";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  timeoutMs: number = 0,
): Promise<T> {
  const res = await fetchWithTimeout(
    url,
    {
      ...(init ?? {}),
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      credentials: "same-origin",
    },
    timeoutMs,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    const text = typeof res.text === "function" ? await res.text() : "";
    const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype");
    throw new ApiError(
      res.status,
      isHtml
        ? "Gateway returned WebUI HTML instead of JSON. Restart nanoinfra gateway and try again."
        : "Gateway returned a non-JSON response.",
    );
  }
  return (await res.json()) as T;
}

function mcpValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "X-Nanoinfra-MCP-Values": JSON.stringify(payload) };
}

function automationValuesHeader(values: AutomationUpdatePayload): HeadersInit {
  return { "X-Nanoinfra-Automation-Values": encodeURIComponent(JSON.stringify(values)) };
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    model_preset?: string | null;
    run_started_at?: number | null;
    workspace_scope?: WorkspaceScopePayload | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    modelPreset: s.model_preset ?? null,
    runStartedAt: s.run_started_at ?? null,
    workspaceScope: s.workspace_scope ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export interface FetchWebuiThreadOptions {
  limit?: number;
  direction?: "latest";
  before?: string | null;
  signal?: AbortSignal;
}

export async function fetchWebuiThread(
  token: string,
  key: string,
  optionsOrBase?: FetchWebuiThreadOptions | string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const options = typeof optionsOrBase === "string" ? undefined : optionsOrBase;
  const resolvedBase = typeof optionsOrBase === "string" ? optionsOrBase : base;
  const params = new URLSearchParams();
  if (options?.limit !== undefined) params.set("limit", String(options.limit));
  if (options?.direction) params.set("direction", options.direction);
  if (options?.before) params.set("before", options.before);
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  const url = `${resolvedBase}/api/sessions/${encodeURIComponent(key)}/webui-thread${suffix}`;
  const res = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
    cache: "no-store",
    signal: options?.signal,
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function fetchFilePreview(
  token: string,
  key: string,
  path: string,
  base: string = "",
): Promise<FilePreviewPayload> {
  const query = new URLSearchParams();
  query.set("path", path);
  return request<FilePreviewPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/file-preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/** The workspaces under `tools.workspacesRoot`, plus the configured one. */
export async function fetchWorkspaceProjects(
  token: string,
  base: string = "",
): Promise<WorkspaceProjectsPayload> {
  return request<WorkspaceProjectsPayload>(
    `${base}/api/webui/workspace/projects`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createWorkspaceProject(
  token: string,
  name: string,
  base: string = "",
): Promise<{ workspace: string }> {
  return request<{ workspace: string }>(
    `${base}/api/webui/workspace/projects/create`,
    token,
    { headers: { [WORKSPACE_VALUES_HEADER]: encodeURIComponent(JSON.stringify({ name })) } },
  );
}

/** One directory of a workspace, for the Workspaces explorer. */
export async function fetchWorkspaceListing(
  token: string,
  path: string | null,
  includeHidden: boolean = false,
  workspace: string | null = null,
  base: string = "",
): Promise<WorkspaceListingPayload> {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (includeHidden) query.set("hidden", "1");
  if (workspace) query.set("workspace", workspace);
  return request<WorkspaceListingPayload>(
    `${base}/api/webui/workspace/list?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/** One file's text, scoped to the workspace rather than to a chat session. */
export async function fetchWorkspaceFilePreview(
  token: string,
  path: string,
  workspace: string | null = null,
  base: string = "",
): Promise<FilePreviewPayload> {
  const query = new URLSearchParams();
  query.set("path", path);
  if (workspace) query.set("workspace", workspace);
  return request<FilePreviewPayload>(
    `${base}/api/webui/workspace/preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/** Arguments for one workspace mutation. `parent` null means the workspace root. */
interface WorkspaceMutation {
  /** Which workspace the parent path belongs to; null is the configured one. */
  workspace?: string | null;
  parent: string | null;
  name: string;
  newName?: string;
  /** Absolute destination directory for a move; null is the workspace root. */
  destination?: string | null;
  recursive?: boolean;
  /** Answer with the listing in the view the caller is showing. */
  includeHidden?: boolean;
}

function workspaceMutation(
  path: string,
  token: string,
  values: WorkspaceMutation,
  base: string,
): Promise<WorkspaceListingPayload> {
  // Every mutation answers with the fresh listing of the directory it touched, so
  // the explorer re-renders from what the server now holds rather than from a
  // guess about what the call did.
  return request<WorkspaceListingPayload>(
    `${base}${path}`,
    token,
    { headers: { [WORKSPACE_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)) } },
  );
}

export async function createWorkspaceFolder(
  token: string,
  parent: string | null,
  name: string,
  includeHidden: boolean = false,
  workspace: string | null = null,
  base: string = "",
): Promise<WorkspaceListingPayload> {
  return workspaceMutation(
    "/api/webui/workspace/mkdir",
    token,
    { parent, name, includeHidden, workspace },
    base,
  );
}

export async function renameWorkspaceEntry(
  token: string,
  parent: string | null,
  name: string,
  newName: string,
  includeHidden: boolean = false,
  workspace: string | null = null,
  base: string = "",
): Promise<WorkspaceListingPayload> {
  return workspaceMutation(
    "/api/webui/workspace/rename",
    token,
    { parent, name, newName, includeHidden, workspace },
    base,
  );
}

export async function moveWorkspaceEntry(
  token: string,
  parent: string | null,
  name: string,
  destination: string | null,
  includeHidden: boolean = false,
  workspace: string | null = null,
  base: string = "",
): Promise<WorkspaceListingPayload> {
  return workspaceMutation(
    "/api/webui/workspace/move",
    token,
    { parent, name, destination, includeHidden, workspace },
    base,
  );
}

export async function deleteWorkspaceEntry(
  token: string,
  parent: string | null,
  name: string,
  recursive: boolean,
  includeHidden: boolean = false,
  workspace: string | null = null,
  base: string = "",
): Promise<WorkspaceListingPayload> {
  return workspaceMutation(
    "/api/webui/workspace/delete",
    token,
    { parent, name, recursive, includeHidden, workspace },
    base,
  );
}

/**
 * One file's bytes, as a blob.
 *
 * Not `request`, which requires JSON: this body is deliberately
 * `application/octet-stream`. And not a plain `<a href>` either -- the route needs
 * the bearer token, which a link cannot carry.
 */
export async function downloadWorkspaceFile(
  token: string,
  path: string,
  workspace: string | null = null,
  base: string = "",
): Promise<Blob> {
  const query = new URLSearchParams();
  query.set("path", path);
  if (workspace) query.set("workspace", workspace);
  const res = await fetchWithTimeout(
    `${base}/api/webui/workspace/download?${query}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    },
    API_READ_TIMEOUT_MS,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  return res.blob();
}

export async function fetchFilePreviewAvailability(
  token: string,
  key: string,
  path: string,
  base: string = "",
): Promise<boolean> {
  const query = new URLSearchParams();
  query.set("path", path);
  query.set("probe", "1");
  const payload = await request<{ available?: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/file-preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return payload.available !== false;
}

export async function fetchSessionAutomations(
  token: string,
  key: string,
  base: string = "",
): Promise<SessionAutomationsPayload> {
  return request<SessionAutomationsPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAutomations(
  token: string,
  base: string = "",
): Promise<AutomationsPayload> {
  return request<AutomationsPayload>(
    `${base}/api/webui/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runAutomationAction(
  token: string,
  action: "enable" | "disable" | "delete" | "run",
  id: string,
  base: string = "",
): Promise<AutomationsPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<AutomationsPayload>(
    `${base}/api/webui/automations/${action}?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export interface AutomationStatePayload {
  id: string;
  values: Record<string, unknown>;
}

export async function fetchAutomationState(
  token: string,
  id: string,
  base: string = "",
): Promise<AutomationStatePayload> {
  return request<AutomationStatePayload>(
    `${base}/api/webui/automations/${encodeURIComponent(id)}/state`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function resetAutomationState(
  token: string,
  id: string,
  base: string = "",
): Promise<{ id: string; cleared: boolean }> {
  return request<{ id: string; cleared: boolean }>(
    `${base}/api/webui/automations/${encodeURIComponent(id)}/state/reset`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/**
 * Rehearse one automation now. It previews every gated action and takes none, so this writes
 * nothing except the verdict it comes back with.
 *
 * A model turn, so it takes as long as one: the timeout is the write timeout and not the read one.
 */
export async function commissionAutomation(
  token: string,
  id: string,
  base: string = "",
): Promise<CommissioningResult> {
  // No method. This gateway serves its HTTP surface from the WebSocket handshake hook, which
  // reads a request line and never a verb, so every mutating route here is a plain GET with its
  // values in the query or a header. A POST closes the connection, and the proxy in front reports
  // a 502 -- which is how this was found, against the real gateway rather than a stubbed fetch.
  return request<CommissioningResult>(
    `${base}/api/webui/automations/${encodeURIComponent(id)}/commission`,
    token,
  );
}

/**
 * Write the standing grant this automation's finding proposed.
 *
 * Deliberately sends no body. The grant comes off the automation's own record on the server: a
 * grant this client could name would be a grant this client chose.
 */
export async function grantAutomationCommissioning(
  token: string,
  id: string,
  base: string = "",
): Promise<GrantPromotionResult> {
  return request<GrantPromotionResult>(
    `${base}/api/webui/automations/${encodeURIComponent(id)}/grant`,
    token,
  );
}

export async function updateAutomation(
  token: string,
  id: string,
  values: AutomationUpdatePayload,
  base: string = "",
): Promise<AutomationsPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<AutomationsPayload>(
    `${base}/api/webui/automations/update?${query}`,
    token,
    {
      headers: automationValuesHeader(values),
    },
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/webui/skills`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkillDetail(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  return request<SkillDetail>(
    `${base}/api/webui/skills/${encodeURIComponent(name)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSkillEnabled(
  token: string,
  name: string,
  enabled: boolean,
  base: string = "",
): Promise<SkillActionPayload> {
  const params = new URLSearchParams({ name, enabled: String(enabled) });
  return request<SkillActionPayload>(
    `${base}/api/webui/skills/update?${params}`,
    token,
  );
}

export async function deleteSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillActionPayload> {
  const params = new URLSearchParams({ name });
  return request<SkillActionPayload>(
    `${base}/api/webui/skills/delete?${params}`,
    token,
  );
}

export async function searchMarketplaceSkills(
  token: string,
  query: string,
  provider: MarketplaceProvider = "all",
  base: string = "",
  /** One of `skill`, `agent-plugin`, `connector`. Narrows to that kind (#207). */
  kind: string = "",
): Promise<SkillsSearchPayload> {
  const params = new URLSearchParams({ q: query, provider });
  if (kind) params.set("kind", kind);
  return request<SkillsSearchPayload>(
    `${base}/api/webui/skills/search?${params}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchTrendingMarketplaceSkills(
  token: string,
  provider: MarketplaceProvider = "all",
  base: string = "",
): Promise<SkillsTrendingPayload> {
  const params = new URLSearchParams({ provider });
  return request<SkillsTrendingPayload>(
    `${base}/api/webui/skills/trending?${params}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchMarketplaceSkillTrends(
  token: string,
  skillIds: string[],
  base: string = "",
): Promise<SkillsTrendsPayload> {
  const params = new URLSearchParams();
  skillIds.forEach((id) => params.append("id", id));
  return request<SkillsTrendsPayload>(
    `${base}/api/webui/skills/trends?${params}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function installMarketplaceSkill(
  token: string,
  provider: Exclude<MarketplaceProvider, "all">,
  source: string,
  skill: string,
  version: string = "",
  base: string = "",
): Promise<SkillInstallPayload> {
  const params = new URLSearchParams({ provider, source, skill });
  if (version) params.set("version", version);
  return request<SkillInstallPayload>(
    `${base}/api/webui/skills/install?${params}`,
    token,
    undefined,
    150_000,
  );
}

export async function deleteSession(
  token: string,
  key: string,
  optionsOrBase?: { deleteAutomations?: boolean } | string,
  base: string = "",
): Promise<SessionDeleteResult> {
  const options = typeof optionsOrBase === "string" ? undefined : optionsOrBase;
  const resolvedBase = typeof optionsOrBase === "string" ? optionsOrBase : base;
  const query = new URLSearchParams();
  if (options?.deleteAutomations) query.set("delete_automations", "true");
  const suffix = query.toString() ? `?${query}` : "";
  return request<SessionDeleteResult>(
    `${resolvedBase}/api/sessions/${encodeURIComponent(key)}/delete${suffix}`,
    token,
  );
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSettingsUsage(
  token: string,
  base: string = "",
): Promise<NonNullable<SettingsPayload["usage"]>> {
  return request<NonNullable<SettingsPayload["usage"]>>(
    `${base}/api/settings/usage`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export interface VersionCheckResult {
  updateAvailable: {
    currentVersion: string;
    latestVersion: string;
    pypiUrl?: string;
  } | null;
}

export async function checkVersion(
  token: string,
  base: string = "",
): Promise<VersionCheckResult> {
  return request<VersionCheckResult>(
    `${base}/api/settings/version-check`,
    token,
    undefined,
    10_000,
  );
}

export async function fetchWorkspaces(
  token: string,
  base: string = "",
): Promise<WorkspacesPayload> {
  return request<WorkspacesPayload>(
    `${base}/api/workspaces`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/**
 * Read Agent Plugins state. There is no mutating counterpart on purpose: activation lives in
 * `tools.agentPlugins` and is reconciled by the executor, so a toggle here would be a second
 * authority. See nanoinfraorg/nanoinfra#141.
 */
export async function fetchAgentPlugins(
  token: string,
  base: string = "",
): Promise<AgentPluginsPayload> {
  return request<AgentPluginsPayload>(
    `${base}/api/settings/agent-plugins`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchInstalledCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps?installed_only=1`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchNanoinfraFeatures(
  token: string,
  base: string = "",
): Promise<NanoinfraFeaturesPayload> {
  return request<NanoinfraFeaturesPayload>(
    `${base}/api/settings/nanoinfra-features`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchApiService(token: string, base: string = ""): Promise<ApiServicePayload> {
  return request<ApiServicePayload>(`${base}/api/settings/api-service`, token);
}

export async function startApiService(
  token: string,
  values: { host: string; port: number; timeout: number; apiKey?: string },
  base: string = "",
): Promise<ApiServicePayload> {
  const query = new URLSearchParams({
    host: values.host,
    port: String(values.port),
    timeout: String(values.timeout),
  });
  const headers = values.apiKey === undefined
    ? undefined
    : { [API_SERVICE_VALUES_HEADER]: JSON.stringify({ api_key: values.apiKey }) };
  return request<ApiServicePayload>(
    `${base}/api/settings/api-service/start?${query}`,
    token,
    { headers },
  );
}

export async function stopApiService(token: string, base: string = ""): Promise<ApiServicePayload> {
  return request<ApiServicePayload>(`${base}/api/settings/api-service/stop`, token);
}

export async function enableNanoinfraFeature(
  token: string,
  name: string,
  options: { instanceId?: string } = {},
  base: string = "",
): Promise<NanoinfraFeaturesPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (options.instanceId) query.set("instance_id", options.instanceId);
  return request<NanoinfraFeaturesPayload>(
    `${base}/api/settings/nanoinfra-features/enable?${query}`,
    token,
  );
}

export async function disableNanoinfraFeature(
  token: string,
  name: string,
  options: { instanceId?: string } = {},
  base: string = "",
): Promise<NanoinfraFeaturesPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (options.instanceId) query.set("instance_id", options.instanceId);
  return request<NanoinfraFeaturesPayload>(
    `${base}/api/settings/nanoinfra-features/disable?${query}`,
    token,
  );
}

export async function fetchPairingRequests(
  token: string,
  base: string = "",
): Promise<PairingPayload> {
  return request<PairingPayload>(
    `${base}/api/settings/pairing`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runPairingAction(
  token: string,
  action: "approve" | "deny",
  code: string,
  base: string = "",
): Promise<PairingPayload> {
  const query = new URLSearchParams();
  query.set("code", code);
  return request<PairingPayload>(
    `${base}/api/settings/pairing/${action}?${query}`,
    token,
  );
}

export async function startChannelConnect(
  token: string,
  channel: string,
  options: {
    domain?: string;
    instanceId?: string;
    mode?: "replace" | "create";
    force?: boolean;
  } = {},
  base: string = "",
): Promise<ChannelConnectPayload> {
  const query = new URLSearchParams();
  if (options.domain) query.set("domain", options.domain);
  if (options.instanceId) query.set("instance_id", options.instanceId);
  if (options.mode) query.set("mode", options.mode);
  if (options.force) query.set("force", "true");
  const suffix = query.toString();
  return request<ChannelConnectPayload>(
    `${base}/api/settings/channels/${channel}/connect/start${suffix ? `?${suffix}` : ""}`,
    token,
  );
}

export async function pollChannelConnect(
  token: string,
  channel: string,
  sessionId: string,
  base: string = "",
): Promise<ChannelConnectPayload> {
  const query = new URLSearchParams();
  query.set("session_id", sessionId);
  return request<ChannelConnectPayload>(
    `${base}/api/settings/channels/${channel}/connect/poll?${query}`,
    token,
  );
}

export async function cancelChannelConnect(
  token: string,
  channel: string,
  sessionId: string,
  base: string = "",
): Promise<ChannelConnectPayload> {
  const query = new URLSearchParams();
  query.set("session_id", sessionId);
  return request<ChannelConnectPayload>(
    `${base}/api/settings/channels/${channel}/connect/cancel?${query}`,
    token,
  );
}

export async function configureChannel(
  token: string,
  name: string,
  values: Record<string, string>,
  options: { enable?: boolean; instanceId?: string } = {},
  base: string = "",
): Promise<ChannelConfigurePayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (options.enable !== undefined) query.set("enable", String(options.enable));
  if (options.instanceId) query.set("instance_id", options.instanceId);
  return request<ChannelConfigurePayload>(
    `${base}/api/settings/channels/configure?${query}`,
    token,
    {
      headers: {
        [CHANNEL_VALUES_HEADER]: JSON.stringify(values),
      },
    },
  );
}

export async function validateChannel(
  token: string,
  name: string,
  values: Record<string, string> = {},
  options: { instanceId?: string } = {},
  base: string = "",
): Promise<ChannelValidationPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (options.instanceId) query.set("instance_id", options.instanceId);
  return request<ChannelValidationPayload>(
    `${base}/api/settings/channels/validate?${query}`,
    token,
    {
      headers: {
        [CHANNEL_VALUES_HEADER]: JSON.stringify(values),
      },
    },
  );
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps/${action}?${query}`, token);
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchConnectors(
  token: string,
  base: string = "",
): Promise<ConnectorsPayload> {
  return request<ConnectorsPayload>(
    `${base}/api/settings/connectors`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function setConnectorAttach(
  token: string,
  name: string,
  attach: "always" | "mention",
  base: string = "",
): Promise<ConnectorsPayload> {
  const query = new URLSearchParams({ name, attach });
  return request<ConnectorsPayload>(
    `${base}/api/settings/connectors/attach?${query}`,
    token,
  );
}

export async function fetchConnectorObjects(
  token: string,
  base: string = "",
  { refresh = true }: { refresh?: boolean } = {},
): Promise<ConnectorObjectsPayload> {
  // `refresh=0` reads the recorded objects and calls nothing. The default costs one declared read
  // per connector through the executor -- a token mint and a live API call -- which is seconds, and
  // for that whole time the composer had no `@calendar:` prefix and no way to say why.
  const query = refresh ? "" : "?refresh=0";
  return request<ConnectorObjectsPayload>(
    `${base}/api/settings/connectors/objects${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function startConnectorConsent(
  token: string,
  name: string,
  values: { clientId: string; clientSecret: string; account?: string },
  base: string = "",
): Promise<ConnectorConsentStart> {
  const query = new URLSearchParams();
  query.set("name", name);
  // The client id and secret travel in a header, not the query string: a query string reaches
  // an access log and a browser history.
  return request<ConnectorConsentStart>(
    `${base}/api/settings/connectors/connect?${query}`,
    token,
    { headers: { "X-Nanoinfra-Connector-Values": JSON.stringify(values) } },
  );
}

export async function reloadConnectors(
  token: string,
  base: string = "",
): Promise<ConnectorsPayload> {
  return request<ConnectorsPayload>(`${base}/api/settings/connectors/reload`, token);
}

export async function runConnectorTest(
  token: string,
  name: string,
  base: string = "",
): Promise<ConnectorTestResult> {
  const query = new URLSearchParams();
  query.set("name", name);
  // No read timeout: the test performs a real call through the executor, and the executor owns
  // how long that may take.
  return request<ConnectorTestResult>(
    `${base}/api/settings/connectors/test?${query}`,
    token,
  );
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  base: string = "",
): Promise<ProviderModelsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<ProviderModelsPayload>(
    `${base}/api/settings/provider-models?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test" | "pause" | "resume" | "attach_always" | "attach_on_mention",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  // The action is the identifier the UI passes around; the route is its dashed form, the way
  // `import-cursor` already is. Sending `attach_always` verbatim reached no route at all.
  const path = action.replace(/_/g, "-");
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${path}?${query}`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    { headers: mcpValuesHeader({ config }) },
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    { headers: mcpValuesHeader({ name, enabled_tools: enabledTools }) },
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
    lifecycle?: unknown;
    accepts_args?: unknown;
  };
  const body = await request<{ commands: Row[] }>(
    `${base}/api/commands`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.commands
    .flatMap((command) => {
      if (!isSlashCommandLifecycle(command.lifecycle)) return [];
      return [{
        command: command.command,
        title: command.title,
        description: command.description,
        icon: command.icon,
        argHint: command.arg_hint ?? "",
        lifecycle: command.lifecycle,
        acceptsArgs: command.accepts_args === true,
      }];
    });
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  const query = new URLSearchParams();
  query.set("state", JSON.stringify(state));
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state/update?${query}`,
    token,
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.modelPreset !== undefined) {
    query.set("model_preset", update.modelPreset ?? "default");
  }
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  if (update.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(update.contextWindowTokens));
  }
  if (update.timezone !== undefined) query.set("timezone", update.timezone);
  if (update.maxConcurrentSubagents !== undefined) {
    query.set("max_concurrent_subagents", String(update.maxConcurrentSubagents));
  }
  if (update.toolHintMaxLength !== undefined) {
    query.set("tool_hint_max_length", String(update.toolHintMaxLength));
  }
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

function appendModelGenerationSettings(
  query: URLSearchParams,
  configuration: Pick<
    ModelConfigurationCreate,
    "maxTokens" | "contextWindowTokens" | "temperature" | "reasoningEffort"
  >,
): void {
  if (configuration.maxTokens !== undefined) {
    query.set("max_tokens", String(configuration.maxTokens));
  }
  if (configuration.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(configuration.contextWindowTokens));
  }
  if (configuration.temperature !== undefined) {
    query.set("temperature", String(configuration.temperature));
  }
  if (configuration.reasoningEffort !== undefined) {
    query.set("reasoning_effort", configuration.reasoningEffort ?? "");
  }
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (configuration.name !== undefined) query.set("name", configuration.name);
  query.set("label", configuration.label);
  query.set("provider", configuration.provider);
  query.set("model", configuration.model);
  appendModelGenerationSettings(query, configuration);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/create?${query}`,
    token,
  );
}

export async function updateModelConfiguration(
  token: string,
  configuration: ModelConfigurationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("name", configuration.name);
  if (configuration.label !== undefined) query.set("label", configuration.label);
  if (configuration.provider !== undefined) query.set("provider", configuration.provider);
  if (configuration.model !== undefined) query.set("model", configuration.model);
  appendModelGenerationSettings(query, configuration);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/update?${query}`,
    token,
  );
}

export async function deleteModelConfiguration(
  token: string,
  name: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams({ name });
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/delete?${query}`,
    token,
  );
}

export async function migrateModelConfigurations(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/migrate`,
    token,
  );
}

export async function updateModelCallOrder(
  token: string,
  order: string[],
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams({ order: JSON.stringify(order) });
  return request<SettingsPayload>(
    `${base}/api/settings/model-call-order/update?${query}`,
    token,
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const { provider, ...values } = update;
  const query = new URLSearchParams({ provider });
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
    {
      headers: {
        [PROVIDER_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)),
      },
    },
  );
}

export async function createProviderSettings(
  token: string,
  update: ProviderCreationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/provider/create`,
    token,
    {
      headers: {
        [PROVIDER_VALUES_HEADER]: encodeURIComponent(JSON.stringify(update)),
      },
    },
  );
}

export async function loginProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
  remoteBrowserAccess: boolean = false,
): Promise<ProviderOAuthLoginResult> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  if (remoteBrowserAccess) query.set("remote_browser", "true");
  return request<ProviderOAuthLoginResult>(
    `${base}/api/settings/provider/oauth-login?${query}`,
    token,
    { cache: "no-store" },
  );
}

export async function completeProviderOAuth(
  token: string,
  provider: string,
  flowId: string,
  authorizationResponse?: string,
  base: string = "",
): Promise<ProviderOAuthCompletionResult> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  query.set("flow_id", flowId);
  const responseHeader = provider === "openai_codex"
    ? OAUTH_CALLBACK_HEADER
    : OAUTH_CODE_HEADER;
  const headers = authorizationResponse
    ? { [responseHeader]: authorizationResponse }
    : undefined;
  return request<ProviderOAuthCompletionResult>(
    `${base}/api/settings/provider/oauth-login/complete?${query}`,
    token,
    { cache: "no-store", ...(headers ? { headers } : {}) },
  );
}

export async function logoutProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth-logout?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  if (update.maxResults !== undefined) query.set("max_results", String(update.maxResults));
  if (update.timeout !== undefined) query.set("timeout", String(update.timeout));
  if (update.useJinaReader !== undefined) {
    query.set("use_jina_reader", String(update.useJinaReader));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}

export async function updateNetworkSafetySettings(
  token: string,
  update: NetworkSafetySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("webui_allow_local_service_access", String(update.webuiAllowLocalServiceAccess));
  query.set("webui_default_access_mode", update.webuiDefaultAccessMode);
  return request<SettingsPayload>(
    `${base}/api/settings/network-safety/update?${query}`,
    token,
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("default_aspect_ratio", update.defaultAspectRatio);
  query.set("default_image_size", update.defaultImageSize);
  query.set("max_images_per_turn", String(update.maxImagesPerTurn));
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation/update?${query}`,
    token,
  );
}

export async function updateTranscriptionSettings(
  token: string,
  update: TranscriptionSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("language", update.language);
  query.set("max_duration_sec", String(update.maxDurationSec));
  query.set("max_upload_mb", String(update.maxUploadMb));
  return request<SettingsPayload>(
    `${base}/api/settings/transcription/update?${query}`,
    token,
  );
}

/**
 * Capability gate routes (nanoinfraorg/nanoinfra#26, #28, #29).
 *
 * Each gate call keeps an explicit timeout. An operator must read a refusal, so a gate request
 * must not wait for the gateway without a limit.
 */
export async function updateKnowledgeSettings(
  token: string,
  update: KnowledgeSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("mode", update.mode);
  query.set("reindex_interval_s", String(update.reindexIntervalS));
  // A JSON array, because a glob may hold a comma and a comma-joined list would split it.
  query.set("exclude", JSON.stringify(update.exclude));
  query.set("max_file_bytes", String(update.maxFileBytes));
  query.set("max_total_bytes", String(update.maxTotalBytes));
  query.set("max_results", String(update.maxResults));
  return request<SettingsPayload>(`${base}/api/settings/knowledge/update?${query}`, token);
}

export async function updateGatesPolicy(
  token: string,
  policy: GatesPolicy,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/gates/update`,
    token,
    {
      headers: {
        [GATES_VALUES_HEADER]: encodeURIComponent(JSON.stringify(policy)),
      },
    },
    DEFAULT_HTTP_TIMEOUT_MS,
  );
}

export async function fetchGatesLatches(
  token: string,
  base: string = "",
): Promise<GatesLatchPayload> {
  return request<GatesLatchPayload>(
    `${base}/api/webui/gates/latches`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function clearGatesLatch(
  token: string,
  values: GatesLatchClearValues,
  base: string = "",
): Promise<GatesLatchClearPayload> {
  // A GET, and the values travel in a header. The HTTP layer of the WebSocket channel serves GET
  // alone: a POST reaches no route, the server closes the connection with no response, and the
  // operator reads "the gateway did not answer". Every write in this client works the same way.
  return request<GatesLatchClearPayload>(
    `${base}/api/webui/gates/latches/clear`,
    token,
    {
      headers: {
        [LATCH_VALUES_HEADER]: JSON.stringify(values),
      },
    },
    DEFAULT_HTTP_TIMEOUT_MS,
  );
}

/**
 * The approvals inbox (nanoinfraorg/nanoinfra#27).
 *
 * The read carries the payload the executor rendered. The answer carries the digest of those
 * bytes, so an approval covers what the operator read.
 */
export async function fetchGatesApprovals(
  token: string,
  base: string = "",
): Promise<GatesApprovalsPayload> {
  return request<GatesApprovalsPayload>(
    `${base}/api/webui/gates/approvals`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function answerGatesApproval(
  token: string,
  values: GatesApprovalAnswerValues,
  base: string = "",
): Promise<GatesApprovalAnswer> {
  return request<GatesApprovalAnswer>(
    `${base}/api/webui/gates/approvals/answer`,
    token,
    {
      // A GET for the same reason the latch clear is one: this transport serves no POST.
      headers: {
        [APPROVAL_VALUES_HEADER]: JSON.stringify(values),
      },
    },
    DEFAULT_HTTP_TIMEOUT_MS,
  );
}

/** The audit route reads only. It takes every filter from the query string. */
export async function fetchGatesAudit(
  token: string,
  query: GatesAuditQuery = {},
  base: string = "",
): Promise<GatesAuditPage> {
  const params = new URLSearchParams();
  if (query.decision) params.set("decision", query.decision);
  if (query.capabilityClass) params.set("capabilityClass", query.capabilityClass);
  if (query.executionContext) params.set("executionContext", query.executionContext);
  if (query.originActor) params.set("originActor", query.originActor);
  if (query.since) params.set("since", query.since);
  const suffix = params.toString();
  return request<GatesAuditPage>(
    `${base}/api/webui/gates/audit${suffix ? `?${suffix}` : ""}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchDiagramCatalog(
  token: string,
  base: string = "",
): Promise<DiagramCatalogPayload> {
  return request<DiagramCatalogPayload>(
    `${base}/api/webui/diagrams/catalog`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export interface DiagramsPayload {
  diagrams: DiagramSummary[];
}

/**
 * One mentionable agent. Name and description only: what an agent may *reach* is the
 * authorization model, and the mention menu is not where a browser should be able to read it.
 * The Agents page shows counts of those bindings, from the settings payload.
 */
export interface NamedAgentSummary {
  name: string;
  description: string;
}

export interface NamedAgentsPayload {
  agents: NamedAgentSummary[];
}

export async function fetchNamedAgents(
  token: string,
  base: string = "",
): Promise<NamedAgentsPayload> {
  return request<NamedAgentsPayload>(
    `${base}/api/webui/agents/named`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/**
 * Write the whole roster: `agents.named`, replaced (#262).
 *
 * **The whole map, never a diff.** A roster is one object that has to stay internally consistent:
 * `delegates` names other agents, so a server asked to validate one agent against a roster this
 * client had not sent would accept a pair config refuses. Deleting an agent is therefore sending
 * the roster without it, and there is no delete route to get out of step with this one.
 *
 * **No `method`.** Every settings write in this file is a path with a header, because
 * `websockets`' `process_request` exposes the request line and the headers and never a body -- and
 * over that transport a `POST` reaches no route at all: the connection closes with no response.
 * The route reads the header, which is what decides a write from a read here.
 *
 * The reply is the whole fresh `SettingsPayload`, the way every other settings write answers, so a
 * caller hands it straight to `applyPayload` rather than refetching.
 */
export async function saveNamedAgents(
  token: string,
  request_: NamedAgentsSaveRequest,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/agents`,
    token,
    {
      headers: chunkedValuesHeaders(AGENTS_HEADER, AGENTS_CHUNK_COUNT_HEADER, request_),
    },
    API_READ_TIMEOUT_MS,
  );
}

/**
 * Write the deployment's own agent -- the fields of `agents.defaults` that are an agent's (#265,
 * completed in #266).
 *
 * **A partial write, by design.** `agents.defaults` holds twenty-six fields -- the timezone, the
 * tool-iteration cap, the subagent limit -- and this form shows the seven that are an *agent's*
 * rather than a deployment's: the addendum, the replaced prompt sections, and the tool groups,
 * skills, MCP servers, connectors and delegates it narrows itself to. The route writes the keys the payload carries and leaves the rest
 * alone, so a caller must send only what was edited: a full snapshot of the form's state would
 * reset nineteen settings to whatever this client last read. `agentDefaultsPatch` in
 * `components/agents/agentValues.ts` is what builds that diff.
 *
 * The transport is the roster write's: a GET, because `websockets`' `read_request` refuses a POST
 * before a route sees it, and the payload in chunked headers because prompt text has no size a
 * header line can be assumed to survive. Same header pair, so the two writes cannot drift.
 */
export async function saveAgentDefaults(
  token: string,
  request_: AgentDefaultsSaveRequest,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/agents/defaults`,
    token,
    {
      headers: chunkedValuesHeaders(AGENTS_HEADER, AGENTS_CHUNK_COUNT_HEADER, request_),
    },
    API_READ_TIMEOUT_MS,
  );
}

/**
 * The gateway's own words for a refusal, unparaphrased.
 *
 * The refusals that matter here come from the config schema -- an unknown delegate, an agent
 * listing itself, a name that could not be typed as `@agent:<name>` -- and they name the offending
 * value. A UI that replaced them with "could not save" would throw away the only part an operator
 * can act on. Text or `{"error": "..."}` are both unwrapped, so the reason survives the gateway
 * changing its mind about which it sends.
 */
export function serverReason(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const text = raw.trim();
  if (!text.startsWith("{")) return text;
  try {
    const parsed = JSON.parse(text) as { error?: unknown; message?: unknown };
    const reason = parsed.error ?? parsed.message;
    return typeof reason === "string" && reason.trim() ? reason.trim() : text;
  } catch {
    return text;
  }
}

/**
 * What a deployment may change about one prompt section (#256).
 *
 * `replaceable` is the persona and what it remembers; `workspace` is already yours by another
 * route (`AGENTS.md` and friends); `fixed` is the tool contract and the safety notes, which have
 * no override path at all; `derived` is computed from config, so config is where it changes; and
 * `append_only` is the addendum, which is added and displaces nothing.
 */
export type AgentPromptPermission =
  | "replaceable"
  | "workspace"
  | "fixed"
  | "derived"
  | "append_only";

export interface AgentPromptSection {
  name: string;
  permission: AgentPromptPermission;
  /** True when this deployment replaced the section's text instead of taking the platform's. */
  overridden: boolean;
  /** False for a section this agent does not have -- an addendum it never declared. */
  present: boolean;
  /** True when the size is a property of the deployment rather than of a turn. */
  static: boolean;
  /** Null for a per-turn section, whose size the turn's own manifest reports instead. */
  tokens: number | null;
  /**
   * The text in force: this deployment's replacement when it made one, otherwise the platform's
   * own. `null` for a section a turn assembles -- the memory block, the bootstrap files, the
   * history -- where there is no text outside a turn to show.
   *
   * Optional here, and on the three below, only so a gateway older than #262's prompt editor
   * still renders a section list. A panel that reads a name and a permission and cannot read the
   * text is a map of the prompt rather than the prompt, which is the thing this field ends.
   */
  text?: string | null;
  /** The platform's own text, so the editor can hand it back after a replacement. */
  platform_text?: string | null;
  /** The `{{ }}` names a replacement has to keep, in the order they appear in the template. */
  placeholders?: string[];
  /** What replacing this section costs, in the platform's words. Empty for most sections. */
  warning?: string;
}

export interface AgentPromptPayload {
  agent: string;
  description: string;
  sections: AgentPromptSection[];
  /** The addendum's own text. Prompt content, read-only: an agent is edited in config. */
  addendum: string;
  measured: boolean;
}

export async function fetchAgentPrompt(
  /**
   * The named agent, or `null` for the deployment's own -- `agents.defaults` (#265).
   *
   * `null` sends no `agent` parameter, which is how the route spells *the agent that answers when
   * nobody picks one*. A gateway that does not serve that yet answers 400 and the panel says it
   * does not report the composition, which is true and is the same thing it says for every other
   * gateway too old for a read: the alternative was a second code path that would need deleting
   * the day the route lands.
   */
  agent: string | null,
  token: string,
  base: string = "",
): Promise<AgentPromptPayload> {
  const query = agent === null ? "" : `?agent=${encodeURIComponent(agent)}`;
  return request<AgentPromptPayload>(
    `${base}/api/webui/agents/prompt${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/**
 * One prompt this workspace may replace (#264).
 *
 * `text` is the text in force -- this workspace's own when it has one, the platform's when it has
 * not. `platform_text` travels either way, because "restore the default" has to put back
 * something the panel can show first.
 */
export interface WorkspacePrompt {
  name: string;
  /** What the prompt decides, in the platform's own words. */
  controls: string;
  /** What a replacement must keep. Non-empty for `evaluator`; empty for `dream`. */
  requirement: string;
  text: string;
  platform_text: string;
  source: "workspace" | "platform";
  /** Where the override file lives, so an operator can edit it in an editor too. */
  path: string;
  max_chars: number;
}

export interface WorkspacePromptsPayload {
  prompts: WorkspacePrompt[];
}

export async function fetchWorkspacePrompts(
  token: string,
  base: string = "",
): Promise<WorkspacePromptsPayload> {
  return request<WorkspacePromptsPayload>(
    `${base}/api/settings/workspace-prompts`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/**
 * Write one override, or remove it -- a GET that writes, because this transport rejects any other
 * method before a route is reached, and a prompt is 8 KB so the body travels in chunked headers.
 *
 * Whether this stores a file or deletes one is the server's decision: text equal to the packaged
 * prompt, and empty text, both delete. The response is the read payload either way, so the panel
 * reads `source` back rather than predicting which happened.
 */
export async function saveWorkspacePrompt(
  token: string,
  values: { name: string; text: string },
  base: string = "",
): Promise<WorkspacePromptsPayload> {
  return request<WorkspacePromptsPayload>(
    `${base}/api/settings/workspace-prompts/save`,
    token,
    {
      headers: chunkedValuesHeaders(
        WORKSPACE_PROMPT_HEADER,
        WORKSPACE_PROMPT_CHUNK_COUNT_HEADER,
        values,
      ),
    },
  );
}

export interface DiagramDetailPayload {
  diagram: Diagram;
}

export async function fetchDiagrams(
  token: string,
  base: string = "",
): Promise<DiagramsPayload> {
  return request<DiagramsPayload>(
    `${base}/api/webui/diagrams`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchDiagram(
  token: string,
  id: string,
  base: string = "",
): Promise<DiagramDetailPayload> {
  return request<DiagramDetailPayload>(
    `${base}/api/webui/diagrams/${encodeURIComponent(id)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createDiagram(
  token: string,
  diagram: Diagram,
  base: string = "",
): Promise<DiagramDetailPayload> {
  return request<DiagramDetailPayload>(
    `${base}/api/webui/diagrams/create`,
    token,
    { headers: diagramValuesHeaders(diagram) },
  );
}

export async function updateDiagram(
  token: string,
  id: string,
  diagram: Diagram,
  base: string = "",
): Promise<DiagramDetailPayload> {
  return request<DiagramDetailPayload>(
    `${base}/api/webui/diagrams/${encodeURIComponent(id)}/update`,
    token,
    { headers: diagramValuesHeaders(diagram) },
  );
}

export async function deleteDiagramApi(
  token: string,
  id: string,
  base: string = "",
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `${base}/api/webui/diagrams/${encodeURIComponent(id)}/delete`,
    token,
  );
}

export interface SecretSummary {
  id: string;
  name: string;
  kind: string;
  providerId: string;
  createdAt: string;
  updatedAt: string;
}

export interface SecretsPayload {
  secrets: SecretSummary[];
}

export interface SecretDetailPayload {
  secret: SecretSummary;
}

export async function fetchSecrets(token: string, base: string = ""): Promise<SecretsPayload> {
  return request<SecretsPayload>(
    `${base}/api/webui/secrets`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createSecret(
  token: string,
  values: { name: string; kind: string; providerId: string; value: string },
  base: string = "",
): Promise<SecretDetailPayload> {
  return request<SecretDetailPayload>(
    `${base}/api/webui/secrets/create`,
    token,
    { headers: { [SECRET_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)) } },
  );
}

export async function updateSecret(
  token: string,
  id: string,
  values: { name: string; kind: string; providerId: string; value: string },
  base: string = "",
): Promise<SecretDetailPayload> {
  return request<SecretDetailPayload>(
    `${base}/api/webui/secrets/${encodeURIComponent(id)}/update`,
    token,
    { headers: { [SECRET_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)) } },
  );
}

export async function deleteSecretApi(
  token: string,
  id: string,
  base: string = "",
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `${base}/api/webui/secrets/${encodeURIComponent(id)}/delete`,
    token,
  );
}

export interface ServerSummary {
  id: string;
  name: string;
  providerId: string;
  tags: string[];
  updatedAt: string;
  /** When this box's NOTES.md was last written; null means it has no memory yet (#225). */
  notesUpdatedAt: string | null;
}

export interface ServersPayload {
  servers: ServerSummary[];
}

export async function fetchServers(token: string, base: string = ""): Promise<ServersPayload> {
  return request<ServersPayload>(`${base}/api/webui/servers`, token, undefined, API_READ_TIMEOUT_MS);
}

export interface ServerDetail extends ServerSummary {
  createdAt: string;
  config: Record<string, string>;
  secretRef: string | null;
}

export interface ServerDetailPayload {
  server: ServerDetail;
}

export interface ServerValues {
  name: string;
  providerId: string;
  config: Record<string, string>;
  secretRef: string | null;
  tags: string[];
}

export async function fetchServer(
  token: string,
  id: string,
  base: string = "",
): Promise<ServerDetailPayload> {
  return request<ServerDetailPayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createServer(
  token: string,
  values: ServerValues,
  base: string = "",
): Promise<ServerDetailPayload> {
  return request<ServerDetailPayload>(
    `${base}/api/webui/servers/create`,
    token,
    { headers: { [SERVER_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)) } },
  );
}

export async function updateServer(
  token: string,
  id: string,
  values: ServerValues,
  base: string = "",
): Promise<ServerDetailPayload> {
  return request<ServerDetailPayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/update`,
    token,
    { headers: { [SERVER_VALUES_HEADER]: encodeURIComponent(JSON.stringify(values)) } },
  );
}

export async function deleteServerApi(
  token: string,
  id: string,
  base: string = "",
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/delete`,
    token,
  );
}

export interface ServerNoteEntry {
  when: string;
  author: string;
  title: string;
  body: string;
  /** Written by a person. Outranks an agent's entry, and no agent may edit it (#228). */
  isOperator: boolean;
}

export interface ServerNotesPayload {
  serverId: string;
  name: string;
  notesUpdatedAt: string | null;
  text: string;
  entries: ServerNoteEntry[];
  hasArchive: boolean;
}

export interface ServerNotesArchivePayload {
  serverId: string;
  text: string;
  entries: ServerNoteEntry[];
}

export async function fetchServerNotes(
  token: string,
  id: string,
  base: string = "",
): Promise<ServerNotesPayload> {
  return request<ServerNotesPayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/notes`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchServerNotesArchive(
  token: string,
  id: string,
  base: string = "",
): Promise<ServerNotesArchivePayload> {
  return request<ServerNotesArchivePayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/notes/archive`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function appendServerNote(
  token: string,
  id: string,
  values: { title: string; body: string },
  base: string = "",
): Promise<ServerNotesPayload> {
  return request<ServerNotesPayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/notes/append`,
    token,
    { headers: serverNotesHeaders(values) },
  );
}

export async function saveServerNotes(
  token: string,
  id: string,
  text: string,
  base: string = "",
): Promise<ServerNotesPayload> {
  return request<ServerNotesPayload>(
    `${base}/api/webui/servers/${encodeURIComponent(id)}/notes/save`,
    token,
    { headers: serverNotesHeaders({ text }) },
  );
}
