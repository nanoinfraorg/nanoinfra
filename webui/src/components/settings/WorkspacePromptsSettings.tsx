/**
 * Settings -> Prompts -- nanoinfraorg/nanoinfra#264.
 *
 * Two prompts run with nobody watching, and neither is the prompt of the agent you talk to.
 * `dream` decides which file a learned fact goes to and how hard stale content is pruned;
 * `evaluator` decides whether a heartbeat result is worth interrupting somebody for. Until now
 * the only way to change either was a slash command an operator had to know about plus a text
 * file, and nothing ever showed the text you were about to replace -- which makes an override a
 * rewrite from memory.
 *
 * Three properties this panel exists to hold:
 *
 * 1. **The comparison is against the platform's text, never against emptiness.** Text equal to
 *    the platform's removes the file rather than storing a copy, because a stored copy still wins
 *    tomorrow: it would freeze this workspace's behaviour at today's version without saying so.
 *    The server enforces that; this panel says it out loud beside the box, so the operator who
 *    pasted the default back knows what the save did.
 * 2. **The requirement is shown where there is one.** For `evaluator` it is load-bearing: a
 *    replacement that stops telling the model to call `evaluate_notification` leaves the gate
 *    failing closed and silent, and silence is indistinguishable from "nothing was worth saying".
 * 3. **A failed read is a missing panel, not an empty prompt.** The route registration is still
 *    outstanding, and an empty editor over a failed read invites somebody to type a replacement
 *    for a prompt that was never read -- and then save it over a working one.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { CircleAlert, Info, Loader2, RotateCcw, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, serverReason } from "@/lib/api";
import { fetchWithTimeout } from "@/lib/http";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------------------------------------------- *
 * The transport, pending its insertion into `lib/api.ts`.
 *
 * `api.ts` belongs to another change in flight, so the read, the write and their payload shapes
 * live here and the exact insertions are reported with this one. Everything below this comment
 * and above the next divider is a copy of what `api.ts` already does for the agent roster: the
 * same chunked headers, the same bearer token, the same non-JSON guard. When the insertion lands
 * this block is deleted and the imports move to `@/lib/api`.
 * -------------------------------------------------------------------------------------------- */

/** Must equal `_WORKSPACE_PROMPT_HEADER` in `nanoinfra/webui/ws_http.py`. */
const WORKSPACE_PROMPT_HEADER = "X-Nanoinfra-Workspace-Prompt";
const WORKSPACE_PROMPT_CHUNK_COUNT_HEADER = "X-Nanoinfra-Workspace-Prompt-Chunks";
/** Must equal `_DIAGRAM_CHUNK_BYTES` in `ws_http.py`; `api.ts` holds the same constant. */
const WORKSPACE_PROMPT_CHUNK_BYTES = 6000;
const API_READ_TIMEOUT_MS = 20_000;

/**
 * One prompt this workspace may replace, as the gateway describes it.
 *
 * `text` is the text **in force**, which is this workspace's own when it has one and the
 * platform's when it has not. `platform_text` travels either way, because "restore the default"
 * has to put back something the panel can show first.
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

/** One JSON payload as numbered header chunks -- the mirror of `_chunked_headers` in `ws_http`. */
function workspacePromptHeaders(payload: unknown): Record<string, string> {
  const encoded = encodeURIComponent(JSON.stringify(payload));
  const chunks: string[] = [];
  for (let index = 0; index < encoded.length; index += WORKSPACE_PROMPT_CHUNK_BYTES) {
    chunks.push(encoded.slice(index, index + WORKSPACE_PROMPT_CHUNK_BYTES));
  }
  if (chunks.length === 0) chunks.push("");
  const headers: Record<string, string> = {
    [WORKSPACE_PROMPT_CHUNK_COUNT_HEADER]: String(chunks.length),
  };
  chunks.forEach((chunk, index) => {
    headers[`${WORKSPACE_PROMPT_HEADER}-${index}`] = chunk;
  });
  return headers;
}

async function promptRequest(
  url: string,
  token: string,
  headers: Record<string, string> = {},
): Promise<WorkspacePromptsPayload> {
  const res = await fetchWithTimeout(
    url,
    {
      credentials: "same-origin",
      headers: { ...headers, Authorization: `Bearer ${token}` },
    },
    API_READ_TIMEOUT_MS,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    throw new ApiError(res.status, "Gateway returned a non-JSON response.");
  }
  return (await res.json()) as WorkspacePromptsPayload;
}

export async function fetchWorkspacePrompts(
  token: string,
  base: string = "",
): Promise<WorkspacePromptsPayload> {
  return promptRequest(`${base}/api/settings/workspace-prompts`, token);
}

