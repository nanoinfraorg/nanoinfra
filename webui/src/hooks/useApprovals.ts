import { useCallback, useEffect, useRef, useState } from "react";

import { usePageVisibility } from "@/hooks/usePageVisibility";
import { ApiError, answerGatesApproval, fetchGatesApprovals } from "@/lib/api";
import type {
  GatesApprovalAnswer,
  GatesApprovalAnswerValues,
  GatesPendingApprovalView,
} from "@/lib/types";

/** How often the inbox reads the queue. The unread count drives the navigation badge. */
const POLL_MS = 5_000;

export interface ApprovalsState {
  pending: GatesPendingApprovalView[];
  count: number;
  /** The gateway holds the inbox, and the read of the operator socket failed. */
  degraded: boolean;
  /** This gateway holds no inbox at all, so the route answered 503. */
  unavailable: boolean;
  loading: boolean;
  /** The request id of the answer in flight, or null. */
  answering: string | null;
  outcome: GatesApprovalAnswer | null;
  answer: (values: GatesApprovalAnswerValues) => Promise<void>;
}

/**
 * The pending approvals of one gateway (nanoinfraorg/nanoinfra#27).
 *
 * The hook converts `expiresInS` into an absolute deadline as the response arrives. The two
 * processes share no clock origin, because the executor reads a monotonic clock, so a remaining
 * time is the only value the wire can carry. The view counts down against the local deadline.
 *
 * A failed read reports `degraded` and keeps the count at zero. An empty list must not read as
 * "no action waits", because the executor may be unreachable instead.
 */
export function useApprovals(getToken: () => string): ApprovalsState {
  const [pending, setPending] = useState<GatesPendingApprovalView[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [answering, setAnswering] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<GatesApprovalAnswer | null>(null);
  const pageVisible = usePageVisibility();
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;

  const refresh = useCallback(async () => {
    try {
      const payload = await fetchGatesApprovals(tokenRef.current());
      const readAt = Date.now();
      setPending(
        payload.pending.map((entry) => ({
          ...entry,
          expiresAt: readAt + Math.max(0, entry.expiresInS) * 1000,
        })),
      );
      setDegraded(payload.degraded);
      setUnavailable(false);
    } catch (err) {
      // A 503 means this gateway holds no inbox. Any other failure means the read itself
      // failed, and the queue behind it stays unknown.
      setPending([]);
      setUnavailable(err instanceof ApiError && err.status === 503);
      setDegraded(!(err instanceof ApiError && err.status === 503));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!pageVisible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [pageVisible, refresh]);

  const answer = useCallback(
    async (values: GatesApprovalAnswerValues) => {
      setAnswering(values.requestId);
      try {
        setOutcome(await answerGatesApproval(tokenRef.current(), values));
      } catch {
        // The answer never reached the executor, so the action still waits. The operator reads
        // that, rather than a refusal nobody issued.
        setOutcome({
          actor: "",
          decision: values.decision,
          degraded: true,
          error: null,
          ok: false,
          refusal: null,
          requestId: values.requestId,
        });
      } finally {
        setAnswering(null);
        await refresh();
      }
    },
    [refresh],
  );

  return {
    answer,
    answering,
    count: pending.length,
    degraded,
    loading,
    outcome,
    pending,
    unavailable,
  };
}
