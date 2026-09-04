/**
 * The plan, as the thread's own object (#252).
 *
 * A manager turn that delegates shows one card with a row per delegation -- peer, task, outcome,
 * cost -- instead of N tool traces the reader has to reassemble. It sits outside the activity fold
 * on purpose: the fold is *how* the turn worked and tucks itself away when the turn ends, and the
 * plan is *what the manager decided*, which is the part a reader comes back to.
 *
 * ## The presentational decision this file makes, and why
 *
 * The spec left one question open: how a manager's total is shown when it is the sum of its peers'.
 * Answer, in three parts:
 *
 * 1. **The plan's total is arithmetic over its own rows.** Not a separately reported number.
 *    Two numbers that can disagree is how a cost display becomes untrustworthy, and the reader can
 *    check this one by adding up what is on screen.
 * 2. **It is labelled as the peers', and kept out of the manager's own usage line.** A delegated
 *    turn is its own turn (#209), so folding a peer's tokens into the manager's `usage` would print
 *    one turn's cost twice -- once on the answer row and once inside the peer's. The answer row
 *    keeps meaning "what the manager itself spent"; the plan says what the delegations cost.
 * 3. **A row with no reported cost prints none.** Same rule as `cached_tokens` in #208: never show
 *    a figure nobody measured -- in particular not the manager's own per-step usage, which sits on
 *    the same activity row and belongs to the manager's call rather than to the peer's turn. When
 *    the total then covers only part of the plan, it says so (`1 of 2 reported`), because a total
 *    that quietly leaves rows out reads as the whole plan's cost.
 */

import { Bot, CircleSlash, Workflow } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ActivityStep, type ActivityStepTone } from "@/components/thread/activity/ActivityStep";
import type {
  DelegationPlan as DelegationPlanModel,
  DelegationStep,
} from "@/components/thread/activity/delegation-plan-model";
import { formatCompactTokens } from "@/lib/format";
import { cn } from "@/lib/utils";

/** What a row reads as, once the turn's own liveness is taken into account. */
type StepDisplayStatus = "running" | "done" | "error" | "no-answer";

function displayStatus(step: DelegationStep, turnActive: boolean): StepDisplayStatus {
  if (step.status === "error") return "error";
  if (step.status === "done") return "done";
  // A delegation still open on a turn that has ended never reported. It is not an error and it is
  // certainly not done -- calling it done is exactly the misreading rule 3 of the issue forbids.
  return turnActive ? "running" : "no-answer";
}

const TONE_BY_STATUS: Record<StepDisplayStatus, ActivityStepTone> = {
  running: "active",
  done: "success",
  error: "error",
  "no-answer": "neutral",
};

