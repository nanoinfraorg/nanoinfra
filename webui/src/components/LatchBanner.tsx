import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Ban } from "lucide-react";

import { Button } from "@/components/ui/button";
import { usePageVisibility } from "@/hooks/usePageVisibility";
import { fmtDateTime } from "@/lib/format";
import { fetchWithTimeout } from "@/lib/http";
import { cn } from "@/lib/utils";

const READ_PATH = "/api/webui/gates/latches";
const CLEAR_PATH = "/api/webui/gates/latches/clear";
const VALUES_HEADER = "X-Nanoinfra-Latch-Values";
const POLL_MS = 15_000;
const TIMEOUT_MS = 20_000;

/**
 * Operator labels for the capability classes, from the gates policy panel (#25 §3).
 * An unknown class falls back to its own name, because a new class must still be readable.
 */
const CLASS_LABELS: Record<string, string> = {
  "credential.access": "Read a secret",
  "mutate.inventory": "Inventory writes",
  "mutate.remote": "Remote execution",
};

interface LatchAttempt {
  at: string | null;
  digest: string | null;
  tool: string | null;
}

interface LatchEntry {
  attempts: LatchAttempt[];
  capabilityClass: string;
  deniedAt: string | null;
  deniedBy: string | null;
  reason: string | null;
  refusals: number;
  sessionId: string;
}

interface LatchPayload {
  degraded: boolean;
  latches: LatchEntry[];
  summary: string;
}

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
  const [payload, setPayload] = useState<LatchPayload | null>(null);
  const [openClass, setOpenClass] = useState<string | null>(null);
  const [busyClass, setBusyClass] = useState<string | null>(null);
  const [clearFailure, setClearFailure] = useState<ClearFailure | null>(null);
  const pageVisible = usePageVisibility();
  const tokenRef = useRef(token);
  tokenRef.current = token;

  const load = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(
        READ_PATH,
        {
          headers: { Authorization: `Bearer ${tokenRef.current}` },
          credentials: "same-origin",
        },
        TIMEOUT_MS,
      );
      // A gateway with no gate runtime answers 503. There is then no latch to show.
      setPayload(res.ok ? ((await res.json()) as LatchPayload) : null);
    } catch {
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
    async (entry: LatchEntry) => {
      setBusyClass(entry.capabilityClass);
      setClearFailure(null);
      try {
        const res = await fetchWithTimeout(
          CLEAR_PATH,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${tokenRef.current}`,
              [VALUES_HEADER]: JSON.stringify({
                capabilityClass: entry.capabilityClass,
                sessionId: entry.sessionId,
              }),
            },
            credentials: "same-origin",
          },
          TIMEOUT_MS,
        );
        if (!res.ok) {
          setClearFailure({
            capabilityClass: entry.capabilityClass,
            message: "The gateway did not clear this latch. The block still holds.",
          });
          return;
        }
        await load();
      } catch {
        setClearFailure({
          capabilityClass: entry.capabilityClass,
          message: "The gateway did not answer. The block still holds.",
        });
      } finally {
        setBusyClass(null);
      }
    },
    [load],
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
        const label = CLASS_LABELS[entry.capabilityClass] ?? entry.capabilityClass;
        const open = openClass === entry.capabilityClass;
        return (
          <Shell key={entry.capabilityClass}>
            <p className="font-medium">{label} is latched for this session.</p>
            <p className="mt-0.5 text-destructive/80">
              {deniedSentence(entry)} {refusedSentence(entry.refusals)}
            </p>
            <p className="mt-0.5 text-destructive/80">
              The agent cannot ask again until you clear this.
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
                {open ? "Hide attempts" : "View attempts"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 px-2 text-[12px]"
                disabled={busyClass === entry.capabilityClass}
                onClick={() => void clear(entry)}
              >
                Clear
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
        "bg-destructive/10 px-3 py-2 text-[12px] leading-5 text-destructive",
        "animate-in fade-in-0 slide-in-from-bottom-1",
      )}
    >
      <Ban className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">{children}</div>
    </div>
  );
}

function Attempts({ entry }: { entry: LatchEntry }) {
  return (
    <div className="mt-2 rounded-md bg-destructive/10 px-2 py-1.5">
      {entry.reason ? <p className="text-destructive/80">Reason: {entry.reason}</p> : null}
      {entry.attempts.length ? (
        <ul className="mt-1 space-y-0.5">
          {entry.attempts.map((attempt, index) => (
            <li key={`${attempt.at ?? index}-${index}`} className="text-destructive/80">
              {[fmtDateTime(attempt.at), attempt.tool, attempt.digest]
                .filter((part) => !!part)
                .join(" · ")}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-destructive/80">The audit log holds no attempt detail.</p>
      )}
    </div>
  );
}

function deniedSentence(entry: LatchEntry): string {
  const at = fmtDateTime(entry.deniedAt);
  const when = at ? `Denied ${at}` : "Denied at a time the audit log does not state";
  return entry.deniedBy ? `${when} by ${entry.deniedBy}.` : `${when}.`;
}

function refusedSentence(refusals: number): string {
  if (refusals < 1) return "No attempt was refused yet.";
  if (refusals === 1) return "1 attempt refused since.";
  return `${refusals} attempts refused since.`;
}
