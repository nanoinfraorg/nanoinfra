import { useCallback, useMemo, useRef, useState } from "react";
import type { DragEvent as ReactDragEvent } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  CornerLeftUp,
  Download,
  Eye,
  EyeOff,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderUp,
  Link2,
  Loader2,
  Pencil,
  RefreshCw,
  ShieldAlert,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import {
  WorkspaceContextMenu,
  type ContextMenuAnchor,
  type ContextMenuItem,
} from "@/components/workspace/WorkspaceContextMenu";
import { WorkspaceDeleteConfirm } from "@/components/workspace/WorkspaceDeleteConfirm";
import { WorkspacePicker } from "@/components/workspace/WorkspacePicker";
import {
  WorkspaceUploadReview,
  type UploadPlan,
} from "@/components/workspace/WorkspaceUploadReview";
import { useFilePreviewResize } from "@/hooks/useFilePreviewResize";
import { useWorkspaceTree, type WorkspaceTreeRow } from "@/hooks/useWorkspaceTree";
import {
  ApiError,
  createWorkspaceFolder,
  deleteWorkspaceEntry,
  downloadWorkspaceFile,
  fetchWorkspaceFilePreview,
  moveWorkspaceEntry,
  renameWorkspaceEntry,
} from "@/lib/api";
import {
  MAX_DROPPED_FILES,
  MAX_UPLOAD_BYTES,
  UPLOAD_CHUNK_BYTES,
  collectDroppedFiles,
  filesFromList,
  type DroppedFile,
} from "@/lib/dropped-files";
import type { WorkspaceEntry, WorkspaceListingPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/**
 * Our own drag type, so a drag from this tree is told apart from any other drag
 * crossing it. `text/plain` would be indistinguishable from a text selection
 * dragged in from elsewhere, and acting on that would move a file nobody picked up.
 */
const DRAG_MIME = "application/x-nanoinfra-workspace-entry";

interface DragPayload {
  parent: string;
  name: string;
  path: string;
  kind: WorkspaceEntry["kind"];
}

interface WorkspaceViewProps {
  /** Absolute path the tree is rooted at, or `null` for the workspace root. */
  path?: string | null;
  onPathChange?: (path: string | null, options?: { replace?: boolean }) => void;
  /** Which workspace is open, or `null` for the configured one. */
  workspace?: string | null;
  onWorkspaceChange?: (workspace: string | null) => void;
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

function EntryIcon({ entry, expanded }: { entry: WorkspaceEntry; expanded: boolean }) {
  if (entry.escapesWorkspace) return <ShieldAlert className="h-4 w-4 text-muted-foreground" />;
  if (entry.kind === "symlink") return <Link2 className="h-4 w-4 text-muted-foreground" />;
  if (entry.kind === "directory") {
    return expanded
      ? <FolderOpen className="h-4 w-4 text-muted-foreground" />
      : <Folder className="h-4 w-4 text-muted-foreground" />;
  }
  return <FileText className="h-4 w-4 text-muted-foreground" />;
}

/** An inline name field, used for both "new folder" and "rename". */
function NameInput({
  value,
  label,
  depth,
  busy,
  onChange,
  onSubmit,
  onCancel,
}: {
  value: string;
  label: string;
  depth: number;
  busy: boolean;
  onChange: (next: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="flex items-center gap-2 py-1 pr-2" style={{ paddingLeft: `${12 + depth * 14}px` }}>
      <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
      <input
        autoFocus
        value={value}
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") onSubmit();
          if (event.key === "Escape") onCancel();
        }}
        className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-[13px] text-foreground outline-none focus:border-foreground/30"
      />
      <button
        type="button"
        onClick={onSubmit}
        disabled={busy || !value.trim()}
        aria-label="Confirm name"
        className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
      >
        <Check className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={onCancel}
        aria-label="Cancel"
        className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

/**
 * The Workspaces explorer: one tree over the active workspace.
 *
 * Every path acted on came from a listing the server produced — a row's own path,
 * its parent, or the root — and the server contains each one again on the way back
 * in (`nanoinfra/webui/file_browser.py`). Containment there is unconditional and
 * does not read `restrict_to_workspace`, which governs the agent's file tools and
 * not what a browser may reach.
 */
export function WorkspaceView({
  path = null,
  onPathChange,
  workspace = null,
  onWorkspaceChange,
}: WorkspaceViewProps) {
  const { client, token } = useClient();
  // Dot entries are off by default: a workspace under version control has a `.git`
  // whose objects are not what an operator opened this to look at. The server does
  // the filtering, so this is a refetch rather than a client-side unfilter.
  const [showHidden, setShowHidden] = useState(false);
  const tree = useWorkspaceTree(path, showHidden, workspace);
  const { root, rows, separator } = tree;

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ anchor: ContextMenuAnchor; row: WorkspaceTreeRow | null } | null>(null);
  const [newFolder, setNewFolder] = useState<{ parent: string; depth: number; value: string } | null>(null);
  const [renaming, setRenaming] = useState<
    { parent: string; name: string; depth: number; value: string } | null
  >(null);
  const [pendingDelete, setPendingDelete] = useState<
    { parent: string; name: string; recursive: boolean } | null
  >(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const uploadTargetRef = useRef<string | null>(null);
  const [progress, setProgress] = useState<
    { done: number; total: number; part?: { index: number; count: number } } | null
  >(null);
  const [plan, setPlan] = useState<UploadPlan | null>(null);
  const dragRef = useRef<DragPayload | null>(null);
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const { width: previewWidth, onResizeStart } = useFilePreviewResize(
    surfaceRef,
    selectedFile !== null,
  );

  const rootName = useMemo(() => {
    if (!root) return "Workspace";
    const parts = root.path.split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] ?? root.path;
  }, [root]);

  /** Run one mutation, adopt the listing it answered with, and surface its refusal. */
  const runMutation = useCallback(
    async (call: () => Promise<WorkspaceListingPayload>, onRefused?: (error: ApiError) => boolean) => {
      setBusy(true);
      setActionError(null);
      try {
        tree.replace(await call());
        return true;
      } catch (e) {
        // A refusal is the server's answer, not a transport failure: 409 is a taken
        // name or a non-empty folder, 403 is the containment boundary, 400 is a move
        // into a folder's own subtree. Its sentence beats a status code.
        if (e instanceof ApiError && onRefused?.(e)) return false;
        setActionError(e instanceof ApiError ? e.message : (e as Error).message);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [tree],
  );

  const submitNewFolder = useCallback(async () => {
    if (!newFolder) return;
    const name = newFolder.value.trim();
    if (!name) return;
    const parent = newFolder.parent;
    if (await runMutation(() => createWorkspaceFolder(token, parent, name, showHidden, workspace))) {
      setNewFolder(null);
      tree.expand(parent);
    }
  }, [newFolder, runMutation, showHidden, token, tree, workspace]);

  const submitRename = useCallback(async () => {
    if (!renaming) return;
    const next = renaming.value.trim();
    if (!next || next === renaming.name) {
      setRenaming(null);
      return;
    }
    const done = await runMutation(() =>
      renameWorkspaceEntry(token, renaming.parent, renaming.name, next, showHidden, workspace),
    );
    if (done) setRenaming(null);
  }, [renaming, runMutation, showHidden, token, workspace]);

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const { parent, name, recursive } = pendingDelete;
    const done = await runMutation(
      () => deleteWorkspaceEntry(token, parent, name, recursive, showHidden, workspace),
      (error) => {
        // The server decides that a tree is involved, and the second dialog says so.
        // `recursive` is never sent speculatively.
        if (error.status === 409 && /not empty/i.test(error.message) && !recursive) {
          setPendingDelete({ parent, name, recursive: true });
          return true;
        }
        return false;
      },
    );
    if (done) {
      setPendingDelete(null);
      setSelectedFile(null);
    }
  }, [pendingDelete, runMutation, showHidden, token, workspace]);

  const download = useCallback(
    async (row: WorkspaceTreeRow) => {
      setActionError(null);
      try {
        const blob = await downloadWorkspaceFile(token, row.path, workspace);
        // Handed to the browser here rather than linked to, because the route needs
        // the bearer token. Revoked on the next tick: the click is already dispatched,
        // and holding the object URL holds the bytes.
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = row.entry.name;
        anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (e) {
        setActionError(e instanceof ApiError ? e.message : (e as Error).message);
      }
    },
    [token],
  );

  /** Show what would be uploaded, and where, before anything is written. */
  const proposeUpload = useCallback(
    (directory: string, files: DroppedFile[], truncated: boolean) => {
      if (files.length === 0) {
        // Said out loud rather than silently doing nothing: a folder drop that the
        // browser reported as empty looks identical to one that never fired.
        setActionError("nothing to upload — no files were found in what was dropped");
        return;
      }
      const label = directory === root?.path
        ? rootName
        : directory.slice(directory.lastIndexOf(separator) + 1);
      setPlan({ destination: directory, destinationLabel: label, files, truncated });
    },
    [root, rootName, separator],
  );

  const uploadInto = useCallback(
    async (directory: string, dropped: DroppedFile[], truncated = false) => {
      if (dropped.length === 0) return;
      setBusy(true);
      setActionError(null);
      setProgress({ done: 0, total: dropped.length });
      const failures: string[] = [];
      try {
        // Sequential on purpose: each answer carries the listing of the directory, so
        // the last one is the state after every file landed. In parallel those
        // listings would race each other — and a tree's intermediate directories
        // would be created by several requests at once.
        for (const [index, item] of dropped.entries()) {
          if (item.file.size > MAX_UPLOAD_BYTES) {
            failures.push(
              `${item.relativePath}: larger than the ${Math.round(MAX_UPLOAD_BYTES / (1024 * 1024))} MB limit`,
            );
            setProgress({ done: index + 1, total: dropped.length });
            continue;
          }
          try {
            // Sent in frame-sized parts, so the size a person sees is not the size a
            // WebSocket frame happens to allow. A single-part file omits the chunk
            // fields entirely, which is the shape the server answers in one go.
            const parts = Math.max(1, Math.ceil(item.file.size / UPLOAD_CHUNK_BYTES));
            const uploadId = parts > 1 ? crypto.randomUUID() : undefined;
            let answered: WorkspaceListingPayload | null = null;
            for (let part = 0; part < parts; part += 1) {
              setProgress({
                done: index,
                total: dropped.length,
                ...(parts > 1 ? { part: { index: part + 1, count: parts } } : {}),
              });
              const slice = parts > 1
                ? item.file.slice(part * UPLOAD_CHUNK_BYTES, (part + 1) * UPLOAD_CHUNK_BYTES)
                : item.file;
              const dataUrl = await new Promise<string>((resolve, reject) => {
                const reader = new FileReader();
                reader.onerror = () => reject(new Error(`could not read ${item.file.name}`));
                reader.onload = () => resolve(String(reader.result));
                reader.readAsDataURL(slice);
              });
              answered = await client.uploadWorkspaceFile(directory, item.file.name, dataUrl, {
                includeHidden: showHidden,
                relativePath: item.relativePath,
                workspace,
                ...(uploadId !== undefined
                  ? { uploadId, chunkIndex: part, chunkCount: parts }
                  : {}),
              });
            }
            // Only the part that finished the file carries one.
            if (answered) tree.replace(answered);
          } catch (e) {
            // One refused file does not abandon the rest of the tree: a folder often
            // holds something that already exists, and stopping there would leave the
            // upload half done with no account of what landed.
            failures.push(`${item.relativePath}: ${(e as Error).message}`);
          }
          setProgress({ done: index + 1, total: dropped.length });
        }
        tree.expand(directory);
      } finally {
        setBusy(false);
        setProgress(null);
        setPlan(null);
      }
      const notes: string[] = [];
      if (truncated) {
        notes.push(`only the first ${MAX_DROPPED_FILES} files were uploaded`);
      }
      if (failures.length > 0) {
        notes.push(
          `${failures.length} of ${dropped.length} not uploaded — ${failures.slice(0, 3).join("; ")}${
            failures.length > 3 ? "; …" : ""
          }`,
        );
      }
      if (notes.length > 0) setActionError(notes.join(". "));
    },
    [client, showHidden, tree, workspace],
  );

  const move = useCallback(
    async (drag: DragPayload, destination: string) => {
      const done = await runMutation(() =>
        moveWorkspaceEntry(token, drag.parent, drag.name, destination, showHidden, workspace),
      );
      if (done) {
        // The source parent came back in the answer; the destination is elsewhere in
        // the tree, and is refetched only if it is open.
        await tree.invalidate([destination]);
        tree.expand(destination);
      }
    },
    [runMutation, showHidden, token, tree, workspace],
  );

  /** Whether *destination* is a place this drag could land. */
  const canDropInto = useCallback(
    (destination: string, drag: DragPayload | null) => {
      if (!drag) return true; // external files
      if (drag.parent === destination) return false; // already there
      if (drag.path === destination) return false; // onto itself
      // Into its own subtree: refused by the server too, but offering the target
      // would be inviting a drop that cannot work.
      return !destination.startsWith(`${drag.path}${separator}`);
    },
    [separator],
  );

  const onRowDragStart = useCallback((event: ReactDragEvent, row: WorkspaceTreeRow) => {
    const payload: DragPayload = {
      parent: row.parent,
      name: row.entry.name,
      path: row.path,
      kind: row.entry.kind,
    };
    dragRef.current = payload;
    event.dataTransfer.setData(DRAG_MIME, JSON.stringify(payload));
    event.dataTransfer.effectAllowed = "move";
  }, []);

  const onDirectoryDragOver = useCallback(
    (event: ReactDragEvent, destination: string) => {
      const external = event.dataTransfer.types.includes("Files");
      const internal = event.dataTransfer.types.includes(DRAG_MIME);
      if (!external && !internal) return;
      if (internal && !canDropInto(destination, dragRef.current)) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = external ? "copy" : "move";
      setDropTarget(destination);
    },
    [canDropInto],
  );

  const onDirectoryDrop = useCallback(
    (event: ReactDragEvent, destination: string) => {
      event.preventDefault();
      event.stopPropagation();
      setDropTarget(null);
      const raw = event.dataTransfer.getData(DRAG_MIME);
      dragRef.current = null;
      if (raw) {
        try {
          const drag = JSON.parse(raw) as DragPayload;
          if (canDropInto(destination, drag)) void move(drag, destination);
        } catch {
          setActionError("could not read what was dropped");
        }
        return;
      }
      // Read before any await: the DataTransfer is emptied once the event handler
      // returns, so collecting it later finds nothing.
      const transfer = event.dataTransfer;
      if (transfer.items.length > 0 || transfer.files.length > 0) {
        void collectDroppedFiles(transfer).then(({ files, truncated }) =>
          proposeUpload(destination, files, truncated),
        );
      }
    },
    [canDropInto, move, uploadInto],
  );

  const openFolderAsRoot = useCallback(
    (target: string | null) => {
      setSelectedFile(null);
      onPathChange?.(target);
    },
    [onPathChange],
  );

  const menuItems = useMemo((): ContextMenuItem[] => {
    const row = menu?.row ?? null;
    const directory = row
      ? row.entry.kind === "directory" ? row.path : row.parent
      : root?.path ?? null;
    const depth = row ? (row.entry.kind === "directory" ? row.depth + 1 : row.depth) : 0;
    const items: ContextMenuItem[] = [];
    const actionable = row !== null && !row.entry.escapesWorkspace;

    if (actionable && row) {
      if (row.entry.kind === "directory") {
        items.push({
          label: row.expanded ? "Collapse" : "Expand",
          icon: row.expanded
            ? <ChevronDown className="h-3.5 w-3.5" />
            : <ChevronRight className="h-3.5 w-3.5" />,
          onSelect: () => tree.toggle(row.path),
        });
        items.push({
          label: "Open as root",
          icon: <FolderOpen className="h-3.5 w-3.5" />,
          onSelect: () => openFolderAsRoot(row.path),
        });
      } else {
        items.push({
          label: "Preview",
          icon: <FileText className="h-3.5 w-3.5" />,
          onSelect: () => setSelectedFile(row.path),
        });
        items.push({
          label: "Download",
          icon: <Download className="h-3.5 w-3.5" />,
          onSelect: () => void download(row),
        });
      }
    }

    if (directory !== null) {
      items.push({
        label: "New folder",
        icon: <FolderPlus className="h-3.5 w-3.5" />,
        startsGroup: items.length > 0,
        onSelect: () => {
          if (row?.entry.kind === "directory") tree.expand(directory);
          setNewFolder({ parent: directory, depth, value: "" });
        },
      });
      items.push({
        label: "Upload files here",
        icon: <Upload className="h-3.5 w-3.5" />,
        onSelect: () => {
          uploadTargetRef.current = directory;
          fileInputRef.current?.click();
        },
      });
      items.push({
        label: "Upload folder here",
        icon: <FolderUp className="h-3.5 w-3.5" />,
        onSelect: () => {
          uploadTargetRef.current = directory;
          folderInputRef.current?.click();
        },
      });
    }

    if (actionable && row) {
      items.push({
        label: "Rename",
        icon: <Pencil className="h-3.5 w-3.5" />,
        startsGroup: true,
        onSelect: () =>
          setRenaming({
            parent: row.parent,
            name: row.entry.name,
            depth: row.depth,
            value: row.entry.name,
          }),
      });
      items.push({
        label: "Delete",
        icon: <Trash2 className="h-3.5 w-3.5" />,
        danger: true,
        onSelect: () =>
          setPendingDelete({ parent: row.parent, name: row.entry.name, recursive: false }),
      });
    }
    return items;
  }, [download, menu, openFolderAsRoot, root, tree]);

  // Stable across renders. The panel holds its loader in a ref too, so either alone
  // is enough -- but a fresh closure per render is the kind of thing that quietly
  // starts refetching again the next time someone touches that effect.
  const loadPreview = useCallback(
    (target: string) => fetchWorkspaceFilePreview(token, target, workspace),
    [token, workspace],
  );

  const rootDropActive = dropTarget !== null && root !== null && dropTarget === root.path;

  return (
    <div ref={surfaceRef} className="relative flex h-full min-h-0 w-full">
      <div className="flex h-full min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-[14px] font-semibold text-foreground" title={root?.path}>
              {rootName}
            </span>
            <span className="text-[11px] text-muted-foreground">
              {root
                ? `${root.entries.length} item${root.entries.length === 1 ? "" : "s"}${
                  root.hiddenCount > 0 ? ` · ${root.hiddenCount} hidden` : ""
                }${root.truncated ? " · list cut, directory holds more" : ""}`
                : tree.loading
                  ? "Loading…"
                  : ""}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {onWorkspaceChange ? (
              <WorkspacePicker
                workspace={workspace}
                onChange={(next) => {
                  // The open sub-path belongs to the workspace being left.
                  onPathChange?.(null);
                  setSelectedFile(null);
                  onWorkspaceChange(next);
                }}
              />
            ) : null}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              aria-label="Upload files"
              onChange={(event) => {
                const destination = uploadTargetRef.current ?? root?.path ?? null;
                const picked = filesFromList(event.target.files);
                if (destination !== null) {
                  proposeUpload(destination, picked.files, picked.truncated);
                }
                uploadTargetRef.current = null;
                // Cleared so choosing the same file twice fires change again.
                event.target.value = "";
              }}
            />
            <input
              ref={folderInputRef}
              type="file"
              multiple
              className="hidden"
              aria-label="Upload folder"
              // Non-standard, and the only way a file picker offers a directory.
              // Every file then carries `webkitRelativePath`, which is the tree.
              {...{ webkitdirectory: "", directory: "" }}
              onChange={(event) => {
                const destination = uploadTargetRef.current ?? root?.path ?? null;
                const picked = filesFromList(event.target.files);
                if (destination !== null) {
                  proposeUpload(destination, picked.files, picked.truncated);
                }
                uploadTargetRef.current = null;
                event.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => {
                uploadTargetRef.current = root?.path ?? null;
                fileInputRef.current?.click();
              }}
              disabled={!root || busy}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Upload className="h-3.5 w-3.5" /> Upload
            </button>
            <button
              type="button"
              onClick={() => {
                uploadTargetRef.current = root?.path ?? null;
                folderInputRef.current?.click();
              }}
              disabled={!root || busy}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <FolderUp className="h-3.5 w-3.5" /> Upload folder
            </button>
            <button
              type="button"
              onClick={() => root && setNewFolder({ parent: root.path, depth: 0, value: "" })}
              disabled={!root || busy}
              className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <FolderPlus className="h-3.5 w-3.5" /> New folder
            </button>
            {root?.parent ? (
              <button
                type="button"
                onClick={() => openFolderAsRoot(root.parent)}
                className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
              >
                <CornerLeftUp className="h-3.5 w-3.5" /> Up
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => setShowHidden((previous) => !previous)}
              aria-label={showHidden ? "Hide dot files" : "Show dot files"}
              title={showHidden ? "Hide dot files" : "Show dot files"}
              aria-pressed={showHidden}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-full hover:bg-muted/70",
                showHidden ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {showHidden ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={() => void tree.refresh()}
              aria-label="Refresh listing"
              className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
            >
              <RefreshCw className={cn("h-4 w-4", tree.loading && "animate-spin")} />
            </button>
          </div>
        </div>

        {tree.error ? (
          <div className="border-b border-border bg-destructive/10 px-4 py-2 text-[12px] text-destructive-text">
            {tree.error}
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

        <div
          data-testid="workspace-tree"
          className={cn(
            "min-h-0 flex-1 overflow-y-auto",
            rootDropActive && "bg-muted/40 ring-2 ring-inset ring-foreground/20",
          )}
          onContextMenu={(event) => {
            event.preventDefault();
            setMenu({ anchor: { x: event.clientX, y: event.clientY }, row: null });
          }}
          onDragOver={(event) => (root ? onDirectoryDragOver(event, root.path) : undefined)}
          onDragLeave={() => setDropTarget(null)}
          onDrop={(event) => (root ? onDirectoryDrop(event, root.path) : undefined)}
        >
          {newFolder && root && newFolder.parent === root.path ? (
            <NameInput
              value={newFolder.value}
              label="New folder name"
              depth={0}
              busy={busy}
              onChange={(value) => setNewFolder({ ...newFolder, value })}
              onSubmit={() => void submitNewFolder()}
              onCancel={() => setNewFolder(null)}
            />
          ) : null}

          {root && root.entries.length === 0 && !tree.loading ? (
            <div className="px-4 py-6 text-[13px] text-muted-foreground">
              This folder is empty. Drop files here to upload them.
            </div>
          ) : null}

          <ul>
            {rows.map((row) => {
              const isDirectory = row.entry.kind === "directory";
              const selected = selectedFile === row.path;
              const isRenaming = renaming?.parent === row.parent && renaming.name === row.entry.name;
              const dropActive = dropTarget === row.path;
              return (
                <li key={row.path}>
                  {isRenaming && renaming ? (
                    <NameInput
                      value={renaming.value}
                      label={`Rename ${row.entry.name}`}
                      depth={row.depth}
                      busy={busy}
                      onChange={(value) => setRenaming({ ...renaming, value })}
                      onSubmit={() => void submitRename()}
                      onCancel={() => setRenaming(null)}
                    />
                  ) : (
                    <div
                      draggable={!row.entry.escapesWorkspace}
                      onDragStart={(event) => onRowDragStart(event, row)}
                      onDragEnd={() => {
                        dragRef.current = null;
                        setDropTarget(null);
                      }}
                      onDragOver={(event) => (isDirectory ? onDirectoryDragOver(event, row.path) : undefined)}
                      onDragLeave={() => (isDirectory ? setDropTarget(null) : undefined)}
                      onDrop={(event) => (isDirectory ? onDirectoryDrop(event, row.path) : undefined)}
                      onContextMenu={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        setMenu({ anchor: { x: event.clientX, y: event.clientY }, row });
                      }}
                      className={cn(
                        "group flex items-center gap-1 pr-2 transition-colors",
                        selected && "bg-muted/70",
                        dropActive
                          ? "bg-muted/60 ring-2 ring-inset ring-foreground/25"
                          : !selected && "hover:bg-muted/50",
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          if (row.entry.escapesWorkspace) return;
                          if (isDirectory) tree.toggle(row.path);
                          else setSelectedFile(row.path);
                        }}
                        onDoubleClick={() => (isDirectory ? openFolderAsRoot(row.path) : undefined)}
                        disabled={row.entry.escapesWorkspace}
                        title={
                          row.entry.escapesWorkspace
                            ? "This link points outside the workspace and cannot be opened here"
                            : row.path
                        }
                        style={{ paddingLeft: `${12 + row.depth * 14}px` }}
                        className="flex min-w-0 flex-1 items-center gap-2 py-1.5 pr-2 text-left text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        <span className="grid h-4 w-4 shrink-0 place-items-center text-muted-foreground">
                          {isDirectory
                            ? row.loading
                              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              : row.expanded
                                ? <ChevronDown className="h-3.5 w-3.5" />
                                : <ChevronRight className="h-3.5 w-3.5" />
                            : null}
                        </span>
                        <EntryIcon entry={row.entry} expanded={row.expanded} />
                        <span className="min-w-0 flex-1 truncate text-foreground">{row.entry.name}</span>
                        <span className="w-20 shrink-0 text-right text-[11px] text-muted-foreground">
                          {formatSize(row.entry.size)}
                        </span>
                        <span className="hidden w-40 shrink-0 text-right text-[11px] text-muted-foreground sm:block">
                          {formatModified(row.entry.modified)}
                        </span>
                      </button>
                      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                        {row.entry.kind === "file" ? (
                          <button
                            type="button"
                            onClick={() => void download(row)}
                            disabled={busy}
                            aria-label={`Download ${row.entry.name}`}
                            className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                          >
                            <Download className="h-3.5 w-3.5" />
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() =>
                            setRenaming({
                              parent: row.parent,
                              name: row.entry.name,
                              depth: row.depth,
                              value: row.entry.name,
                            })}
                          disabled={busy}
                          aria-label={`Rename ${row.entry.name}`}
                          className="rounded-full p-1.5 text-muted-foreground hover:bg-muted/70 hover:text-foreground disabled:opacity-50"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setPendingDelete({
                              parent: row.parent,
                              name: row.entry.name,
                              recursive: false,
                            })}
                          disabled={busy}
                          aria-label={`Delete ${row.entry.name}`}
                          className="rounded-full p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive-text disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  )}

                  {newFolder && isDirectory && newFolder.parent === row.path ? (
                    <NameInput
                      value={newFolder.value}
                      label="New folder name"
                      depth={row.depth + 1}
                      busy={busy}
                      onChange={(value) => setNewFolder({ ...newFolder, value })}
                      onSubmit={() => void submitNewFolder()}
                      onCancel={() => setNewFolder(null)}
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      <WorkspaceContextMenu
        anchor={menu?.anchor ?? null}
        items={menuItems}
        onClose={() => setMenu(null)}
      />

      <WorkspaceUploadReview
        key={plan ? `${plan.destination}:${plan.files.length}` : "none"}
        plan={plan}
        busy={busy}
        progress={progress}
        onCancel={() => setPlan(null)}
        onConfirm={(files) => {
          if (plan) void uploadInto(plan.destination, files, plan.truncated);
        }}
      />

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
          desktopWidth={previewWidth}
          onResizeStart={onResizeStart}
          onClose={() => setSelectedFile(null)}
        />
      ) : null}
    </div>
  );
}
