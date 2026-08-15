import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import { Ban } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { usePageVisibility } from "@/hooks/usePageVisibility";
import { ApiError, clearGatesLatch, fetchGatesLatches } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import type { GatesLatchEntry, GatesLatchPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const POLL_MS = 15_000;

/**
 * Operator labels for the capability classes, from the gates policy panel (#25 §3).
 * The panel owns this copy, so both surfaces read one key for each class. An unknown class falls
 * back to its own name, because a new class must still be readable.
 */
const CLASS_LABELS: Record<string, { key: string; fallback: string }> = {
  "credential.access": { key: "settings.gates.rows.secret", fallback: "Read a secret" },
  "mutate.inventory": { key: "settings.gates.rows.inventory", fallback: "Inventory writes" },
  "mutate.remote": { key: "settings.gates.rows.remote", fallback: "Remote execution" },
};

interface LatchBannerProps {
  sessionKey: string;
  token: string;
}

/** A failed clear, kept per class so one banner never shows another banner's failure. */
interface ClearFailure {
  capabilityClass: string;
  message: string;
}

/**
 * The operator control for a denial latch (#28).
 *
 * A denial latches the capability class for the session, and the gate then refuses that class
 * without a prompt to anybody. Only an operator lifts the latch, so the control cannot be a
 * chat command: a chat command would let the model ask for its own latch to go away. This
 * banner is that control, and it lives outside the transcript.
 *
 * The refusal count comes from the audit log, so it survives a gateway restart (#32) and never
 * resets to zero.
 */
export function LatchBanner({ sessionKey, token }: LatchBannerProps) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<GatesLatchPayload | null>(null);
  const [openClass, setOpenClass] = useState<string | null>(null);
  const [busyClass, setBusyClass] = useState<string | null>(null);
  const [clearFailure, setClearFailure] = useState<ClearFailure | null>(null);
  const pageVisible = usePageVisibility();
  const tokenRef = useRef(token);
  tokenRef.current = token;

  const load = useCallback(async () => {
    try {
      setPayload(await fetchGatesLatches(tokenRef.current));
    } catch {
      // A gateway with no gate runtime answers 503. There is then no latch to show.
      setPayload(null);
    }
  }, []);

  useEffect(() => {
    // The count rises with each blocked attempt, so a poll keeps the number honest. A hidden
    // tab needs no poll, and the read walks the audit log on every call.
    if (!pageVisible) return;
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load, pageVisible]);

  const clear = useCallback(
    async (entry: GatesLatchEntry) => {
      setBusyClass(entry.capabilityClass);
      setClearFailure(null);
      try {
        await clearGatesLatch(tokenRef.current, {
          capabilityClass: entry.capabilityClass,
          sessionId: entry.sessionId,
        });
        await load();
      } catch (err) {
        // An ApiError means the gateway answered and kept the latch. Any other error means the
        // gateway sent no answer at all. The operator must read which one happened.
        setClearFailure({
          capabilityClass: entry.capabilityClass,
          message: err instanceof ApiError
            ? t("thread.latch.clearRefused", {
              defaultValue: "The gateway did not clear this latch. The block still holds.",
            })
            : t("thread.latch.clearNoAnswer", {
              defaultValue: "The gateway did not answer. The block still holds.",
            }),
        });
      } finally {
        setBusyClass(null);
      }
    },
    [load, t],
  );

  if (payload?.degraded) {
    // #32 fails closed. The log cannot name the sessions it lost, so every session waits.
    return <Shell>{payload.summary}</Shell>;
  }

  const entries = (payload?.latches ?? []).filter((entry) => entry.sessionId === sessionKey);
  if (!entries.length) return null;

  return (
    <>
      {entries.map((entry) => {
        const label = classLabel(t, entry.capabilityClass);
        const open = openClass === entry.capabilityClass;
        return (
          <Shell key={entry.capabilityClass}>
            <p className="font-medium">
              {t("thread.latch.title", {
                defaultValue: "{{label}} is latched for this session.",
                label,
              })}
            </p>
            <p className="mt-0.5 text-destructive-text/80">
              {deniedSentence(t, entry)} {refusedSentence(t, entry.refusals)}
            </p>
            <p className="mt-0.5 text-destructive-text/80">
              {t("thread.latch.mustClear", {
                defaultValue: "The agent cannot ask again until you clear this.",
              })}
            </p>
            {clearFailure?.capabilityClass === entry.capabilityClass ? (
              <p className="mt-1 font-medium">{clearFailure.message}</p>
            ) : null}
            {open ? <Attempts entry={entry} /> : null}
            <div className="mt-2 flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                className="h-7 px-2 text-[12px]"
                onClick={() => setOpenClass(open ? null : entry.capabilityClass)}
              >
                {open
                  ? t("thread.latch.hideAttempts", { defaultValue: "Hide attempts" })
                  : t("thread.latch.viewAttempts", { defaultValue: "View attempts" })}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 px-2 text-[12px]"
                disabled={busyClass === entry.capabilityClass}
                onClick={() => void clear(entry)}
              >
                {t("thread.latch.clear", { defaultValue: "Clear" })}
              </Button>
            </div>
          </Shell>
        );
      })}
    </>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className={cn(
        "mb-2 flex items-start gap-2 rounded-lg border border-destructive/30",
        "bg-destructive/10 px-3 py-2 text-[12px] leading-5 text-destructive-text",
        "animate-in fade-in-0 slide-in-from-bottom-1",
      )}
    >
      <Ban className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">{children}</div>
    </div>
  );
}

