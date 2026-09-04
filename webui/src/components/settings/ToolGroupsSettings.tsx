/**
 * Settings -> Tool groups -- nanoinfraorg/nanoinfra#210.
 *
 * `tools.groups` decides which built-in schemas are in every prompt and which wait to be asked
 * for by name. It is the one knob that cuts tool schemas out of a prompt, and until this panel it
 * had no surface at all: declaring a group meant hand-editing `~/.nanoinfra/config.json`.
 *
 * Three things this panel has to do that a plain form would not:
 *
 * - **Show the built-ins nobody declared.** An operator cannot decide to put `servers` in mention
 *   mode without being told `servers` exists. A list of only the declared groups is empty on
 *   every fresh deployment, which is exactly when the decision is worth the most.
 * - **Say what a group with no members means.** `groups.py` inherits `BUILTIN_GROUPS`' members for
 *   a group that names none, so an empty member list is "the five diagram tools" and not "no
 *   tools". Rendered as the inherited names, because the difference is a capability.
 * - **Say what the mode costs.** `mention` is the token saving and `always` is the discoverability;
 *   a switch whose two positions are not explained is a switch nobody moves.
 *
 * Members are picked from the tools this deployment actually registered rather than typed, since a
 * misspelled tool name in config is a group that gates nothing and says nothing.
 *
 * One action is one write of the whole map, because that is the contract: `tools.groups` is
 * replaced, not merged. There is no draft to accumulate and therefore nothing that can be half
 * saved.
 */
