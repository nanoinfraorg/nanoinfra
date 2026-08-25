import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, FolderPlus, Loader2, ShieldAlert } from "lucide-react";

import { ApiError, createWorkspaceProject, fetchWorkspaceProjects } from "@/lib/api";
import type { WorkspaceProjectsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/**
 * Which workspace the explorer is looking at.
 *
 * Independent of the chat's own project on purpose: looking at files should not
 * change which project a conversation answers about. The list comes from
 * `tools.workspacesRoot` plus the configured workspace, which is the same set the
 * server will accept — a name typed here cannot widen it.
 */
export function WorkspacePicker({
  workspace,
  onChange,
}: {
  /** Absolute path, or `null` for the configured workspace. */
  workspace: string | null;
  onChange: (workspace: string | null) => void;
}) {
  const { getToken } = useClient();
  const [open, setOpen] = useState(false);
  const [payload, setPayload] = useState<WorkspaceProjectsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const load = useCallback(async () => {
    try {
      setPayload(await fetchWorkspaceProjects(tokenRef.current()));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const current = payload?.workspaces.find((entry) =>
    workspace === null ? entry.path === payload.defaultWorkspace : entry.path === workspace,
  );
  const label = current?.name
    ?? (workspace ? workspace.split(/[\\/]/).filter(Boolean).pop() : null)
    ?? "Workspace";

  const submitNew = useCallback(async () => {
    const name = (creating ?? "").trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      const made = await createWorkspaceProject(tokenRef.current(), name);
      await load();
      setCreating(null);
      setOpen(false);
      onChange(made.workspace);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [creating, load, onChange]);

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((previous) => !previous)}
        aria-expanded={open}
        aria-label="Switch workspace"
        className="flex h-8 max-w-[220px] items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
      >
        <span className="truncate">{label}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Workspaces"
          className="absolute right-0 z-50 mt-1 w-[min(22rem,80vw)] overflow-hidden rounded-xl border border-border/70 bg-card/95 py-1 shadow-[0_18px_50px_rgba(15,23,42,0.22)] backdrop-blur-xl"
        >
          {payload ? (
            <div className="px-3 py-1.5 text-[11px] text-muted-foreground">
              {`Workspaces root: ${payload.root}`}
            </div>
          ) : null}

          <div className="max-h-64 overflow-y-auto">
            {(payload?.workspaces ?? []).map((entry) => {
              const selected = current?.path === entry.path;
              return (
                <button
                  key={entry.path}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setOpen(false);
                    onChange(entry.path === payload?.defaultWorkspace ? null : entry.path);
                  }}
                  className="flex w-full items-start gap-2 px-3 py-1.5 text-left hover:bg-muted/70"
                >
                  <span className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center text-muted-foreground">
                    {selected ? <Check className="h-3.5 w-3.5" /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-[13px] text-foreground">
                      <span className="truncate">{entry.name}</span>
                      {entry.isDefault ? (
                        <span className="shrink-0 rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
                          default
                        </span>
                      ) : null}
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground">{entry.path}</span>
                    {entry.outsideRoot ? (
                      <span className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
                        <ShieldAlert className="h-3 w-3" />
                        outside the root — allowed because config names it
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })}
            {payload && payload.workspaces.length === 0 ? (
              <div className="px-3 py-2 text-[12px] text-muted-foreground">
                No workspaces yet. Create one below.
              </div>
            ) : null}
          </div>

          <div className="mt-1 border-t border-border/60 pt-1">
            {creating === null ? (
              <button
                type="button"
                role="menuitem"
                onClick={() => setCreating("")}
                className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-[13px] text-foreground hover:bg-muted/70"
              >
                <FolderPlus className="h-3.5 w-3.5 text-muted-foreground" />
                New workspace
              </button>
            ) : (
              <div className="flex items-center gap-2 px-3 py-1.5">
                <input
                  autoFocus
                  value={creating}
                  aria-label="New workspace name"
                  onChange={(event) => setCreating(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void submitNew();
                    if (event.key === "Escape") setCreating(null);
                  }}
                  placeholder="project-name"
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-[13px] text-foreground outline-none focus:border-foreground/30"
                />
                <button
                  type="button"
                  onClick={() => void submitNew()}
                  disabled={busy || !creating.trim()}
                  aria-label="Create workspace"
                  className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                >
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                </button>
              </div>
            )}
          </div>

          {error ? (
            <div className={cn("px-3 py-1.5 text-[11px] text-destructive-text")}>{error}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