/**
 * Write one override, or remove it -- a GET that writes, because this transport rejects any other
 * method before a route is reached, and a prompt is 8 KB so the body travels in chunked headers.
 *
 * The decision of whether this stores a file or deletes one is the server's: text equal to the
 * packaged prompt, and empty text, both delete. The response is the read payload either way, so
 * the panel never has to predict which happened -- it reads `source` back.
 */
export async function saveWorkspacePrompt(
  token: string,
  values: { name: string; text: string },
  base: string = "",
): Promise<WorkspacePromptsPayload> {
  return promptRequest(
    `${base}/api/settings/workspace-prompts/save`,
    token,
    workspacePromptHeaders(values),
  );
}

/* -------------------------------------------------------------------------------------------- *
 * The panel.
 * -------------------------------------------------------------------------------------------- */

/** What the last write of one prompt did, in the words this panel will show for it. */
type PromptStatus =
  | { kind: "saved" }
  | { kind: "removed" }
  | { kind: "failed"; reason: string };

/** One key dropped from a record. An edit clears the outcome of the previous write. */
function without(
  statuses: Record<string, PromptStatus>,
  name: string,
): Record<string, PromptStatus> {
  return Object.fromEntries(Object.entries(statuses).filter(([key]) => key !== name));
}

export function WorkspacePromptsSettings({
  token,
  base = "",
}: {
  token: string;
  base?: string;
}) {
  const { t } = useTranslation();
  const tx = useCallback(
    (key: string, fallback: string, values: Record<string, string> = {}) =>
      t(key, { defaultValue: fallback, ...values }),
    [t],
  );
  const [payload, setPayload] = useState<WorkspacePromptsPayload | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<Record<string, PromptStatus>>({});

  const apply = useCallback((next: WorkspacePromptsPayload) => {
    setPayload(next);
    // The drafts follow the texts in force. A save that removed the file leaves the platform's
    // text in the box, which is what is running now.
    setDrafts(Object.fromEntries(next.prompts.map((prompt) => [prompt.name, prompt.text])));
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await fetchWorkspacePrompts(token, base);
        if (cancelled) return;
        apply(next);
        setUnavailable(null);
      } catch (err) {
        if (cancelled) return;
        // Whatever failed, this panel read no prompt, so it offers no editor. A 404 is the route
        // that is not registered yet; anything else is a gateway that could not answer.
        setPayload(null);
        setUnavailable(serverReason(err) || `HTTP ${(err as ApiError)?.status ?? 0}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apply, base, token]);

  const write = async (name: string, text: string) => {
    setBusy(name);
    setStatus((previous) => without(previous, name));
    try {
      const next = await saveWorkspacePrompt(token, { name, text }, base);
      apply(next);
      const saved = next.prompts.find((prompt) => prompt.name === name);
      setStatus((previous) => ({
        ...previous,
        [name]: { kind: saved?.source === "workspace" ? "saved" : "removed" },
      }));
    } catch (err) {
      setStatus((previous) => ({
        ...previous,
        [name]: { kind: "failed", reason: serverReason(err) },
      }));
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return (
      <div className="space-y-7" data-testid="workspace-prompts">
        <PromptsTitle>{tx("settings.prompts.title", "Prompts")}</PromptsTitle>
        <PromptsGroup>
          <PromptsNote testId="workspace-prompts-loading">
            <Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" aria-hidden />
            {tx("settings.prompts.loading", "Reading the prompts in force…")}
          </PromptsNote>
        </PromptsGroup>
      </div>
    );
  }

  if (unavailable !== null || !payload) {
    return (
      <div className="space-y-7" data-testid="workspace-prompts">
        <PromptsTitle>{tx("settings.prompts.title", "Prompts")}</PromptsTitle>
        <PromptsGroup>
          <PromptsNote testId="workspace-prompts-unavailable" tone="warning">
            {tx(
              "settings.prompts.unavailable",
              "This gateway did not serve the prompts panel, so nothing here was read. The two "
              + "prompts are still whatever they were: edit their files on disk, and no text "
              + "typed here would have been saved.",
            )}
            {unavailable ? (
              <span className="mt-1 block font-mono text-[11px] text-muted-foreground">
                {unavailable}
              </span>
            ) : null}
          </PromptsNote>
        </PromptsGroup>
      </div>
    );
  }

  return (
    <div className="space-y-7" data-testid="workspace-prompts">
      <section>
        <PromptsTitle>{tx("settings.prompts.title", "Prompts")}</PromptsTitle>
        <PromptsGroup>
          <PromptsNote testId="workspace-prompts-intro">
            {tx(
              "settings.prompts.subtitle",
              "These two prompts run with nobody watching, and neither is the prompt of the "
              + "agent you talk to.",
            )}
          </PromptsNote>
        </PromptsGroup>
      </section>
      {payload.prompts.map((prompt) => (
        <PromptCard
          busy={busy === prompt.name}
          draft={drafts[prompt.name] ?? prompt.text}
          key={prompt.name}
          onDraftChange={(text) => {
            setDrafts((previous) => ({ ...previous, [prompt.name]: text }));
            setStatus((previous) => without(previous, prompt.name));
          }}
          onRestore={() => {
            // The box first, then the file: the operator sees the default they asked for even if
            // the write is refused, and the status line beside it says whether it landed.
            setDrafts((previous) => ({ ...previous, [prompt.name]: prompt.platform_text }));
            void write(prompt.name, prompt.platform_text);
          }}
          onSave={(text) => void write(prompt.name, text)}
          prompt={prompt}
          status={status[prompt.name]}
        />
      ))}
    </div>
  );
}

/**
 * One prompt: what it decides, what is in force, and what a replacement must keep.
 *
 * The requirement sits above the box rather than below it, because it is a constraint on what
 * you are about to type and not a remark about what you typed.
 */
function PromptCard({
  busy,
  draft,
  onDraftChange,
  onRestore,
  onSave,
  prompt,
  status,
}: {
  busy: boolean;
  draft: string;
  onDraftChange: (text: string) => void;
  onRestore: () => void;
  onSave: (text: string) => void;
  prompt: WorkspacePrompt;
  status: PromptStatus | undefined;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values: Record<string, string> = {}) =>
    t(key, { defaultValue: fallback, ...values });
  const label = promptLabel(t, prompt.name);
  // Trimmed on both sides of every comparison, exactly as the server compares them: a trailing
  // newline is not a replacement, and it must not read as unsaved work either.
  const inForce = prompt.text.trim();
  const platform = prompt.platform_text.trim();
  const typed = draft.trim();
  const dirty = typed !== inForce;
  const matchesPlatform = typed === platform;
  const tooLong = typed.length > prompt.max_chars;
  const restorable = prompt.source === "workspace" || !matchesPlatform;
  const chars = useMemo(() => typed.length.toLocaleString(), [typed]);

  return (
    <section data-testid={`prompt-card-${prompt.name}`}>
      <PromptsTitle>{label}</PromptsTitle>
      <PromptsGroup>
        <div className="space-y-3 px-4 py-3.5 sm:px-5">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 text-[11px] font-medium",
                prompt.source === "workspace"
                  ? "border-border/60 text-foreground"
                  : "border-border/45 text-muted-foreground",
              )}
              data-source={prompt.source}
              data-testid={`prompt-source-${prompt.name}`}
            >
              {prompt.source === "workspace"
                ? tx("settings.prompts.source.workspace", "This workspace's own text")
                : tx("settings.prompts.source.platform", "The platform's text")}
            </span>
          </div>
          <p
            className="text-[12.5px] leading-5 text-muted-foreground"
            data-testid={`prompt-controls-${prompt.name}`}
          >
            <span className="font-medium text-foreground">
              {tx("settings.prompts.controlsLabel", "Decides")}
              {": "}
            </span>
            {prompt.controls}
          </p>
          {prompt.requirement.trim() ? (
            <p
              className="flex gap-2 rounded-[12px] bg-muted/60 px-3 py-2 text-[12px] leading-5 text-foreground/90"
              data-testid={`prompt-requirement-${prompt.name}`}
            >
              <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
              <span>
                <span className="font-medium">
                  {tx("settings.prompts.requirementLabel", "A replacement must keep this")}
                  {": "}
                </span>
                {prompt.requirement}
              </span>
            </p>
          ) : null}
          <Textarea
            aria-label={tx("settings.prompts.editorLabel", "{{name}} prompt text", {
              name: label,
            })}
            className="rounded-[12px] font-mono text-[11.5px] leading-5"
            data-testid={`prompt-editor-${prompt.name}`}
            onChange={(event) => onDraftChange(event.target.value)}
            rows={14}
            value={draft}
          />
          <p
            className={cn(
              "text-[11.5px] leading-4",
              tooLong ? "text-destructive-text" : "text-muted-foreground",
            )}
            data-testid={`prompt-chars-${prompt.name}`}
          >
            {tooLong
              ? tx(
                "settings.prompts.tooLong",
                "This text is longer than the {{max}} characters a workspace prompt may hold.",
                { max: prompt.max_chars.toLocaleString() },
              )
              : tx("settings.prompts.charCount", "{{chars}} of {{max}} characters", {
                chars,
                max: prompt.max_chars.toLocaleString(),
              })}
          </p>
          {matchesPlatform ? (
            <p
              className="text-[11.5px] leading-4 text-muted-foreground"
              data-testid={`prompt-matches-platform-${prompt.name}`}
            >
              {tx(
                "settings.prompts.matchesPlatform",
                "This is the platform's own text. Saving it removes the override file rather "
                + "than storing a copy: a copy would still win tomorrow, and this workspace "
                + "would stop receiving every later improvement without saying so.",
              )}
            </p>
          ) : null}
          <p
            className="text-[11.5px] leading-4 text-muted-foreground"
            data-testid={`prompt-path-${prompt.name}`}
          >
            {tx("settings.prompts.pathLabel", "File")}
            {": "}
            <span className="break-all font-mono text-[11px] text-foreground/85">
              {prompt.path}
            </span>
            {" "}
            {tx(
              "settings.prompts.pathHelp",
              "Saving writes that file and restoring deletes it. An editor writes the same one.",
            )}
          </p>
        </div>
        <div className="flex min-h-[58px] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div
            className={cn(
              "min-w-0 text-[13px] leading-5",
              status?.kind === "failed" ? "text-destructive-text" : "text-muted-foreground",
            )}
            data-testid={`prompt-status-${prompt.name}`}
          >
            {status?.kind === "failed"
              ? tx("settings.prompts.status.failed", "Not saved: {{reason}}", {
                reason: status.reason,
              })
              : status?.kind === "removed"
                ? tx(
                  "settings.prompts.status.removed",
                  "The override file is gone. The platform's prompt is in force again, and it "
                  + "will keep improving with the platform.",
                )
                : status?.kind === "saved"
                  ? tx(
                    "settings.prompts.status.saved",
                    "Saved. This workspace's own text is in force from the next run.",
                  )
                  : dirty
                    ? tx("settings.prompts.status.unsaved", "Unsaved changes.")
                    : undefined}
          </div>
          <div className="flex w-full shrink-0 flex-wrap justify-end gap-2 sm:w-auto">
            <Button
              className="h-8 rounded-full px-3 text-[12px] text-muted-foreground hover:text-foreground"
              data-testid={`prompt-restore-${prompt.name}`}
              disabled={busy || !restorable}
              onClick={onRestore}
              size="sm"
              variant="ghost"
            >
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {tx("settings.prompts.actions.restore", "Restore default")}
            </Button>
            <Button
              className="h-8 rounded-full px-3 text-[12px]"
              data-testid={`prompt-discard-${prompt.name}`}
              disabled={busy || !dirty}
              onClick={() => onDraftChange(prompt.text)}
              size="sm"
              variant="ghost"
            >
              {tx("settings.prompts.actions.discard", "Discard")}
            </Button>
            <Button
              className="h-8 rounded-full px-3 text-[12px]"
              data-testid={`prompt-save-${prompt.name}`}
              disabled={busy || !dirty || tooLong}
              onClick={() => onSave(draft)}
              size="sm"
              variant="outline"
            >
              {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
              {tx("settings.prompts.actions.save", "Save")}
            </Button>
          </div>
        </div>
      </PromptsGroup>
    </section>
  );
}

/**
 * The name an operator reads for one prompt.
 *
 * Decided here rather than server-side, for the reason the agent prompt panel gives for its own
 * labels: the server states the machine name, and a name a person reads has to read the same in
 * eight languages. A prompt this build has no label for falls back to its machine name, so a
 * third prompt added server-side still renders.
 */
function promptLabel(t: ReturnType<typeof useTranslation>["t"], name: string): string {
  if (name === "dream") {
    return t("settings.prompts.name.dream", { defaultValue: "Dream — memory consolidation" });
  }
  if (name === "evaluator") {
    return t("settings.prompts.name.evaluator", {
      defaultValue: "Evaluator — the heartbeat's notification gate",
    });
  }
  return name;
}

function PromptsTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
      {children}
    </h2>
  );
}

function PromptsGroup({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-[22px] bg-settings-surface">
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

function PromptsNote({
  children,
  testId,
  tone = "info",
}: {
  children: ReactNode;
  testId?: string;
  tone?: "info" | "warning";
}) {
  const Icon = tone === "warning" ? CircleAlert : Info;
  return (
    <div
      className={cn(
        "flex gap-2 px-4 py-3 text-[12px] leading-5 sm:px-5",
        tone === "warning" ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground",
      )}
      data-testid={testId}
      data-tone={tone}
    >
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}
