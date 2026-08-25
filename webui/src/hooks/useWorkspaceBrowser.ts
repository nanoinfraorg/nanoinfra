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
  /** Adopt the listing a mutation answered with, instead of refetching it. */
  replace: (payload: WorkspaceListingPayload) => void;
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
      // The server's own sentence, not a status code. A 403 is the containment
      // boundary answering deliberately, and everything else already arrives as a
      // written reason -- including "restart nanoinfra gateway", which is what a
      // gateway too old to have these routes produces when the static handler
      // answers the API path with WebUI HTML. Collapsing that to `HTTP 200` is how
      // a stale gateway looks like a broken feature.
      setError(
        e instanceof ApiError
          ? e.status === 403
            ? "Outside the workspace"
            : e.message
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

  return { listing, loading, error, refresh, replace: setListing };
}
