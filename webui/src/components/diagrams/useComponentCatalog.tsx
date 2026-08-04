import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { fetchDiagramCatalog } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

import { findComponentType, findProvider, type ComponentProvider, type ComponentType } from "./componentCatalog";

interface ComponentCatalogValue {
  componentTypes: ComponentType[];
  loading: boolean;
  error: string | null;
  findComponentType: (id: string) => ComponentType | undefined;
  findProvider: (componentTypeId: string, providerId: string) => ComponentProvider | undefined;
  // Replaces the old hardcoded GROUP_COMPONENT_ID sentinel — the palette and
  // canvas ask "which fetched type is the group container" instead.
  groupType: ComponentType | undefined;
  refresh: () => void;
}

const ComponentCatalogContext = createContext<ComponentCatalogValue | null>(null);

// A Context, not a plain fetch-on-use hook (contrast with useSkills.ts) —
// DiagramNode/GroupNode render once per node on the canvas, so each would
// independently re-fetch the whole catalog if this were called directly in
// every node instance. One fetch per diagram session instead, shared by
// the palette, canvas nodes, and inspector.
export function ComponentCatalogProvider({ children }: { children: ReactNode }) {
  const { getToken } = useClient();
  const [componentTypes, setComponentTypes] = useState<ComponentType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchDiagramCatalog(getToken())
      .then((payload) => {
        if (cancelled) return;
        setComponentTypes(payload.componentTypes);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  useEffect(() => load(), [load]);

  const value = useMemo<ComponentCatalogValue>(
    () => ({
      componentTypes,
      loading,
      error,
      findComponentType: (id) => findComponentType(componentTypes, id),
      findProvider: (componentTypeId, providerId) => findProvider(componentTypes, componentTypeId, providerId),
      groupType: componentTypes.find((c) => c.isGroup),
      refresh: load,
    }),
    [componentTypes, loading, error, load],
  );

  return <ComponentCatalogContext.Provider value={value}>{children}</ComponentCatalogContext.Provider>;
}

export function useComponentCatalog(): ComponentCatalogValue {
  const ctx = useContext(ComponentCatalogContext);
  if (!ctx) {
    throw new Error("useComponentCatalog must be used within a ComponentCatalogProvider");
  }
  return ctx;
}