export function DelegationPlan({
  plan,
  turnActive,
}: {
  plan: DelegationPlanModel;
  turnActive: boolean;
}) {
  const { t } = useTranslation();

  const summary: string[] = [];
  if (plan.done > 0) {
    summary.push(t("message.plan.answered", {
      count: plan.done,
      defaultValue: "{{count}} answered",
    }));
  }
  if (plan.failed > 0) {
    summary.push(t("message.plan.failed", {
      count: plan.failed,
      defaultValue: "{{count}} failed",
    }));
  }
  if (plan.running > 0 && turnActive) {
    summary.push(t("message.plan.running", {
      count: plan.running,
      defaultValue: "{{count}} running",
    }));
  }

  const costParts: string[] = [];
  if (plan.cost) {
    costParts.push(t("message.usage.in", {
      tokens: formatCompactTokens(plan.cost.inputTokens),
      defaultValue: "{{tokens}} in",
    }));
    if (plan.cost.cachedTokens !== null && plan.cost.cachedOverInputTokens > 0) {
      costParts.push(t("message.usage.cached", {
        percent: Math.round(
          Math.min(1, plan.cost.cachedTokens / plan.cost.cachedOverInputTokens) * 100,
        ),
        defaultValue: "{{percent}}% cached",
      }));
    }
    costParts.push(t("message.usage.out", {
      tokens: formatCompactTokens(plan.cost.outputTokens),
      defaultValue: "{{tokens}} out",
    }));
    // Said out loud when the total covers only part of the plan, because a total that silently
    // leaves rows out reads as the whole plan's cost.
    if (plan.cost.steps < plan.steps.length) {
      costParts.push(t("message.plan.totalPartial", {
        reported: plan.cost.steps,
        total: plan.steps.length,
        defaultValue: "{{reported}} of {{total}} reported",
      }));
    }
  }

  return (
    <div
      data-testid="delegation-plan"
      data-delegation-count={plan.steps.length}
      className={cn(
        "mt-1.5 w-full max-w-[45rem] rounded-lg border border-border/60 bg-muted/25 px-2.5 py-1.5",
        "animate-in fade-in duration-300 motion-reduce:animate-none",
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="inline-flex min-w-0 items-center gap-1.5 text-[13px] font-medium leading-[18px] text-muted-foreground/85">
          <Workflow className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" aria-hidden />
          {t("message.plan.title", { defaultValue: "Plan" })}
        </span>
        {summary.length ? (
          <span
            data-delegation-summary
            className="text-[11px] leading-none text-muted-foreground/70"
          >
            {summary.join(" · ")}
          </span>
        ) : null}
        {plan.cost ? (
          <span
            data-delegation-total
            title={t("message.plan.totalTitle", {
              defaultValue:
                "The delegations' own cost, summed. A delegated turn is its own turn, so this is not part of the answering agent's own usage.",
            })}
            className="text-[11px] leading-none text-muted-foreground/70 tabular-nums"
          >
            {t("message.plan.total", {
              usage: costParts.join(" · "),
              defaultValue: "delegated {{usage}}",
            })}
          </span>
        ) : null}
      </div>
      <div className="mt-0.5 flex flex-col gap-0.5">
        {plan.steps.map((step) => (
          <DelegationPlanRow key={step.key} step={step} turnActive={turnActive} />
        ))}
      </div>
    </div>
  );
}

function DelegationPlanRow({ step, turnActive }: { step: DelegationStep; turnActive: boolean }) {
  const { t } = useTranslation();
  const status = displayStatus(step, turnActive);
  const agent = step.agent || t("message.plan.unknownAgent", { defaultValue: "a peer" });
  const headline = status === "running"
    ? t("message.plan.step.running", { agent, defaultValue: "Delegating to {{agent}}" })
    : status === "done"
      ? t("message.plan.step.done", { agent, defaultValue: "{{agent}} answered" })
      : status === "error"
        ? t("message.plan.step.failed", { agent, defaultValue: "{{agent}} failed" })
        : t("message.plan.step.noAnswer", { agent, defaultValue: "{{agent}} did not report" });
  const detail = step.error || step.task || step.label || "";
  const cost = step.usage
    ? [
      t("message.usage.in", {
        tokens: formatCompactTokens(step.usage.prompt_tokens),
        defaultValue: "{{tokens}} in",
      }),
      t("message.usage.out", {
        tokens: formatCompactTokens(step.usage.completion_tokens),
        defaultValue: "{{tokens}} out",
      }),
    ].join(" · ")
    : "";

  return (
    <div
      data-testid="delegation-step"
      data-delegation-agent={step.agent || undefined}
      data-delegation-status={status}
      className="flex min-w-0 items-center gap-2"
    >
      {/* The cost is a sibling of the step rather than part of its label: the label truncates,
          and a truncated cost is worse than no cost at all. */}
      <div className="min-w-0 flex-1">
        <ActivityStep
          active={status === "running"}
          tone={TONE_BY_STATUS[status]}
          ariaLabel={[headline, detail, cost].filter(Boolean).join(" · ")}
          marker={(
            <span
              className={cn(
                "grid h-3.5 w-3.5 place-items-center",
                status === "error"
                  ? "text-destructive-text/80"
                  : status === "done"
                    ? "text-emerald-500/78"
                    : "text-muted-foreground/60",
                status === "running" && "animate-pulse motion-reduce:animate-none",
              )}
              aria-hidden
            >
              {status === "no-answer"
                ? <CircleSlash className="h-3 w-3" strokeWidth={2} />
                : <Bot className="h-3.5 w-3.5" strokeWidth={2} />}
            </span>
          )}
          label={[headline, detail].filter(Boolean).join(" · ")}
        />
      </div>
      {cost ? (
        <span
          data-delegation-cost
          className="shrink-0 text-[11px] leading-none tabular-nums text-muted-foreground/60"
        >
          {cost}
        </span>
      ) : null}
    </div>
  );
}
