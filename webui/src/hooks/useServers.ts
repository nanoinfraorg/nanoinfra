import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import {
  ApiError,
  createServer,
  deleteServerApi,
  fetchServer,
  fetchServers,
  updateServer,
  type ServerDetail,
  type ServerSummary,
  type ServerValues,
} from "@/lib/api";

/** Server inventory + CRUD, backed by `/api/webui/servers*` (see `nanoinfra/servers/`). */
export function useServers(): {
  servers: ServerSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  load: (id: string) => Promise<ServerDetail | null>;
  save: (id: string | null, values: ServerValues) => Promise<ServerDetail>;
  remove: (id: string) => Promise<boolean>;
} {
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

  const load = useCallback(async (id: string): Promise<ServerDetail | null> => {
    try {
      const payload = await fetchServer(tokenRef.current(), id);
      return payload.server;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  }, []);

  const save = useCallback(
    async (id: string | null, values: ServerValues): Promise<ServerDetail> => {
      const payload = id
        ? await updateServer(tokenRef.current(), id, values)
        : await createServer(tokenRef.current(), values);
      await refresh();
      return payload.server;
    },
    [refresh],
  );

  const remove = useCallback(async (id: string): Promise<boolean> => {
    try {
      const { deleted } = await deleteServerApi(tokenRef.current(), id);
      await refresh();
      return deleted;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        await refresh();
        return false;
      }
      throw e;
    }
  }, [refresh]);

  return { servers, loading, error, refresh, load, save, remove };
}