import { Fragment, useMemo, useState, type ReactNode } from "react";
import { Boxes, CircleAlert, Info, Loader2, Pencil, Plus, RotateCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { fetchWithTimeout } from "@/lib/http";
import type { SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------------------------------- */
/* The contract. Written here rather than in `lib/types.ts` and `lib/api.ts` because those two    */
/* files are owned elsewhere this session; the manager's report carries the exact additions.      */
/* ------------------------------------------------------------------------------------------- */

export type ToolGroupAttach = "always" | "mention" | "search";

/**
 * One group's entry in `settings.tool_groups`: what config says about it, and what nanoinfra ships
 * under that name. The group's *name* is the map key, so it is not repeated here -- `types.ts`
 * already documents that slice as "keyed by group name" and the agent editor reads those keys as
 * its vocabulary (`agents/agentValues.ts:155`). One map therefore serves both panels.
 */
export interface ToolGroupInfo {
  /** The mode in force today. `always` for a built-in nobody declared. */
  attach: ToolGroupAttach;
  /** True when `tools.groups` names this group. False for a built-in offered to declare. */
  declared: boolean;
  /** True when the name is one nanoinfra defines (`diagrams`, `servers`). */
  builtin: boolean;
  /** Config's wording, verbatim. Empty when config named none. */
  description: string;
  /** nanoinfra's wording for this name. Empty when the name is not built-in. */
  builtin_description: string;
  /** Config's member list, verbatim. Empty means "inherit", which is the whole point. */
  tools: string[];
  /** nanoinfra's members for this name. Empty when the name is not built-in. */
  builtin_tools: string[];
  /** What actually gates today: config's list when it named one, else the built-in members. */
  effective_tools: string[];
  /** Effective members the live registry does not hold: a disabled tool, a missing plugin. */
  missing_tools: string[];
}

/** One group with its key folded in, which is what this panel renders and edits. */
export type ToolGroupRow = ToolGroupInfo & { name: string };

/** One tool this deployment registered, for the member picker. */
export interface ToolGroupToolRow {
  name: string;
  /** `builtin`, `mcp`, `connector`, `plugin` -- rendered as given, never matched on. */
  source: string;
  /** The declared groups already holding it. */
  groups: string[];
}

/**
 * `GET /api/settings` plus the tool list the member picker needs.
 *
 * `tool_groups` is not restated here: `SettingsPayload` already declares it as
 * `Record<string, unknown>` on purpose, and narrowing it in an intersection would not typecheck.
 * It is parsed at this boundary instead, by `toolGroupRows`, which is the honest place for a shape
 * this file is the only reader of.
 */
export type SettingsWithToolGroups = SettingsPayload & {
  registered_tools?: ToolGroupToolRow[] | null;
};

/** One entry of the map that replaces `tools.groups`. `tools` omitted means "inherit". */
export interface ToolGroupWrite {
  attach: ToolGroupAttach;
  tools?: string[];
  description?: string;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string")
    : [];
}

/**
 * The settings slice, read as rows.
 *
 * Field by field rather than one cast, because the payload is `Record<string, unknown>` by
 * design and a gateway that answers with less than the whole shape should degrade rather than
 * render `undefined`. The one inference worth naming: an entry that omits `declared` is treated
 * as declared, since a gateway that sends only what config wrote is still an editable map.
 */
export function toolGroupRows(raw: Record<string, unknown> | null | undefined): ToolGroupRow[] {
  if (!raw) return [];
  return Object.entries(raw)
    .map(([name, value]) => {
      const info = (typeof value === "object" && value !== null ? value : {}) as Record<
        string,
        unknown
      >;
      const tools = asStringArray(info.tools);
      const builtinTools = asStringArray(info.builtin_tools);
      const effective = asStringArray(info.effective_tools);
      return {
        name,
        attach:
          info.attach === "mention"
            ? "mention"
            : info.attach === "search"
              ? "search"
              : "always",
        declared: info.declared !== false,
        builtin: info.builtin === true || builtinTools.length > 0,
        description: asString(info.description),
        builtin_description: asString(info.builtin_description),
        tools,
        builtin_tools: builtinTools,
        effective_tools: effective.length ? effective : tools.length ? tools : builtinTools,
        missing_tools: asStringArray(info.missing_tools),
      } satisfies ToolGroupRow;
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}

/** The registry's tool list, read the same defensive way. */
export function registeredToolRows(raw: unknown): ToolGroupToolRow[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry) => {
      const row = (typeof entry === "object" && entry !== null ? entry : {}) as Record<
        string,
        unknown
      >;
      return {
        name: asString(row.name),
        source: asString(row.source),
        groups: asStringArray(row.groups),
      } satisfies ToolGroupToolRow;
    })
    .filter((row) => row.name)
    .sort((left, right) => left.name.localeCompare(right.name));
}

export const TOOL_GROUPS_HEADER = "X-Nanoinfra-Tool-Groups";
export const TOOL_GROUPS_CHUNK_COUNT_HEADER = "X-Nanoinfra-Tool-Groups-Chunks";

/**
 * Bytes per chunk, matching `ws_http._DIAGRAM_CHUNK_BYTES`.
 *
 * Not a product decision: `websockets` drops a request line or a header line over
 * `MAX_LINE_LENGTH` (8192 bytes) with no status and no error body -- the connection closes and the
 * browser reports a network error, which is the failure `lib/api.ts` documents for the diagram
 * editor. The gateway's reader is chunk-aware (`tool_groups_values_from_request`), so a map of any
 * size travels rather than being refused at some size nobody could predict.
 */
const TOOL_GROUPS_CHUNK_BYTES = 6000;

const TOOL_GROUPS_TIMEOUT_MS = 20_000;

/** The write's headers, mirroring `ws_http.tool_groups_values_headers`. */
export function toolGroupsHeaders(groups: Record<string, ToolGroupWrite>): Record<string, string> {
  const encoded = encodeURIComponent(JSON.stringify({ groups }));
  const chunks: string[] = [];
  for (let index = 0; index < encoded.length; index += TOOL_GROUPS_CHUNK_BYTES) {
    chunks.push(encoded.slice(index, index + TOOL_GROUPS_CHUNK_BYTES));
  }
  if (chunks.length === 0) chunks.push("");
  const headers: Record<string, string> = {
    [TOOL_GROUPS_CHUNK_COUNT_HEADER]: String(chunks.length),
  };
  chunks.forEach((chunk, index) => {
    headers[`${TOOL_GROUPS_HEADER}-${index}`] = chunk;
  });
  return headers;
}

/** The refusal, verbatim: plain text from `http_error()`, or `{"error": ...}` from a JSON route. */
async function refusalText(res: Response): Promise<string> {
  const raw = typeof res.text === "function" ? (await res.text()).trim() : "";
  if (!raw) return `HTTP ${res.status}`;
  try {
    const parsed = JSON.parse(raw) as { error?: unknown };
    if (typeof parsed.error === "string" && parsed.error.trim()) return parsed.error.trim();
  } catch {
    // A plain-text body, which is what `nanoinfra.webui.http_utils.http_error` sends.
  }
  return raw;
}

/**
 * Replace `tools.groups` with this map.
 *
 * No `method: "POST"`, and not an oversight: this gateway is `websockets.serve(process_request=)`,
 * whose request parser raises `unsupported HTTP method; expected GET` before a route is ever
 * consulted. Every write in this WebUI is therefore a GET carrying a header -- the mechanism the
 * automation editor established -- and the route dispatches on the path alone.
 */
export async function saveToolGroups(
  token: string,
  groups: Record<string, ToolGroupWrite>,
  base: string = "",
): Promise<SettingsWithToolGroups> {
  const res = await fetchWithTimeout(
    `${base}/api/settings/tool-groups`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        ...toolGroupsHeaders(groups),
      },
      credentials: "same-origin",
    },
    TOOL_GROUPS_TIMEOUT_MS,
  );
  if (!res.ok) throw new Error(await refusalText(res));
  return (await res.json()) as SettingsWithToolGroups;
}

/* ------------------------------------------------------------------------------------------- */

interface GroupDraft {
  name: string;
  attach: ToolGroupAttach;
  tools: string[];
  description: string;
}

interface EditorState {
  /** The name being replaced, or `null` for a group that does not exist yet. */
  original: string | null;
  draft: GroupDraft;
}

/** A group name has to survive being typed as `@name` in a Telegram message. */
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const NAME_MAX = 64;

