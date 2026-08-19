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
import type { DiagramUpdateHandler } from "@/lib/nanoinfra-client";
import type { Diagram, DiagramSummary } from "@/components/diagrams/diagramTypes";

/** Trailing window used to coalesce a burst of writes (e.g. the agent creating several nodes). */
const REFRESH_COALESCE_MS = 250;

/**
 * Subscribe to server-side diagram writes (``diagram_updated`` frames, raised by
 * `nanoinfra/diagrams/changes.py`).
 *
 * The handler is kept in a ref so a caller can pass an inline closure without
 * re-subscribing on every render.
 */
export function useDiagramUpdates(handler: DiagramUpdateHandler): void {
  const { client } = useClient();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    return client.onDiagramUpdate((diagramId, kind, revision) => {
      handlerRef.current(diagramId, kind, revision);
    });
  }, [client]);
}

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

  // The gallery follows the store: a diagram the agent just created, renamed or
  // deleted shows up without the operator reloading the page. Summaries are
  // small and this collapses a burst into one request, so refetching the list is
  // cheaper than reasoning about how each frame edits it in place — and it also
  // picks up the derived fields only the server computes (`updatedAt`, and the
  // `modified_outside` / `unreadable` status).
  const coalesceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useDiagramUpdates(
    useCallback(() => {
      if (coalesceRef.current) clearTimeout(coalesceRef.current);
      coalesceRef.current = setTimeout(() => {
        coalesceRef.current = null;
        void refresh();
      }, REFRESH_COALESCE_MS);
    }, [refresh]),
  );
  useEffect(() => {
    return () => {
      if (coalesceRef.current) clearTimeout(coalesceRef.current);
    };
  }, []);

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
