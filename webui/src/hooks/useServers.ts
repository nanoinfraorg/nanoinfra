import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import { ApiError, fetchServers, type ServerSummary } from "@/lib/api";

/** Server inventory listing, backed by `/api/webui/servers` (see `nanoinfra/servers/`). */
export function useServers(): { servers: ServerSummary[]; loading: boolean; error: string | null } {
  const { getToken } = useClient();
  const [servers, setServers] = useState<ServerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await fetchServers(tokenRef.current());
      setServers(payload.servers);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { servers, loading, error };
}