export function writeFrom(draft: GroupDraft): ToolGroupWrite {
  const write: ToolGroupWrite = { attach: draft.attach };
  // An empty list is omitted rather than sent: `[]` and "absent" mean the same thing to config,
  // and the absent form is the one that reads as "inherit" in the file an operator opens next.
  if (draft.tools.length) write.tools = [...draft.tools];
  if (draft.description.trim()) write.description = draft.description.trim();
  return write;
}

export function nameProblem(
  name: string,
  taken: string[],
): "empty" | "shape" | "long" | "taken" | null {
  const value = name.trim();
  if (!value) return "empty";
  if (value.length > NAME_MAX) return "long";
  if (!NAME_PATTERN.test(value)) return "shape";
  if (taken.includes(value)) return "taken";
  return null;
}

export function ToolGroupsSettings({
  token,
  settings,
  onSaved,
}: {
  token: string;
  settings: SettingsWithToolGroups;
  onSaved: (payload: SettingsWithToolGroups) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const slice = settings.tool_groups ?? null;
  const rows = useMemo(() => toolGroupRows(slice), [slice]);
  const registered = useMemo(
    () => registeredToolRows(settings.registered_tools),
    [settings.registered_tools],
  );
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [restartNeeded, setRestartNeeded] = useState(false);

  const declared = useMemo(() => rows.filter((row) => row.declared), [rows]);
  const undeclared = useMemo(
    () => rows.filter((row) => !row.declared).sort((a, b) => a.name.localeCompare(b.name)),
    [rows],
  );
  const builtinRowByName = useMemo(() => {
    const map = new Map<string, ToolGroupRow>();
    rows.forEach((row) => {
      if (row.builtin) map.set(row.name, row);
    });
    return map;
  }, [rows]);

  /**
   * The picker's options. The registry's list when the gateway sends one; otherwise the names
   * already grouped, so the form still works rather than offering nothing to tick.
   */
  const pickable = useMemo<ToolGroupToolRow[]>(() => {
    if (registered.length) return registered;
    const seen = new Set<string>();
    rows.forEach((row) => {
      [...row.effective_tools, ...row.builtin_tools].forEach((name) => seen.add(name));
    });
    return [...seen].sort().map((name) => ({ name, source: "builtin", groups: [] }));
  }, [registered, rows]);
  const toolListIsGuessed = registered.length === 0;

  if (!slice) {
    return (
      <div className="space-y-7" data-testid="tool-groups-settings">
        <section>
          <GroupsTitle>{tx("settings.toolGroups.title", "Tool groups")}</GroupsTitle>
          <GroupsCard>
            <GroupsNote tone="warning" testId="tool-groups-unavailable">
              {tx(
                "settings.toolGroups.unavailable",
                "This gateway does not report tool groups yet. Update nanoinfra, or declare groups under tools.groups in config.json.",
              )}
            </GroupsNote>
          </GroupsCard>
        </section>
      </div>
    );
  }

  const mapFromDeclared = (): Record<string, ToolGroupWrite> => {
    const map: Record<string, ToolGroupWrite> = {};
    declared.forEach((row) => {
      map[row.name] = writeFrom({
        name: row.name,
        attach: row.attach,
        tools: row.tools,
        description: row.description,
      });
    });
    return map;
  };

  const commit = async (map: Record<string, ToolGroupWrite>, marker: string, note: string) => {
    setBusy(marker);
    setError(null);
    setSavedNote(null);
    try {
      const next = await saveToolGroups(token, map);
      onSaved(next);
      setSavedNote(note);
      setRestartNeeded(next.requires_restart !== false);
      setEditor(null);
      setPendingDelete(null);
    } catch (err) {
      // Verbatim. Config is the authority on what it refuses and this panel is a convenience.
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const changeAttach = (row: ToolGroupRow, attach: ToolGroupAttach) => {
    if (attach === row.attach) return;
    const map = mapFromDeclared();
    map[row.name] = writeFrom({
      name: row.name,
      attach,
      tools: row.tools,
      description: row.description,
    });
    const status =
      attach === "mention"
        ? tx(
          "settings.toolGroups.status.savedMention",
          "Saved. These schemas leave every prompt after a restart; say @group to attach them for one turn.",
        )
        : attach === "search"
          ? tx(
            "settings.toolGroups.status.savedSearch",
            "Saved. These schemas leave every prompt after a restart; the assistant loads them by calling tool_search when a turn needs them.",
          )
          : tx(
            "settings.toolGroups.status.savedAlways",
            "Saved. These schemas are in every prompt again after a restart.",
          );
    void commit(map, `attach:${row.name}`, status);
  };

  const startCreate = () =>
    setEditor({
      original: null,
      draft: { name: "", attach: "mention", tools: [], description: "" },
    });

  const startEdit = (row: ToolGroupRow) =>
    setEditor({
      original: row.name,
      draft: {
        name: row.name,
        attach: row.attach,
        tools: [...row.tools],
        description: row.description,
      },
    });

  const startDeclare = (row: ToolGroupRow) =>
    setEditor({
      original: row.name,
      // `mention` preselected, and no tools ticked: declaring a built-in and leaving it `always`
      // changes nothing, and its members are the ones nanoinfra defines. So the two answers an
      // operator came here for are already chosen.
      draft: { name: row.name, attach: "mention", tools: [], description: "" },
    });

  const saveEditor = (state: EditorState) => {
    const map = mapFromDeclared();
    if (state.original) delete map[state.original];
    map[state.draft.name.trim()] = writeFrom({
      ...state.draft,
      name: state.draft.name.trim(),
    });
    void commit(
      map,
      "editor",
      state.original
        ? tx("settings.toolGroups.status.savedEdit", "Saved. The gateway reads groups at start, so this takes effect after a restart.")
        : tx("settings.toolGroups.status.savedCreate", "Group declared. The gateway reads groups at start, so this takes effect after a restart."),
    );
  };

  const confirmDelete = (name: string) => {
    const map = mapFromDeclared();
    delete map[name];
    void commit(
      map,
      `delete:${name}`,
      tx(
        "settings.toolGroups.status.savedDelete",
        "Group removed. Its tools go back to being in every prompt after a restart.",
      ),
    );
  };

  const mentionCount = declared.filter((row) => row.attach === "mention").length;

  /**
   * The editor, in the list, under the row it belongs to.
   *
   * Not a dialog and not a tab strip. A dialog would cover the one thing the operator is deciding
   * against -- the other groups and their modes -- and a tab per group would turn a list that
   * grows into navigation. An expanded row keeps the comparison on screen, which is the whole
   * reason this panel lists the built-ins at all.
   */
  const editorFor = (name: string | null) =>
    editor && editor.original === name ? (
      <GroupEditor
        state={editor}
        rows={rows}
        builtinRowByName={builtinRowByName}
        pickable={pickable}
        toolListIsGuessed={toolListIsGuessed}
        saving={busy === "editor"}
        tx={tx}
        onChange={(draft) => setEditor({ ...editor, draft })}
        onCancel={() => setEditor(null)}
        onSave={() => saveEditor(editor)}
      />
    ) : null;

  return (
    <div className="space-y-7" data-testid="tool-groups-settings">
      <section>
        <GroupsTitle>{tx("settings.toolGroups.title", "Tool groups")}</GroupsTitle>
        <GroupsCard>
          <GroupsNote testId="tool-groups-intro">
            {tx(
              "settings.toolGroups.intro",
              "A group is a named set of built-in tools, and its mode decides whether their schemas are in every prompt. This is the only setting that takes tool schemas out of a prompt.",
            )}
          </GroupsNote>
          <div className="px-4 py-3.5 sm:px-5" data-testid="tool-groups-cost">
            <ModeCost
              mention={tx(
                "settings.toolGroups.cost.mention",
                "Only when mentioned: the schemas leave every prompt and cost nothing until somebody types @group in the message. One advertised line keeps the group visible, so the model can say it needs attaching instead of failing quietly. This is the mode that saves tokens.",
              )}
              search={tx(
                "settings.toolGroups.cost.search",
                "Only when searched: the schemas leave every prompt too, but the assistant loads them itself by searching when a request needs them — no @group to type. One shared pointer covers every searched group at once, so this is the mode that scales when you defer many groups.",
              )}
              always={tx(
                "settings.toolGroups.cost.always",
                "Always: every schema in every prompt, so the model can call the tools without being asked — and pays for them on turns that never touch them. This is the mode that keeps the tools discoverable.",
              )}
              measured={tx(
                "settings.toolGroups.cost.measured",
                "Measured on the demo: the diagram and SSH server clusters were 3,857 tokens of a 17,302-token greeting.",
              )}
            />
          </div>
        </GroupsCard>
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between gap-3 px-1">
          <GroupsTitle className="mb-0">
            {tx("settings.toolGroups.declaredTitle", "Declared groups")}
          </GroupsTitle>
          <Button
            size="sm"
            variant="outline"
            className="rounded-full"
            onClick={startCreate}
            disabled={Boolean(busy)}
            data-testid="tool-groups-create"
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {tx("settings.toolGroups.actions.create", "New group")}
          </Button>
        </div>
        <GroupsCard>
          {editorFor(null)}
          {declared.length ? (
            declared.map((row) => (
              <Fragment key={row.name}>
                <GroupRowView
                  row={row}
                  busy={busy}
                  tx={tx}
                  onAttach={(attach) => changeAttach(row, attach)}
                  onEdit={() => startEdit(row)}
                  onDelete={() => setPendingDelete(row.name)}
                />
                {editorFor(row.name)}
              </Fragment>
            ))
          ) : (
            <GroupsNote testId="tool-groups-none-declared">
              {tx(
                "settings.toolGroups.noneDeclared",
                "No group is declared, so every built-in schema is in every prompt. Declare one of the groups below to take its schemas out.",
              )}
            </GroupsNote>
          )}
          {declared.length ? (
            <GroupsNote testId="tool-groups-mention-count">
              {mentionCount
                ? tx(
                  "settings.toolGroups.mentionSummary",
                  "Groups waiting to be mentioned keep their schemas out of every prompt until a turn names them.",
                )
                : tx(
                  "settings.toolGroups.mentionSummaryNone",
                  "Every declared group is in Always mode, so nothing is saved yet.",
                )}
            </GroupsNote>
          ) : null}
        </GroupsCard>
      </section>

      <section>
        <GroupsTitle>
          {tx("settings.toolGroups.builtinTitle", "Groups nanoinfra defines")}
        </GroupsTitle>
        <GroupsCard>
          {undeclared.length ? (
            undeclared.map((row) => (
              <Fragment key={row.name}>
                <GroupRowView
                  row={row}
                  busy={busy}
                  tx={tx}
                  onDeclare={() => startDeclare(row)}
                />
                {editorFor(row.name)}
              </Fragment>
            ))
          ) : (
            <GroupsNote
              tone={rows.some((row) => row.builtin) ? "info" : "warning"}
              testId="tool-groups-all-declared"
            >
              {rows.some((row) => row.builtin)
                ? tx(
                  "settings.toolGroups.allDeclared",
                  "Every group nanoinfra defines is declared above.",
                )
                : tx(
                  "settings.toolGroups.builtinUnreported",
                  "This gateway did not report the groups nanoinfra defines, so none can be offered here.",
                )}
            </GroupsNote>
          )}
          <GroupsNote testId="tool-groups-builtin-help">
            {tx(
              "settings.toolGroups.builtinHelp",
              "These ship defined but in Always mode, so an upgrade never withdraws a tool by regrouping it. Declaring one carries the mode, not the tool names.",
            )}
          </GroupsNote>
        </GroupsCard>
      </section>

      <GroupsCard>
        <div
          className={cn(
            "flex min-h-[58px] flex-col gap-1 px-4 py-3 text-[13px] leading-5 sm:px-5",
            error ? "text-destructive-text" : "text-muted-foreground",
          )}
          data-testid="tool-groups-status"
        >
          {error ?? savedNote ?? tx(
            "settings.toolGroups.status.idle",
            "Every change here replaces the whole tools.groups map in config.json.",
          )}
        </div>
        {restartNeeded && !error ? (
          <GroupsNote tone="warning" testId="tool-groups-restart">
            <span className="inline-flex items-center gap-1.5">
              <RotateCw className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {tx(
                "settings.toolGroups.restart",
                "Restart the gateway to apply this. Groups are read once when the agent is built, so a running gateway keeps the modes it started with.",
              )}
            </span>
          </GroupsNote>
        ) : null}
      </GroupsCard>

      <AlertDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => (!open ? setPendingDelete(null) : undefined)}
      >
        <AlertDialogContent className="rounded-[20px]">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {tx("settings.toolGroups.delete.title", "Stop declaring this group?")}
            </AlertDialogTitle>
            <AlertDialogDescription data-testid="tool-groups-delete-description">
              {pendingDelete && builtinRowByName.has(pendingDelete)
                ? tx(
                  "settings.toolGroups.delete.builtinBody",
                  "The group goes back to how nanoinfra ships it: every one of its schemas in every prompt, on every turn.",
                )
                : tx(
                  "settings.toolGroups.delete.customBody",
                  "The group stops existing. Its tools stay installed and their schemas go back into every prompt.",
                )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setPendingDelete(null)}>
              {tx("settings.toolGroups.delete.cancel", "Keep it")}
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="tool-groups-delete-confirm"
              onClick={() => {
                if (pendingDelete) confirmDelete(pendingDelete);
              }}
            >
              {tx("settings.toolGroups.delete.confirm", "Remove group")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function GroupRowView({
  row,
  busy,
  tx,
  onAttach,
  onEdit,
  onDelete,
  onDeclare,
}: {
  row: ToolGroupRow;
  busy: string | null;
  tx: (key: string, fallback: string) => string;
  onAttach?: (attach: ToolGroupAttach) => void;
  onEdit?: () => void;
  onDelete?: () => void;
  onDeclare?: () => void;
}) {
  const inherits = row.declared && !row.tools.length && row.builtin_tools.length > 0;
  const description = row.description || row.builtin_description;
  return (
    <div
      className="flex flex-col gap-3 px-4 py-3.5 sm:px-5"
      data-testid={`tool-group-row-${row.name}`}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Boxes className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <code className="rounded-full bg-muted px-2.5 py-0.5 text-[12.5px] font-medium text-foreground">
              @{row.name}
            </code>
            <Badge tone={row.attach === "always" ? "neutral" : "saving"}>
              {row.attach === "mention"
                ? tx("settings.toolGroups.badge.mention", "Not in the prompt · saves tokens")
                : row.attach === "search"
                  ? tx("settings.toolGroups.badge.search", "Not in the prompt · searched")
                  : tx("settings.toolGroups.badge.always", "In every prompt")}
            </Badge>
            {row.builtin ? (
              <Badge tone="neutral">
                {tx("settings.toolGroups.badge.builtin", "Defined by nanoinfra")}
              </Badge>
            ) : (
              <Badge tone="neutral">{tx("settings.toolGroups.badge.custom", "Yours")}</Badge>
            )}
          </div>
          {description ? (
            <div className="mt-1 max-w-[32rem] text-[12px] leading-5 text-muted-foreground">
              {description}
            </div>
          ) : null}
          <div
            className="mt-1.5 flex flex-wrap gap-1.5"
            data-testid={`tool-group-members-${row.name}`}
          >
            {row.effective_tools.length ? (
              row.effective_tools.map((name) => (
                <code
                  key={name}
                  className={cn(
                    "rounded-full px-2 py-0.5 text-[11.5px]",
                    row.missing_tools.includes(name)
                      ? "bg-amber-500/10 text-amber-700 line-through dark:text-amber-300"
                      : "bg-muted/70 text-muted-foreground",
                  )}
                >
                  {name}
                </code>
              ))
            ) : (
              <span className="text-[12px] text-muted-foreground">
                {tx("settings.toolGroups.noMembers", "No tools, so this group gates nothing.")}
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {onAttach ? (
            <div data-testid={`tool-group-attach-${row.name}`}>
              <SegmentedControl
                value={row.attach}
                ariaLabel={`${tx("settings.toolGroups.attachLabel", "Attach mode")}: ${row.name}`}
                disabled={busy !== null}
                options={[
                  {
                    value: "always",
                    label: tx("settings.toolGroups.attach.always", "Always"),
                  },
                  {
                    value: "mention",
                    label: tx("settings.toolGroups.attach.mention", "Only when mentioned"),
                  },
                  {
                    value: "search",
                    label: tx("settings.toolGroups.attach.search", "Only when searched"),
                  },
                ]}
                onChange={(value) => onAttach(value as ToolGroupAttach)}
              />
            </div>
          ) : null}
          {onEdit ? (
            <Button
              size="sm"
              variant="ghost"
              className="rounded-full"
              disabled={busy !== null}
              onClick={onEdit}
              data-testid={`tool-group-edit-${row.name}`}
            >
              <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {tx("settings.toolGroups.actions.edit", "Edit")}
            </Button>
          ) : null}
          {onDelete ? (
            <Button
              size="sm"
              variant="ghost"
              className="rounded-full text-destructive-text"
              disabled={busy !== null}
              onClick={onDelete}
              data-testid={`tool-group-delete-${row.name}`}
            >
              <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {tx("settings.toolGroups.actions.delete", "Delete")}
            </Button>
          ) : null}
          {onDeclare ? (
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              disabled={busy !== null}
              onClick={onDeclare}
              data-testid={`tool-group-declare-${row.name}`}
            >
              {tx("settings.toolGroups.actions.declare", "Declare")}
            </Button>
          ) : null}
        </div>
      </div>
      {inherits ? (
        <InlineNote testId={`tool-group-inherits-${row.name}`}>
          {tx(
            "settings.toolGroups.inheritsRow",
            "Names no tools in config, so it inherits the members nanoinfra defines:",
          )}{" "}
          {row.builtin_tools.join(", ")}
        </InlineNote>
      ) : null}
      {row.missing_tools.length ? (
        <InlineNote tone="warning" testId={`tool-group-missing-${row.name}`}>
          {tx(
            "settings.toolGroups.missing",
            "Not registered in this deployment, so the group cannot load them:",
          )}{" "}
          {row.missing_tools.join(", ")}
        </InlineNote>
      ) : null}
    </div>
  );
}

function GroupEditor({
  state,
  rows,
  builtinRowByName,
  pickable,
  toolListIsGuessed,
  saving,
  tx,
  onChange,
  onCancel,
  onSave,
}: {
  state: EditorState;
  rows: ToolGroupRow[];
  builtinRowByName: Map<string, ToolGroupRow>;
  pickable: ToolGroupToolRow[];
  toolListIsGuessed: boolean;
  saving: boolean;
  tx: (key: string, fallback: string) => string;
  onChange: (draft: GroupDraft) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const [filter, setFilter] = useState("");
  const draft = state.draft;
  const typedName = draft.name.trim();
  const builtinRow = builtinRowByName.get(typedName);
  const nameIsBuiltin = Boolean(builtinRow);
  const nameLocked = Boolean(state.original && builtinRowByName.has(state.original));
  const taken = rows
    .filter((row) => row.declared && row.name !== state.original)
    .map((row) => row.name);
  const problem = nameProblem(draft.name, taken);
  // A group that names no tools and is not one nanoinfra defines has no members at all, and
  // `set_tool_groups` drops it rather than advertising a group that can never load a tool. Said
  // here, before the request, because a silently dropped group looks like a failed save.
  const membersMissing = !draft.tools.length && !nameIsBuiltin;
  const inherited = builtinRow?.builtin_tools ?? [];
  const visible = filter.trim()
    ? pickable.filter((tool) => tool.name.includes(filter.trim().toLowerCase()))
    : pickable;

  const toggle = (name: string) => {
    const next = draft.tools.includes(name)
      ? draft.tools.filter((entry) => entry !== name)
      : [...draft.tools, name].sort();
    onChange({ ...draft, tools: next });
  };

  return (
    // An inset block inside the list, not a card of its own and not a dialog: the row above it
    // stays visible, and so do the other groups it is being compared against.
    <div className="bg-muted/25" data-testid="tool-group-editor">
      <div className="px-4 pt-3.5 sm:px-5">
        <h3 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
          {state.original
            ? tx("settings.toolGroups.editor.editTitle", "Edit group")
            : tx("settings.toolGroups.editor.createTitle", "New group")}
        </h3>
      </div>
      <div className="divide-y divide-border/40">
        <EditorRow
          label={tx("settings.toolGroups.editor.name", "Name")}
          help={tx(
            "settings.toolGroups.editor.nameHelp",
            "Lower case, letters, digits, dash and underscore. This is what somebody types as @name in a message, from any channel.",
          )}
        >
          <Input
            value={draft.name}
            disabled={nameLocked}
            aria-label={tx("settings.toolGroups.editor.name", "Name")}
            data-testid="tool-group-editor-name"
            onChange={(event) => onChange({ ...draft, name: event.target.value })}
            className="h-8 w-48 max-w-full rounded-full text-[13px]"
          />
        </EditorRow>
        {problem ? (
          <InlineNote tone="warning" testId="tool-group-editor-name-problem">
            {problem === "empty"
              ? tx("settings.toolGroups.editor.nameEmpty", "A group needs a name.")
              : problem === "long"
                ? tx("settings.toolGroups.editor.nameLong", "A group name stops at 64 characters.")
                : problem === "taken"
                  ? tx(
                    "settings.toolGroups.editor.nameTaken",
                    "A group with this name is already declared.",
                  )
                  : tx(
                    "settings.toolGroups.editor.nameShape",
                    "Use lower case letters, digits, dash and underscore only, starting with a letter or digit — a name that cannot be typed as @name can never be attached.",
                  )}
          </InlineNote>
        ) : null}

        <EditorRow
          label={tx("settings.toolGroups.editor.attach", "When its schemas reach the prompt")}
        >
          <SegmentedControl
            value={draft.attach}
            ariaLabel={tx("settings.toolGroups.editor.attach", "When its schemas reach the prompt")}
            options={[
              { value: "always", label: tx("settings.toolGroups.attach.always", "Always") },
              {
                value: "mention",
                label: tx("settings.toolGroups.attach.mention", "Only when mentioned"),
              },
              {
                value: "search",
                label: tx("settings.toolGroups.attach.search", "Only when searched"),
              },
            ]}
            onChange={(value) => onChange({ ...draft, attach: value as ToolGroupAttach })}
          />
        </EditorRow>
        <div className="px-4 py-3 sm:px-5" data-testid="tool-group-editor-cost">
          <ModeCost
            mention={tx(
              "settings.toolGroups.cost.mention",
              "Only when mentioned: the schemas leave every prompt and cost nothing until somebody types @group in the message. One advertised line keeps the group visible, so the model can say it needs attaching instead of failing quietly. This is the mode that saves tokens.",
            )}
            always={tx(
              "settings.toolGroups.cost.always",
              "Always: every schema in every prompt, so the model can call the tools without being asked — and pays for them on turns that never touch them. This is the mode that keeps the tools discoverable.",
            )}
            highlight={draft.attach}
          />
        </div>

        <EditorRow
          label={tx("settings.toolGroups.editor.members", "Tools in this group")}
          help={tx(
            "settings.toolGroups.editor.membersHelp",
            "Picked from the tools this deployment registered. A name that is not registered gates nothing.",
          )}
        >
          <span className="text-[12px] text-muted-foreground" data-testid="tool-group-editor-count">
            {draft.tools.length}
          </span>
        </EditorRow>
        {draft.tools.length === 0 && inherited.length ? (
          <InlineNote testId="tool-group-editor-inherits">
            {tx(
              "settings.toolGroups.editor.inherits",
              "Nothing ticked, so this group inherits the members nanoinfra defines for this name:",
            )}{" "}
            {inherited.join(", ")}
            {". "}
            {tx(
              "settings.toolGroups.editor.inheritsWhy",
              "Leave it that way and a release that regroups a tool keeps the group correct.",
            )}
          </InlineNote>
        ) : null}
        {membersMissing ? (
          <InlineNote tone="warning" testId="tool-group-editor-members-problem">
            {tx(
              "settings.toolGroups.editor.membersEmpty",
              "A group nanoinfra does not define needs at least one tool. A group with no members is dropped rather than advertised.",
            )}
          </InlineNote>
        ) : null}
        {toolListIsGuessed ? (
          <InlineNote tone="warning" testId="tool-group-editor-tools-guessed">
            {tx(
              "settings.toolGroups.editor.toolsGuessed",
              "This gateway did not send the tool list, so only tools already grouped can be picked here.",
            )}
          </InlineNote>
        ) : null}
        <div className="px-4 py-3 sm:px-5">
          <Input
            value={filter}
            placeholder={tx("settings.toolGroups.editor.filter", "Filter tools")}
            aria-label={tx("settings.toolGroups.editor.filter", "Filter tools")}
            data-testid="tool-group-editor-filter"
            onChange={(event) => setFilter(event.target.value)}
            className="mb-2 h-8 w-full rounded-full text-[13px]"
          />
          <div
            className="max-h-64 overflow-y-auto rounded-[14px] bg-muted/40 p-2"
            data-testid="tool-group-editor-tools"
          >
            {visible.length ? (
              visible.map((tool) => (
                <label
                  key={tool.name}
                  className="flex cursor-pointer items-center gap-2 rounded-[10px] px-2 py-1.5 text-[12.5px] hover:bg-background/70"
                >
                  <input
                    type="checkbox"
                    checked={draft.tools.includes(tool.name)}
                    onChange={() => toggle(tool.name)}
                    aria-label={tool.name}
                  />
                  <code className="text-foreground">{tool.name}</code>
                  <span className="text-[11px] text-muted-foreground">{tool.source}</span>
                  {tool.groups.filter((name) => name !== typedName).length ? (
                    <span className="text-[11px] text-muted-foreground">
                      {tx("settings.toolGroups.editor.alreadyIn", "also in")}{" "}
                      {tool.groups.filter((name) => name !== typedName).join(", ")}
                    </span>
                  ) : null}
                </label>
              ))
            ) : (
              <div className="px-2 py-1.5 text-[12.5px] text-muted-foreground">
                {tx("settings.toolGroups.editor.noTools", "No tool matches that filter.")}
              </div>
            )}
          </div>
        </div>

        <EditorRow
          label={tx("settings.toolGroups.editor.description", "What it is for")}
          help={tx(
            "settings.toolGroups.editor.descriptionHelp",
            "Shown to the model in the advertised line, so it can name the group a request needs.",
          )}
        >
          <Input
            value={draft.description}
            placeholder={builtinRow?.builtin_description ?? ""}
            aria-label={tx("settings.toolGroups.editor.description", "What it is for")}
            data-testid="tool-group-editor-description"
            onChange={(event) => onChange({ ...draft, description: event.target.value })}
            className="h-8 w-full max-w-[22rem] rounded-full text-[13px]"
          />
        </EditorRow>

        <div className="flex flex-wrap justify-end gap-2 px-4 py-3 sm:px-5">
          <Button
            size="sm"
            variant="ghost"
            className="rounded-full"
            disabled={saving}
            onClick={onCancel}
            data-testid="tool-group-editor-cancel"
          >
            {tx("settings.toolGroups.editor.cancel", "Cancel")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={saving || problem !== null || membersMissing}
            onClick={onSave}
            data-testid="tool-group-editor-save"
          >
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {tx("settings.toolGroups.editor.save", "Save group")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function ModeCost({
  mention,
  always,
  search,
  measured,
  highlight,
}: {
  mention: string;
  always: string;
  search?: string;
  measured?: string;
  highlight?: ToolGroupAttach;
}) {
  return (
    <div className="space-y-2 text-[12px] leading-5">
      <div
        className={cn(
          "rounded-[12px] px-3 py-2",
          highlight === "mention" ? "bg-emerald-500/10 text-foreground" : "text-muted-foreground",
        )}
      >
        {mention}
      </div>
      {search ? (
        <div
          className={cn(
            "rounded-[12px] px-3 py-2",
            highlight === "search"
              ? "bg-emerald-500/10 text-foreground"
              : "text-muted-foreground",
          )}
        >
          {search}
        </div>
      ) : null}
      <div
        className={cn(
          "rounded-[12px] px-3 py-2",
          highlight === "always" ? "bg-muted text-foreground" : "text-muted-foreground",
        )}
      >
        {always}
      </div>
      {measured ? <div className="px-3 text-[11.5px] text-muted-foreground">{measured}</div> : null}
    </div>
  );
}

function Badge({ children, tone }: { children: ReactNode; tone: "neutral" | "saving" }) {
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone === "saving"
          ? "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300"
          : "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function GroupsTitle({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <h2
      className={cn(
        "mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85",
        className,
      )}
    >
      {children}
    </h2>
  );
}

function GroupsCard({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-[22px] bg-settings-surface">
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

function EditorRow({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="text-[14px] font-medium leading-5 text-foreground">{label}</div>
        {help ? (
          <div className="mt-0.5 max-w-[30rem] text-[12px] leading-5 text-muted-foreground">
            {help}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}

function GroupsNote({
  children,
  tone = "info",
  testId,
}: {
  children: ReactNode;
  tone?: "info" | "warning";
  testId?: string;
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

function InlineNote({
  children,
  tone = "info",
  testId,
}: {
  children: ReactNode;
  tone?: "info" | "warning";
  testId?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-[12px] px-3 py-2 text-[12px] leading-5",
        tone === "warning"
          ? "bg-amber-500/10 text-amber-700 dark:text-amber-300"
          : "bg-muted/50 text-muted-foreground",
      )}
      data-testid={testId}
      data-tone={tone}
    >
      {children}
    </div>
  );
}
