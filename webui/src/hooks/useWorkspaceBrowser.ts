import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchWorkspaceListing } from "@/lib/api";
import type { WorkspaceListingPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

/**
 * One directory of the active workspace, backed by `/api/webui/workspace/list`
 * (see `nanoinfra/webui/file_browser.py`).
 *
 * The *path* argument is the absolute path the server answered with, not one this
 * hook builds by string concatenation: every listing carries its own `parent` and
 * its children's names, so navigation only ever replays a path the server has
 * already resolved and contained.
 */
export function useWorkspaceBrowser(path: string | null): {
  listing: WorkspaceListingPayload | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
} {
  const { getToken } = useClient();
  const [listing, setListing] = useState<WorkspaceListingPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const load = useCallback(async (target: string | null) => {
    setLoading(true);
    try {
      const payload = await fetchWorkspaceListing(tokenRef.current(), target);
      setListing(payload);
      setError(null);
    } catch (e) {
      // The message matters here in a way it does not in the gallery views: a 403
      // is the containment boundary answering, and "HTTP 403" alone reads like a
      // bug rather than the deliberate refusal it is.
      setError(
        e instanceof ApiError
          ? `${e.status === 403 ? "Outside the workspace" : `HTTP ${e.status}`}`
          : (e as Error).message,
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(path);
  }, [load, path]);

  const refresh = useCallback(() => load(path), [load, path]);

  return { listing, loading, error, refresh };
}
