import { useEffect, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import { AlertTriangle, CheckCircle2, ChevronDown, ShieldQuestion, Timer } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type {
  GatesApprovalAnswer,
  GatesApprovalAnswerValues,
  GatesPendingApprovalView,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** How often the countdown redraws. One second, because an operator reads seconds. */
const TICK_MS = 1_000;

/**
 * The sentence for each rule that refuses an answer.
 *
 * The executor sends the machine name, and this map turns it into an instruction. A name this
 * map does not hold falls back to the executor's own sentence, so a new rule stays readable.
 */
const REFUSAL_KEYS: Record<string, { key: string; fallback: string }> = {
  already_answered: {
    key: "approvals.refusal.alreadyAnswered",
    fallback: "This action already has an answer. One action takes one answer.",
  },
  digest_mismatch: {
    key: "approvals.refusal.digestMismatch",
    fallback:
      "Your answer covers other bytes. Reload this page, read the payload again, then answer.",
  },
  expired: {
    key: "approvals.refusal.expired",
    fallback: "This action expired before your answer arrived. The executor refused it.",
  },
  no_second_path: {
    key: "approvals.refusal.noSecondPath",
    fallback:
      "Only one authenticated path exists. Add a second path, or declare a standing grant.",
  },
  not_an_approver: {
    key: "approvals.refusal.notAnApprover",
    fallback: "This account is not in gates.approvers. Add it to that list, then answer again.",
  },
  same_path: {
    key: "approvals.refusal.samePath",
    fallback:
      "This request came from the path you are on. Answer it from another authenticated path.",
  },
  unauthenticated_path: {
    key: "approvals.refusal.unauthenticatedPath",
    fallback: "gates.approvalPaths does not list webui, so this path authenticates no approver.",
  },
  unknown_origin_path: {
    key: "approvals.refusal.unknownOriginPath",
    fallback: "The request names no origin path. The executor cannot prove path independence.",
  },
  unknown_request: {
    key: "approvals.refusal.unknownRequest",
    fallback: "The executor does not hold this request. It expired, or somebody answered it.",
  },
};

export interface ApprovalsViewProps {
  pending: GatesPendingApprovalView[];
  degraded: boolean;
  unavailable: boolean;
  loading: boolean;
  answering: string | null;
  outcome: GatesApprovalAnswer | null;
  onAnswer: (values: GatesApprovalAnswerValues) => void;
}

/**
 * The approvals inbox (nanoinfraorg/nanoinfra#27).
 *
 * A human reads this screen and authorizes a remote command. Every byte in the payload box comes
 * from the executor, through the renderer in #14. No field on this screen is model-authored. A
 * model-written summary would put the unfaithful summarization problem inside the security path:
 * the human authorizes a sentence, the executor runs a command, and nothing compares the two.
 *
 * The approve control echoes the digest that arrived with the payload. A mismatch refuses and
 * leaves the action pending, which is the point of the digest.
 *
 * Deny costs one click, the same as approve. A deny that cost more steps would make the human
 * the rate limiter of a brute-force loop.
 *
 * Approve is a split button (nanoinfraorg/nanoinfra#220). The bare click stays plain **Approve**,
 * because the default action of a split button is the one people press without reading, so it is
 * the one that grants nothing. Behind the caret, the same click also writes the standing grant
 * this action implies. Nothing here describes that grant: the duration is the only choice, and
 * the command and hosts come from the payload the executor rendered.
 *
 * The context rows name the **acting agent** (nanoinfraorg/nanoinfra#258). An operator approving
 * a command otherwise reads the request without knowing whether the manager or one of its peers
 * runs it, and with delegation those are different blast radii. The row names the peer and the
 * agent that delegated to it, it sits below the divider because the digest does not cover it,
 * and it says the name is the agent's own claim. Where no agent is named the row is absent
 * altogether: absent attribution renders nothing rather than a guess, and that is the case in
 * every deployment that does not delegate.
 */
export function ApprovalsView({
  answering,
  degraded,
  loading,
  onAnswer,
  outcome,
  pending,
  unavailable,
}: ApprovalsViewProps) {
  const { t } = useTranslation();
  const now = useTick(pending.length > 0);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold text-foreground">
            {t("approvals.title", { defaultValue: "Approvals" })}
          </span>
          <span className="text-[11px] text-muted-foreground">
            {t("approvals.subtitle", {
              defaultValue: "One human answer per action. The executor rendered every byte below.",
            })}
          </span>
        </div>
        <span className="rounded-full border border-border/45 px-3 py-1 text-[12px] font-medium text-foreground">
          {t("approvals.pendingCount", { count: pending.length, defaultValue: "Pending: {{count}}" })}
        </span>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
        {outcome ? <Outcome outcome={outcome} /> : null}
        {unavailable ? (
          <Notice tone="warning">
            {t("approvals.unavailable", {
              defaultValue:
                "This gateway holds no approvals inbox. An approve decision waits for the "
                + "deadline and then refuses.",
            })}
          </Notice>
        ) : null}
        {degraded ? (
          <Notice tone="warning">
            {t("approvals.degraded", {
              defaultValue:
                "The gateway cannot reach the executor. An action may wait, and this list "
                + "cannot show it.",
            })}
          </Notice>
        ) : null}
        {pending.map((entry) => (
          <ApprovalCard
            busy={answering === entry.requestId}
            entry={entry}
            key={entry.requestId}
            now={now}
            onAnswer={onAnswer}
          />
        ))}
        {!pending.length && !degraded && !unavailable && !loading ? (
          <Notice tone="quiet">
            {t("approvals.empty", {
              defaultValue:
                "No action waits for an answer. Policy still refuses every action it refuses, "
                + "so an empty inbox is not an open gate.",
            })}
          </Notice>
        ) : null}
      </div>
    </div>
  );
}

function ApprovalCard({
  busy,
  entry,
  now,
  onAnswer,
}: {
  busy: boolean;
  entry: GatesPendingApprovalView;
  now: number;
  onAnswer: (values: GatesApprovalAnswerValues) => void;
}) {
  const { t } = useTranslation();
  // The confirmation for the one option a click makes permanent. It lives per card, because two
  // waiting actions are two separate decisions.
  const [confirmPermanent, setConfirmPermanent] = useState(false);
  // Absent, blank and whitespace all mean "no agent named itself". One test here rather than
  // three at the two places that read it.
  const actingAgent = entry.actingAgent?.trim() ?? "";
  const delegatedBy = entry.delegatedBy?.trim() ?? "";
  const remainingMs = entry.expiresAt - now;
  const expired = remainingMs <= 0;
  const approvable = !busy && !expired && !entry.samePath;

  const approve = (grant?: GatesApprovalAnswerValues["grant"]) =>
    onAnswer({
      decision: "approve",
      requestId: entry.requestId,
      targetDigest: entry.targetDigest,
      ...(grant ? { grant } : {}),
    });

  return (
    <section className="rounded-[14px] border border-border/45 bg-settings-surface p-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border/45 pb-2 text-[12px]">
        <span className="flex items-center gap-1.5 font-medium text-foreground">
          <Timer className="h-3.5 w-3.5" aria-hidden />
          {expired
            ? t("approvals.expired", {
              defaultValue: "This action expired. The executor refused it.",
            })
            : t("approvals.timeLeft", {
              defaultValue: "{{time}} left",
              time: countdown(remainingMs),
            })}
        </span>
        <span className="text-muted-foreground">
          {t("approvals.hosts", { count: entry.hostCount, defaultValue: "Hosts: {{count}}" })}
        </span>
        <span className="text-muted-foreground">
          {t("approvals.from", { defaultValue: "from {{path}}", path: entry.originPath })}
        </span>
      </div>

      <p className="mt-3 text-[12px] font-medium text-foreground">
        {t("approvals.payloadTitle", {
          defaultValue: "Command and hosts, exactly as the executor will run them",
        })}
      </p>
      <pre className="mt-1 max-h-96 overflow-auto rounded-md border border-border/45 bg-muted/40 p-3 text-[12px] leading-5 text-foreground">
        {entry.payload}
      </pre>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t("approvals.payloadNote", {
          defaultValue: "The executor produced this text. The agent did not describe it.",
        })}
      </p>

      <p className="mt-3 text-[12px] font-medium text-foreground">
        {t("approvals.hostsTitle", {
          count: entry.hostCount,
          defaultValue: "Resolved hosts: {{count}}",
        })}
      </p>
      <ul className="mt-1 flex flex-wrap gap-1.5">
        {entry.hosts.map((host) => (
          <li
            className="rounded-md border border-border/45 px-1.5 py-0.5 font-mono text-[11px] text-foreground"
            key={host}
          >
            {host}
          </li>
        ))}
      </ul>

      <p className="mt-4 border-t border-dashed border-border/45 pt-2 text-[11px] text-muted-foreground">
        {t("approvals.contextDivider", {
          defaultValue: "The lines below are context. The digest does not cover them.",
        })}
      </p>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[12px]">
        <ContextRow
          label={t("approvals.context.origin", { defaultValue: "Requested on" })}
          value={entry.originPath}
        />
        <ContextRow
          label={t("approvals.context.approve", { defaultValue: "Approve from" })}
          value={t("approvals.approveFromValue", {
            defaultValue: "webui (this session, authenticated)",
          })}
        />
        <ContextRow
          label={t("approvals.context.session", { defaultValue: "Session" })}
          value={entry.sessionId}
        />
        <ContextRow
          label={t("approvals.context.class", { defaultValue: "Capability class" })}
          value={`${entry.capabilityClass} · ${entry.scope} · ${entry.executionContext}`}
        />
        {actingAgent ? (
          <ContextRow
            label={t("approvals.context.agent", { defaultValue: "Acting agent" })}
            value={
              delegatedBy
                ? t("approvals.agentDelegated", {
                  agent: actingAgent,
                  defaultValue: "{{agent}} — delegated by {{delegatedBy}}",
                  delegatedBy,
                })
                : actingAgent
            }
          />
        ) : null}
        <ContextRow
          label={t("approvals.context.digest", { defaultValue: "Binding digest" })}
          value={entry.targetDigest}
        />
      </dl>

      {actingAgent ? (
        <p className="mt-1 text-[11px] text-muted-foreground">
          {t("approvals.agentAsserted", {
            defaultValue:
              "The agent named itself. That is a claim of the request, not an identity this "
              + "deployment authenticated, and the digest above does not cover it.",
          })}
        </p>
      ) : null}

      {entry.samePath ? (
        <Notice tone="warning">
          {t("approvals.samePath", {
            defaultValue:
              "This request came from the path you are on. Approve it from another "
              + "authenticated path. One compromised account must not supply both halves.",
          })}
        </Notice>
      ) : null}

      <p className="mt-3 text-[11px] text-muted-foreground">
        {t("approvals.denyTerminal", {
          defaultValue:
            "A denial is terminal for this session. The agent cannot ask again until an "
            + "operator clears the latch.",
        })}
      </p>
      {!expired ? (
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          {t("approvals.expiresNote", {
            defaultValue: "At zero the executor refuses this action. The agent gets no retry.",
          })}
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
        <Button
          className="h-8 px-3 text-[12px]"
          disabled={busy || expired}
          onClick={() => onAnswer({ decision: "deny", requestId: entry.requestId })}
          size="sm"
          variant="outline"
        >
          {t("approvals.deny", { defaultValue: "Deny" })}
        </Button>
        <div className="flex items-stretch overflow-hidden rounded-md border border-input">
          <Button
            className="h-8 rounded-none border-0 px-3 text-[12px]"
            disabled={busy || expired || entry.samePath}
            onClick={() => approve()}
            size="sm"
            variant="ghost"
          >
            {entry.samePath
              ? t("approvals.approveUnavailable", { defaultValue: "Approve (unavailable)" })
              : t("approvals.approve", { defaultValue: "Approve" })}
          </Button>
          {approvable ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  aria-label={t("approvals.grant.menuLabel", {
                    defaultValue: "Approve and add a standing grant",
                  })}
                  className="h-8 w-7 rounded-none border-0 border-l border-input px-0"
                  size="sm"
                  variant="ghost"
                >
                  <ChevronDown className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-w-[22rem]">
                <DropdownMenuLabel className="whitespace-normal text-[11px] font-normal text-muted-foreground">
                  {t("approvals.grant.menuNote", {
                    defaultValue:
                      "Adds a standing grant for this exact command on these hosts. A command "
                      + "that differs by one flag is a different command.",
                  })}
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={() => approve({ expires: "24h" })}>
                  {t("approvals.grant.add24h", {
                    defaultValue: "Approve and add — expires in 24 hours",
                  })}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => approve({ expires: "7d" })}>
                  {t("approvals.grant.add7d", {
                    defaultValue: "Approve and add — expires in 7 days",
                  })}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setConfirmPermanent(true)}>
                  {t("approvals.grant.addNever", {
                    defaultValue: "Approve and add — never expires",
                  })}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </div>
      </div>

      <PermanentGrantConfirm
        onCancel={() => setConfirmPermanent(false)}
        onConfirm={() => {
          setConfirmPermanent(false);
          approve({ expires: "never", permanentAcknowledged: true });
        }}
        open={confirmPermanent}
      />
    </section>
  );
}

/**
 * The second click behind "never expires".
 *
 * Not because permanent is wrong -- an operator may legitimately want it -- but because it is the
 * only option a click makes permanent, and this dialog is where the audit record gets an explicit
 * "yes, permanent" instead of inferring it from a duration string. A second click, and not a
 * second approver: a second approver is easy to add later and impossible to retrofit into records
 * already written without one.
 */
function PermanentGrantConfirm({
  onCancel,
  onConfirm,
  open,
}: {
  onCancel: () => void;
  onConfirm: () => void;
  open: boolean;
}) {
  const { t } = useTranslation();
  return (
    <AlertDialog onOpenChange={(next) => (next ? undefined : onCancel())} open={open}>
      <AlertDialogContent className="w-[min(calc(100vw-2rem),28rem)]">
        <AlertDialogHeader>
          <AlertDialogTitle>
            {t("approvals.grant.permanentTitle", { defaultValue: "Add a grant that never expires?" })}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {t("approvals.grant.permanentBody", {
              defaultValue:
                "This command runs on these hosts from now on, with nobody asked. Nothing "
                + "removes the grant later: you edit gates.standingGrants in config.json to take "
                + "it back.",
            })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>
            {t("common.cancel", { defaultValue: "Cancel" })}
          </AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>
            {t("approvals.grant.permanentConfirm", { defaultValue: "Yes, never expires" })}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ContextRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-[11px] text-foreground">{value}</dd>
    </>
  );
}

function Outcome({ outcome }: { outcome: GatesApprovalAnswer }) {
  const { t } = useTranslation();
  if (outcome.ok) {
    return (
      <>
        <Notice tone={outcome.decision === "deny" ? "warning" : "done"}>
          {outcome.decision === "deny"
            ? t("approvals.denied", {
              defaultValue: "You denied this action. The denial is terminal for this session.",
            })
            : t("approvals.approved", {
              defaultValue: "You approved this action. The executor runs it now.",
            })}
        </Notice>
        <GrantOutcome outcome={outcome} />
      </>
    );
  }
  return <Notice tone="warning">{refusalSentence(t, outcome)}</Notice>;
}

/**
 * What became of the grant, beside an approval that already went through.
 *
 * The two are reported separately because they happened in two processes and either one can fail
 * alone. A failed grant write must read as a failed grant write: an operator who saw "approved"
 * and assumed the grant landed would find out at 03:00, when the automation waits for a human
 * nobody is there to be.
 */
function GrantOutcome({ outcome }: { outcome: GatesApprovalAnswer }) {
  const { t } = useTranslation();
  const grant = outcome.grant;
  if (!grant) return null;
  if (!grant.ok) {
    return (
      <Notice tone="warning">
        {t("approvals.grant.notSaved", {
          defaultValue: "The action was approved. The grant was not saved: {{reason}}",
          reason: grant.reason ?? "",
        })}
      </Notice>
    );
  }
  return (
    <Notice tone="done">
      {grant.expiresAt
        ? t("approvals.grant.saved", {
          defaultValue: "Standing grant {{id}} added. It expires {{when}}.",
          id: grant.id ?? "",
          when: expiryLabel(grant.expiresAt),
        })
        : t("approvals.grant.savedPermanent", {
          defaultValue:
            "Standing grant {{id}} added, and it never expires. Edit gates.standingGrants to "
            + "take it back.",
          id: grant.id ?? "",
        })}
    </Notice>
  );
}

function Notice({
  children,
  tone,
}: {
  children: ReactNode;
  tone: "warning" | "done" | "quiet";
}) {
  const Icon = tone === "done" ? CheckCircle2 : tone === "quiet" ? ShieldQuestion : AlertTriangle;
  return (
    <div
      className={cn(
        "mt-2 flex items-start gap-2 rounded-md px-3 py-2 text-[12px] leading-5",
        tone === "warning" && "bg-destructive/10 text-destructive-text",
        tone === "done" && "bg-muted/60 text-foreground",
        tone === "quiet" && "bg-muted/40 text-muted-foreground",
      )}
      role={tone === "quiet" ? undefined : "alert"}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">{children}</div>
    </div>
  );
}

/** The sentence an operator reads for one failed answer. */
function refusalSentence(t: TFunction, outcome: GatesApprovalAnswer): string {
  if (outcome.degraded) {
    return t("approvals.noAnswer", {
      defaultValue: "The gateway did not answer. The action still waits.",
    });
  }
  const known = outcome.refusal ? REFUSAL_KEYS[outcome.refusal] : undefined;
  if (known) return t(known.key, { defaultValue: known.fallback });
  return t("approvals.refusalOther", {
    defaultValue: "The executor refused this answer: {{error}}",
    error: outcome.error ?? outcome.refusal ?? "",
  });
}

/**
 * The date a grant stops, as the reader's own locale writes it.
 *
 * An absolute date and not "in 24 hours": a file somebody reads six months later needs a date,
 * not a subtraction. A value that is not a date renders as itself rather than as "Invalid Date".
 */
function expiryLabel(iso: string): string {
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? iso : moment.toLocaleString();
}

/** ``m:ss`` for a remaining time. An operator reads minutes and seconds, and never a float. */
function countdown(remainingMs: number): string {
  const total = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

/** One redraw per second while an action waits. An empty queue needs no timer. */
function useTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}
