/**
 * The connection state, and who the gateway thinks you are -- nanoinfraorg/nanoinfra#70.
 *
 * A misconfigured proxy is invisible until an approval fails, and an approval fails at the worst
 * moment. So the identity reads here, beside the state an operator already checks, and not on a
 * page they have to find.
 *
 * The value comes from the gateway on the ready frame. The browser never sets it: a display the
 * browser could set would lie exactly when an operator needs it.
 *
 * A deployment with no proxy answers ``webui``. That reads in the same words and the same tone as
 * an asserted identity, because it is the true actor of that deployment and not a fault.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";
import type { ConnectionStatus } from "@/lib/types";

const COPY: Record<ConnectionStatus, { color: string }> = {
  idle: { color: "text-muted-foreground" },
  connecting: {
    color: "text-amber-700 dark:text-amber-300",
  },
  open: {
    color: "text-emerald-700 dark:text-emerald-400",
  },
  reconnecting: {
    color: "text-amber-700 dark:text-amber-300",
  },
  closed: {
    color: "text-muted-foreground",
  },
  error: {
    color: "text-destructive-text",
  },
};

export function ConnectionBadge() {
  const { t } = useTranslation();
  const { client } = useClient();
  const [status, setStatus] = useState<ConnectionStatus>(client.status);
  const [actor, setActor] = useState<string | null>(client.operatorActor);

  useEffect(() => client.onStatus(setStatus), [client]);
  useEffect(() => client.onOperatorActor(setActor), [client]);

  const meta = COPY[status];
  const pulsing =
    status === "connecting" ||
    status === "reconnecting" ||
    status === "error";
  // Three lines, and the last two arrive together. An operator reads the actor, then reads what
  // to do with it, which is to write that exact value in the approver list.
  const label = [
    t(`connection.${status}`),
    ...(actor
      ? [t("connection.identity.actor", { actor }), t("connection.identity.approverNote")]
      : []),
  ].join("\n");
  return (
    <span
      className={cn(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
        "text-muted-foreground/70 hover:bg-sidebar-accent/65",
        meta.color,
      )}
      aria-live="polite"
      role="status"
      title={label}
    >
      <span className="relative flex h-2 w-2" aria-hidden>
        {pulsing && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-75" />
        )}
        <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
      </span>
      <span className="sr-only">{label}</span>
    </span>
  );
}