function Attempts({ entry }: { entry: GatesLatchEntry }) {
  const { t } = useTranslation();
  return (
    <div className="mt-2 rounded-md bg-destructive/10 px-2 py-1.5">
      {entry.reason ? (
        <p className="text-destructive-text/80">
          {t("thread.latch.reason", { defaultValue: "Reason: {{reason}}", reason: entry.reason })}
        </p>
      ) : null}
      {entry.attempts.length ? (
        <ul className="mt-1 space-y-0.5">
          {entry.attempts.map((attempt, index) => (
            <li key={`${attempt.at ?? index}-${index}`} className="text-destructive-text/80">
              {[fmtDateTime(attempt.at), attempt.tool, attempt.digest]
                .filter((part) => !!part)
                .join(" · ")}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-destructive-text/80">
          {t("thread.latch.noAttempts", {
            defaultValue: "The audit log holds no attempt detail.",
          })}
        </p>
      )}
    </div>
  );
}

function classLabel(t: TFunction, capabilityClass: string): string {
  const label = CLASS_LABELS[capabilityClass];
  if (!label) return capabilityClass;
  return t(label.key, { defaultValue: label.fallback });
}

function deniedSentence(t: TFunction, entry: GatesLatchEntry): string {
  const at = fmtDateTime(entry.deniedAt);
  if (at && entry.deniedBy) {
    return t("thread.latch.deniedAtBy", {
      defaultValue: "Denied {{at}} by {{by}}.",
      at,
      by: entry.deniedBy,
    });
  }
  if (at) return t("thread.latch.deniedAt", { defaultValue: "Denied {{at}}.", at });
  if (entry.deniedBy) {
    return t("thread.latch.deniedUnknownTimeBy", {
      defaultValue: "Denied at a time the audit log does not state by {{by}}.",
      by: entry.deniedBy,
    });
  }
  return t("thread.latch.deniedUnknownTime", {
    defaultValue: "Denied at a time the audit log does not state.",
  });
}

function refusedSentence(t: TFunction, refusals: number): string {
  if (refusals < 1) {
    return t("thread.latch.refusedNone", { defaultValue: "No attempt was refused yet." });
  }
  if (refusals === 1) {
    return t("thread.latch.refusedOne", { defaultValue: "1 attempt refused since." });
  }
  return t("thread.latch.refusedMany", {
    defaultValue: "{{count}} attempts refused since.",
    count: refusals,
  });
}
