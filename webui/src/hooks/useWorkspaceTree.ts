import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, fetchWorkspaceListing } from "@/lib/api";
import type { WorkspaceEntry, WorkspaceListingPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

/** One rendered line of the tree. */
export interface WorkspaceTreeRow {
  /** Absolute path of this entry. */
  path: string;
  /** Absolute path of the directory holding it — what a mutation needs as `parent`. */
  parent: string;
  entry: WorkspaceEntry;
  depth: number;
  expanded: boolean;
  /** Expanded, but its listing has not arrived yet. */
  loading: boolean;
}

export function joinPath(directory: string, name: string, separator: string): string {
  // "." is the workspace root, and joining onto it would produce "./cron" — correct but noisy in
  // every URL the explorer builds. The listing addresses children relative to the workspace now.
  if (directory === "." || directory === "") return name;
  return directory.endsWith(separator) ? `${directory}${name}` : `${directory}${separator}${name}`;
}

function readError(e: unknown): string {
  // The server's own sentence. A 403 is the containment boundary answering
  // deliberately; everything else already arrives as a written reason, including
  // the "restart nanoinfra gateway" a gateway without these routes produces.
  if (e instanceof ApiError) {
    return e.status === 403 ? "Outside the workspace" : e.message;
  }
  return (e as Error).message;
}

/**
 * The Workspaces tree: many directories at once, expanded in place.
 *
 * One listing per expanded directory, cached by absolute path. Expansion is a
 * fetch, not a filter — the server decides what a directory holds, and it filters
 * dot entries itself (see `nanoinfra/webui/file_browser.py`), so nothing that was
 * hidden ever crossed the wire to be un-hidden here.
 */
export function useWorkspaceTree(
  rootPath: string | null,
  includeHidden: boolean,
  workspace: string | null = null,
): {
  root: WorkspaceListingPayload | null;
  rows: WorkspaceTreeRow[];
  separator: string;
  loading: boolean;
  error: string | null;
  isExpanded: (path: string) => boolean;
  toggle: (path: string) => void;
  expand: (path: string) => void;
  /** Adopt a listing a mutation answered with. */
  replace: (listing: WorkspaceListingPayload) => void;
  /** Refetch these directories if the tree is showing them. */
  invalidate: (paths: (string | null)[]) => Promise<void>;
  refresh: () => Promise<void>;
} {
  const { getToken } = useClient();
  const [listings, setListings] = useState<Map<string, WorkspaceListingPayload>>(new Map());
  const [rootKey, setRootKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;
  // Paths with a request in flight, so the effect that fills expanded-but-unloaded
  // directories cannot re-fire for one it is already fetching.
  const inFlight = useRef<Set<string>>(new Set());

  const fetchInto = useCallback(
    async (target: string | null, options?: { isRoot?: boolean }) => {
      const key = target ?? "";
      if (inFlight.current.has(key)) return;
      inFlight.current.add(key);
      try {
        const payload = await fetchWorkspaceListing(
          tokenRef.current(),
          target,
          includeHidden,
          workspace,
        );
        setListings((previous) => new Map(previous).set(payload.path, payload));
        if (options?.isRoot) setRootKey(payload.path);
        setError(null);
      } catch (e) {
        setError(readError(e));
      } finally {
        inFlight.current.delete(key);
      }
    },
    [includeHidden, workspace],
  );

  // The root, and a full reset whenever the root or the dot-entry view changes:
  // every cached listing was fetched under the old question, so keeping them would
  // show a mix of both answers.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setListings(new Map());
    setRootKey(null);
    inFlight.current.clear();
    void fetchInto(rootPath, { isRoot: true }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [fetchInto, rootPath]);

  // Fill in directories the operator left expanded but whose listing is missing —
  // after a reset, or when one was expanded before its parent had arrived.
  useEffect(() => {
    if (rootKey === null) return;
    for (const path of expanded) {
      if (!listings.has(path) && !inFlight.current.has(path)) void fetchInto(path);
    }
  }, [expanded, fetchInto, listings, rootKey]);

  const separator = useMemo(() => {
    const root = rootKey === null ? null : listings.get(rootKey);
    return root && root.projectPath.includes("\\") ? "\\" : "/";
  }, [listings, rootKey]);

  const rows = useMemo(() => {
    const out: WorkspaceTreeRow[] = [];
    if (rootKey === null) return out;
    const walk = (directory: string, depth: number) => {
      const listing = listings.get(directory);
      if (!listing) return;
      for (const entry of listing.entries) {
        const path = joinPath(listing.path, entry.name, separator);
        const isDirectory = entry.kind === "directory";
        const isExpanded = isDirectory && expanded.has(path);
        out.push({
          path,
          parent: listing.path,
          entry,
          depth,
          expanded: isExpanded,
          loading: isExpanded && !listings.has(path),
        });
        if (isExpanded) walk(path, depth + 1);
      }
    };
    walk(rootKey, 0);
    return out;
  }, [expanded, listings, rootKey, separator]);

  const expand = useCallback((path: string) => {
    setExpanded((previous) => {
      if (previous.has(path)) return previous;
      const next = new Set(previous);
      next.add(path);
      return next;
    });
  }, []);

  const toggle = useCallback((path: string) => {
    setExpanded((previous) => {
      const next = new Set(previous);
      // Collapsing keeps the cached listing: re-expanding is then instant, and the
      // bytes are already paid for.
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const isExpanded = useCallback((path: string) => expanded.has(path), [expanded]);

  const replace = useCallback((listing: WorkspaceListingPayload) => {
    setListings((previous) => new Map(previous).set(listing.path, listing));
  }, []);

  const invalidate = useCallback(
    async (paths: (string | null)[]) => {
      const wanted = new Set<string>();
      for (const path of paths) {
        const key = path ?? rootKey;
        // Only what the tree is actually showing: a directory nobody expanded will
        // be fetched fresh when it is.
        if (key !== null && listings.has(key)) wanted.add(key);
      }
      await Promise.all([...wanted].map((path) => fetchInto(path === rootKey ? rootPath : path)));
    },
    [fetchInto, listings, rootKey, rootPath],
  );

  const refresh = useCallback(async () => {
    const open = [...listings.keys()];
    await Promise.all(
      open.map((path) => fetchInto(path === rootKey ? rootPath : path, { isRoot: path === rootKey })),
    );
  }, [fetchInto, listings, rootKey, rootPath]);

  return {
    root: rootKey === null ? null : listings.get(rootKey) ?? null,
    rows,
    separator,
    loading,
    error,
    isExpanded,
    toggle,
    expand,
    replace,
    invalidate,
    refresh,
  };
}
