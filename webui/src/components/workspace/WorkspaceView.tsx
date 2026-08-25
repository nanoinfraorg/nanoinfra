import { useCallback, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronRight,
  CornerLeftUp,
  FileText,
  Folder,
  FolderPlus,
  Link2,
  Download,
  Pencil,
  RefreshCw,
  ShieldAlert,
  Upload,
  Trash2,
  X,
} from "lucide-react";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { WorkspaceDeleteConfirm } from "@/components/workspace/WorkspaceDeleteConfirm";
import { useWorkspaceBrowser } from "@/hooks/useWorkspaceBrowser";
import {
  ApiError,
  createWorkspaceFolder,
  deleteWorkspaceEntry,
  downloadWorkspaceFile,
  fetchWorkspaceFilePreview,
  renameWorkspaceEntry,
} from "@/lib/api";
import type { WorkspaceEntry, WorkspaceListingPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

interface WorkspaceViewProps {
  /** Absolute directory path from the URL, or `null` for the workspace root. */
  path?: string | null;
  onPathChange?: (path: string | null, options?: { replace?: boolean }) => void;
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function formatModified(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function EntryIcon({ entry }: { entry: WorkspaceEntry }) {
  if (entry.escapesWorkspace) return <ShieldAlert className="h-4 w-4 text-muted-foreground" />;
  if (entry.kind === "symlink") return <Link2 className="h-4 w-4 text-muted-foreground" />;
  if (entry.kind === "directory") return <Folder className="h-4 w-4 text-muted-foreground" />;
  return <FileText className="h-4 w-4 text-muted-foreground" />;
}

/**
 * The Workspaces explorer: one directory of the active workspace at a time.
 *
 * Every path here came from the server's own listing (`path`, `parent`, or a
 * prefix of `path`), and the server contains it again on the next request --
 * `nanoinfra/webui/file_browser.py` resolves symlinks before checking, and does
 * so unconditionally rather than reading `restrict_to_workspace`, which governs
 * the agent's tools and not what a browser may enumerate.
 */
export function WorkspaceView({ path = null, onPathChange }: WorkspaceViewProps) {
  const { client, token } = useClient();
  const { listing, loading, error, refresh, replace } = useWorkspaceBrowser(path);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [newFolderName, setNewFolderName] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ name: string; value: string } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ name: string; recursive: boolean } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const separator = useMemo(
    () => (listing && listing.projectPath.includes("\\") ? "\\" : "/"),
    [listing],
  );

  const crumbs = useMemo(() => {
    if (!listing) return [];
    const relative = listing.displayPath ? listing.displayPath.split("/").filter(Boolean) : [];
    // Absolute paths built by slicing the path the server just resolved, not by
    // joining operator input -- and re-checked server-side on the way back in.
    return relative.map((name, index) => ({
      name,
      path: [listing.projectPath, ...relative.slice(0, index + 1)].join(separator),
    }));
  }, [listing, separator]);

  const navigate = useCallback(
    (target: string | null) => {
      setSelectedFile(null);
      onPathChange?.(target);
    },
    [onPathChange],
  );

  const openEntry = useCallback(
    (entry: WorkspaceEntry) => {
      if (!listing || entry.escapesWorkspace) return;
      const absolute = [listing.path, entry.name].join(separator);
      if (entry.kind === "directory") {
        navigate(absolute);
        return;
      }
      setSelectedFile(absolute);
    },
    [listing, navigate, separator],
  );

  /** Run one mutation, adopt the listing it answered with, and surface its refusal. */
  const runMutation = useCallback(
    async (
      call: () => Promise<WorkspaceListingPayload>,
      onRefused?: (error: ApiError) => boolean,
    ) => {
      setBusy(true);
      setActionError(null);
      try {
        replace(await call());
        return true;
      } catch (e) {
        // A refusal here is the server's answer and not a transport failure: 409 is
        // "that name is taken" or "the folder is not empty", 403 is the containment
        // boundary. Showing its own message beats translating a status code badly.
        if (e instanceof ApiError && onRefused?.(e)) return false;
        setActionError(e instanceof ApiError ? e.message : (e as Error).message);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [replace],
  );

  const submitNewFolder = useCallback(async () => {
    const name = (newFolderName ?? "").trim();
    if (!name) return;
    if (await runMutation(() => createWorkspaceFolder(token, path, name))) {
      setNewFolderName(null);
    }
  }, [newFolderName, path, runMutation, token]);

  const submitRename = useCallback(async () => {
    if (!renaming) return;
    const next = renaming.value.trim();
    if (!next || next === renaming.name) {
      setRenaming(null);
      return;
    }
    if (await runMutation(() => renameWorkspaceEntry(token, path, renaming.name, next))) {
      setRenaming(null);
    }
  }, [path, renaming, runMutation, token]);

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const { name, recursive } = pendingDelete;
    const done = await runMutation(
      () => deleteWorkspaceEntry(token, path, name, recursive),
      (error) => {
        // The server, not the client, decides that a tree is involved. Asking again
        // with what it just said beats sending `recursive` speculatively.
        if (error.status === 409 && /not empty/i.test(error.message) && !recursive) {
          setPendingDelete({ name, recursive: true });
          return true;
        }
        return false;
      },
    );
    if (done) {
      setPendingDelete(null);
      setSelectedFile(null);
    }
  }, [path, pendingDelete, runMutation, token]);

  const upload = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setBusy(true);
      setActionError(null);
      try {
        // One at a time and sequentially: each answer carries the fresh listing, and
        // the last one is then the state after every file landed. Sending them in
        // parallel would race those listings against each other.
        for (const file of Array.from(files)) {
          const dataUrl = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(new Error(`could not read ${file.name}`));
            reader.onload = () => resolve(String(reader.result));
            reader.readAsDataURL(file);
          });
          replace(await client.uploadWorkspaceFile(path, file.name, dataUrl));
        }
      } catch (e) {
        setActionError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [client, path, replace],
  );

  const download = useCallback(
    async (entry: WorkspaceEntry) => {
      if (!listing) return;
      setActionError(null);
      try {
        const blob = await downloadWorkspaceFile(token, [listing.path, entry.name].join(separator));
        // The blob is handed to the browser here rather than linked to, because the
        // route needs the bearer token. Revoked on the next tick: the click has
        // already been dispatched, and holding the object URL holds the bytes.
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = entry.name;
        anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (e) {
        setActionError(e instanceof ApiError ? e.message : (e as Error).message);
      }
    },
    [listing, separator, token],
  );

  const loadPreview = useCallback((target: string) => fetchWorkspaceFilePreview(token, target), [token]);

  const rootName = useMemo(() => {
    if (!listing) return "Workspace";
    const parts = listing.projectPath.split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] ?? listing.projectPath;
  }, [listing]);

  return (
    <div className="flex h-full min-h-0 w-full">
      <div className="flex h-full min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 flex-col">
            <div className="flex min-w-0 items-center gap-1 text-[14px] font-semibold text-foreground">
              <button
                type="button"
                onClick={() => navigate(null)}
                className="max-w-[220px] truncate rounded-md px-1.5 py-0.5 hover:bg-muted/70"
                title={listing?.projectPath}
              >
                {rootName}
              </button>
              {crumbs.map((crumb) => (
                <span key={crumb.path} className="flex min-w-0 items-center gap-1">
                  <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <button
                    type="button"
                    onClick={() => navigate(crumb.path)}
                    className="max-w-[180px] truncate rounded-md px-1.5 py-0.5 hover:bg-muted/70"
                  >
                    {crumb.name}
                  </button>
                </span>
              ))}
            </div>
            <span className="text-[11px] text-muted-foreground">
              {listing
                ? `${listing.entries.length} item${listing.entries.length === 1 ? "" : "s"}${
                  listing.truncated ? " · list cut, directory holds more" : ""
                }`
                : loading
                  ? "Loading…"
                  : ""}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              aria-label="Upload files"
              onChange={(event) => {
                void upload(event.target.files);
                // Cleared so choosing the same file twice fires change again.
                event.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={!listing || busy}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Upload className="h-3.5 w-3.5" /> Upload
            </button>
            <button
              type="button"
              onClick={() => setNewFolderName("")}
              disabled={!listing || busy}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <FolderPlus className="h-3.5 w-3.5" /> New folder
            </button>
            {listing?.parent ? (
              <button
                type="button"
                onClick={() => navigate(listing.parent)}
                className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
              >
                <CornerLeftUp className="h-3.5 w-3.5" /> Up
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => void refresh()}
              aria-label="Refresh listing"
              className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
            >
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            </button>
          </div>
        </div>

        {error ? (
          <div className="border-b border-border bg-destructive/10 px-4 py-2 text-[12px] text-destructive-text">
            {error}
          </div>
        ) : null}

        {actionError ? (
          <div className="flex items-center justify-between gap-3 border-b border-border bg-destructive/10 px-4 py-2 text-[12px] text-destructive-text">
            <span>{actionError}</span>
            <button
              type="button"
              onClick={() => setActionError(null)}
              aria-label="Dismiss error"
              className="rounded-full p-1 hover:bg-muted/70"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {listing && listing.entries.length === 0 && !loading ? (
            <div className="px-4 py-6 text-[13px] text-muted-foreground">This folder is empty.</div>
          ) : null}
          <ul>
            {newFolderName !== null ? (
              <li className="flex items-center gap-3 border-b border-border/45 px-4 py-2">
                <Folder className="h-4 w-4 text-muted-foreground" />
                <input
                  autoFocus
                  value={newFolderName}
                  onChange={(event) => setNewFolderName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void submitNewFolder();
                    if (event.key === "Escape") setNewFolderName(null);
                  }}
                  placeholder="New folder name"
                  aria-label="New folder name"
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-[13px] text-foreground outline-none focus:border-foreground/30"
                />
                <button
                  type="button"
                  onClick={() => void submitNewFolder()}
                  disabled={busy || !newFolderName.trim()}
                  aria-label="Create folder"
                  className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                >
                  <Check className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setNewFolderName(null)}
                  aria-label="Cancel new folder"
                  className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </li>
            ) : null}
            {(listing?.entries ?? []).map((entry) => {
              const absolute = listing ? [listing.path, entry.name].join(separator) : entry.name;
              const selected = selectedFile === absolute;
              return (
                <li key={entry.name} className="group relative">
                  {renaming?.name === entry.name ? (
                    <div className="flex items-center gap-3 px-4 py-2">
                      <EntryIcon entry={entry} />
                      <input
                        autoFocus
                        value={renaming.value}
                        onChange={(event) => setRenaming({ name: entry.name, value: event.target.value })}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void submitRename();
                          if (event.key === "Escape") setRenaming(null);
                        }}
                        aria-label={`Rename ${entry.name}`}
                        className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-[13px] text-foreground outline-none focus:border-foreground/30"
                      />
                      <button
                        type="button"
                        onClick={() => void submitRename()}
                        disabled={busy}
                        aria-label="Save name"
                        className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                      >
                        <Check className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setRenaming(null)}
                        aria-label="Cancel rename"
                        className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  ) : (
                    <div
                      className={cn(
                        "flex items-center gap-3 pr-2 transition-colors",
                        selected ? "bg-muted/70" : "hover:bg-muted/50",
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => openEntry(entry)}
                        disabled={entry.escapesWorkspace}
                        title={
                          entry.escapesWorkspace
                            ? "This link points outside the workspace and cannot be opened here"
                            : absolute
                        }
                        className={cn(
                          "flex min-w-0 flex-1 items-center gap-3 px-4 py-2 text-left text-[13px]",
                          "disabled:cursor-not-allowed disabled:opacity-60",
                        )}
                      >
                        <EntryIcon entry={entry} />
                        <span className="min-w-0 flex-1 truncate text-foreground">{entry.name}</span>
                        <span className="w-20 shrink-0 text-right text-[11px] text-muted-foreground">
                          {formatSize(entry.size)}
                        </span>
                        <span className="hidden w-40 shrink-0 text-right text-[11px] text-muted-foreground sm:block">
                          {formatModified(entry.modified)}
                        </span>
                      </button>
                      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                        {entry.kind === "file" ? (
                          <button
                            type="button"
                            onClick={() => void download(entry)}
                            disabled={busy}
                            aria-label={`Download ${entry.name}`}
                            className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                          >
                            <Download className="h-3.5 w-3.5" />
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => setRenaming({ name: entry.name, value: entry.name })}
                          disabled={busy}
                          aria-label={`Rename ${entry.name}`}
                          className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingDelete({ name: entry.name, recursive: false })}
                          disabled={busy}
                          aria-label={`Delete ${entry.name}`}
                          className="rounded-full p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive-text disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      <WorkspaceDeleteConfirm
        open={pendingDelete !== null}
        name={pendingDelete?.name ?? ""}
        recursive={pendingDelete?.recursive ?? false}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
      />

      {selectedFile ? (
        <FilePreviewPanel
          key={selectedFile}
          path={selectedFile}
          token={token}
          loadPreview={loadPreview}
          onClose={() => setSelectedFile(null)}
        />
      ) : null}
    </div>
  );
}
