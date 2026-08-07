import { useCallback, useState } from "react";

import { ServerForm } from "./ServerForm";
import { ServerList } from "./ServerList";
import { useServers } from "@/hooks/useServers";
import type { ServerDetail, ServerValues } from "@/lib/api";

type Screen =
  | { kind: "list" }
  | { kind: "loading" }
  | { kind: "form"; server: ServerDetail | null };

/**
 * Servers persist through `/api/webui/servers*` (see `nanoinfra/servers/`)
 * via the `useServers` hook — real, workspace-scoped inventory, not a mock.
 * This is a human-operated WebUI CRUD surface only; no agent tool changes
 * are made here.
 */
export function ServersView() {
  const { servers, error, load, save, remove } = useServers();
  const [screen, setScreen] = useState<Screen>({ kind: "list" });

  const handleNew = useCallback(() => {
    setScreen({ kind: "form", server: null });
  }, []);

  const handleOpen = useCallback(
    async (id: string) => {
      setScreen({ kind: "loading" });
      const server = await load(id);
      setScreen(server ? { kind: "form", server } : { kind: "list" });
    },
    [load],
  );

  const handleDelete = useCallback(
    (id: string) => {
      remove(id).catch((e: unknown) => {
        console.error("Failed to delete server", e);
      });
    },
    [remove],
  );

  const handleBack = useCallback(() => {
    setScreen({ kind: "list" });
  }, []);

  const handleSave = useCallback(
    async (values: ServerValues) => {
      await save(screen.kind === "form" ? screen.server?.id ?? null : null, values);
      setScreen({ kind: "list" });
    },
    [save, screen],
  );

  if (screen.kind === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center text-[13px] text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (screen.kind === "form") {
    return <ServerForm server={screen.server} onBack={handleBack} onSave={handleSave} />;
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {error ? (
        <div className="border-b border-border bg-destructive/10 px-4 py-2 text-[12px] text-destructive">
          Failed to load servers: {error}
        </div>
      ) : null}
      <ServerList servers={servers} onOpen={handleOpen} onNew={handleNew} onDelete={handleDelete} />
    </div>
  );
}
