import { useCallback, useMemo, useState } from "react";
import {
  ChevronRight,
  CornerLeftUp,
  FileText,
  Folder,
  Link2,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { useWorkspaceBrowser } from "@/hooks/useWorkspaceBrowser";
import { fetchWorkspaceFilePreview } from "@/lib/api";
import type { WorkspaceEntry } from "@/lib/types";
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
  const { token } = useClient();
  const { listing, loading, error, refresh } = useWorkspaceBrowser(path);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

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

        <div className="min-h-0 flex-1 overflow-y-auto">
          {listing && listing.entries.length === 0 && !loading ? (
            <div className="px-4 py-6 text-[13px] text-muted-foreground">This folder is empty.</div>
          ) : null}
          <ul>
            {(listing?.entries ?? []).map((entry) => {
              const absolute = listing ? [listing.path, entry.name].join(separator) : entry.name;
              const selected = selectedFile === absolute;
              return (
                <li key={entry.name}>
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
                      "flex w-full items-center gap-3 px-4 py-2 text-left text-[13px] transition-colors",
                      "disabled:cursor-not-allowed disabled:opacity-60",
                      selected ? "bg-muted/70" : "hover:bg-muted/50",
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
                </li>
              );
            })}
          </ul>
        </div>
      </div>

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
