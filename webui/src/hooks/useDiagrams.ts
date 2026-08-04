import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import {
  ApiError,
  createDiagram,
  deleteDiagramApi,
  fetchDiagram,
  fetchDiagrams,
  updateDiagram,
} from "@/lib/api";
import type { Diagram, DiagramSummary } from "@/components/diagrams/diagramTypes";

/** Diagram gallery + CRUD, backed by `/api/webui/diagrams*` (see `nanoinfra/diagrams/`). */
export function useDiagrams(): {
  diagrams: DiagramSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  load: (id: string) => Promise<Diagram | null>;
  save: (diagram: Diagram) => Promise<Diagram>;
  remove: (id: string) => Promise<boolean>;
} {
  const { getToken } = useClient();
  const [diagrams, setDiagrams] = useState<DiagramSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await fetchDiagrams(tokenRef.current());
      setDiagrams(payload.diagrams);
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

  const load = useCallback(async (id: string): Promise<Diagram | null> => {
    try {
      const payload = await fetchDiagram(tokenRef.current(), id);
      return payload.diagram;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  }, []);

  const save = useCallback(async (diagram: Diagram): Promise<Diagram> => {
    const payload = diagram.id
      ? await updateDiagram(tokenRef.current(), diagram.id, diagram)
      : await createDiagram(tokenRef.current(), diagram);
    await refresh();
    return payload.diagram;
  }, [refresh]);

  const remove = useCallback(async (id: string): Promise<boolean> => {
    try {
      const { deleted } = await deleteDiagramApi(tokenRef.current(), id);
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

  return { diagrams, loading, error, refresh, load, save, remove };
}
