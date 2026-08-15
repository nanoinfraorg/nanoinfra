import { useCallback, useState } from "react";

import { SecretForm } from "./SecretForm";
import { SecretList } from "./SecretList";
import { useSecrets, type SecretValues } from "@/hooks/useSecrets";
import type { SecretSummary } from "@/lib/api";

type Screen = { kind: "list" } | { kind: "form"; secret: SecretSummary | null };

/**
 * Secrets persist through `/api/webui/secrets*` (see `nanoinfra/secrets/`) via
 * the `useSecrets` hook — real, workspace-scoped storage, not a mock. This is
 * a human-operated WebUI CRUD surface only; there is deliberately no
 * agent-facing tool or skill for secrets.
 */
export function SecretsView() {
  const { secrets, error, save, remove } = useSecrets();
  const [screen, setScreen] = useState<Screen>({ kind: "list" });

  const handleNew = useCallback(() => {
    setScreen({ kind: "form", secret: null });
  }, []);

  const handleOpen = useCallback(
    (id: string) => {
      const secret = secrets.find((s) => s.id === id) ?? null;
      setScreen({ kind: "form", secret });
    },
    [secrets],
  );

  const handleDelete = useCallback(
    (id: string) => {
      remove(id).catch((e: unknown) => {
        console.error("Failed to delete secret", e);
      });
    },
    [remove],
  );

  const handleBack = useCallback(() => {
    setScreen({ kind: "list" });
  }, []);

  const handleSave = useCallback(
    async (values: SecretValues) => {
      await save(screen.kind === "form" ? screen.secret?.id ?? null : null, values);
      setScreen({ kind: "list" });
    },
    [save, screen],
  );

  if (screen.kind === "form") {
    return <SecretForm secret={screen.secret} onBack={handleBack} onSave={handleSave} />;
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {error ? (
        <div className="border-b border-border bg-destructive/10 px-4 py-2 text-[12px] text-destructive-text">
          {`Failed to load secrets: ${error}`}
        </div>
      ) : null}
      <SecretList secrets={secrets} onOpen={handleOpen} onNew={handleNew} onDelete={handleDelete} />
    </div>
  );
}
