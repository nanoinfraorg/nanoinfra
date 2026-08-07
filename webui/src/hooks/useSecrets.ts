import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import {
  ApiError,
  createSecret,
  deleteSecretApi,
  fetchSecrets,
  updateSecret,
  type SecretSummary,
} from "@/lib/api";

export interface SecretValues {
  name: string;
  kind: string;
  providerId: string;
  value: string;
}

/** Secret gallery + CRUD, backed by `/api/webui/secrets*` (see `nanoinfra/secrets/`). */
export function useSecrets(): {
  secrets: SecretSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  save: (id: string | null, values: SecretValues) => Promise<SecretSummary>;
  remove: (id: string) => Promise<boolean>;
} {
  const { getToken } = useClient();
  const [secrets, setSecrets] = useState<SecretSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await fetchSecrets(tokenRef.current());
      setSecrets(payload.secrets);
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

  const save = useCallback(
    async (id: string | null, values: SecretValues): Promise<SecretSummary> => {
      const payload = id
        ? await updateSecret(tokenRef.current(), id, values)
        : await createSecret(tokenRef.current(), values);
      await refresh();
      return payload.secret;
    },
    [refresh],
  );

  const remove = useCallback(async (id: string): Promise<boolean> => {
    try {
      const { deleted } = await deleteSecretApi(tokenRef.current(), id);
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

  return { secrets, loading, error, refresh, save, remove };
}
