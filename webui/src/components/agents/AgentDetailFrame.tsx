/**
 * The chrome every agent page shares: a back arrow, the name, a badge, `model · provider`, an
 * underlined tab strip and one `Save` -- nanoinfraorg/nanoinfra#262, extracted in #265.
 *
 * Two agents are edited through it and they are not the same shape. A named agent is nine config
 * keys under `agents.named[<name>]`; the deployment's own is the agent-shaped fields of
 * `agents.defaults` beside nineteen deployment settings that have panels elsewhere -- so the two
 * write different routes with different rules. Their drafts differ, their
 * write routes differ, and their tab sets differ -- but they are the same *object* to the person
 * looking at them, and two panels drawn twice would drift on the day one of them grew a field.
 *
 * So the frame is shared and the drafts are not. What lives here is everything the two have to
 * agree about:
 *
 * - **`Save` on the frame, never in a tab.** A tabbed editor hides unsaved work by definition, so
 *   the one place that is always on screen is the only honest place for the state of it -- and
 *   `Unsaved changes` sits beside the button rather than inside whichever tab happens to be open.
 * - **Plain tabs with an underline.** The segmented tray this replaced is a control for picking
 *   one of two or three things; six of them in a rounded pill read as a filter over one page
 *   rather than as six pages.
 * - **A refusal is rendered verbatim.** Both routes answer a bad write in the config schema's own
 *   words, and those words name the offending value -- which is the only part of a refusal an
 *   operator can act on.
 * - **A `notice` above the tabs, not inside them.** A gateway that cannot report what a form is
 *   about to overwrite is a fact about the whole page, and a tab is exactly where somebody would
 *   fail to see it.
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Loader2, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface AgentFrameTab {
  key: string;
  label: string;
}

export function AgentDetailFrame({
  name,
  badge,
  subtitle,
  tabs,
  tab,
  onTab,
  dirty,
  saving,
  canSave,
  onSave,
  onClose,
  notice,
  error,
  testId = "agent-detail",
  children,
}: {
  /** Monospace, because an agent's name is a token somebody copies. */
  name: string;
  badge?: ReactNode;
  subtitle: string;
  tabs: readonly AgentFrameTab[];
  tab: string;
  onTab: (key: string) => void;
  dirty: boolean;
  saving: boolean;
  /** False while a save would be refused or would write what the form never read. */
  canSave: boolean;
  onSave: () => void;
  onClose: () => void;
  /** Something true about the whole page -- a gateway that cannot prefill, most often. */
  notice?: ReactNode;
  error?: string | null;
  testId?: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  return (
    <section className="space-y-4" data-testid={testId}>
      <div className="flex flex-wrap items-start justify-between gap-3 px-1">
        <div className="flex min-w-0 items-start gap-1.5">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            aria-label={t("agents.detail.back", { defaultValue: "All agents" })}
            className="h-8 w-8 shrink-0 rounded-full p-0 text-muted-foreground hover:text-foreground"
            data-testid="agent-detail-back"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Button>
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h2 className="truncate font-mono text-[15px] font-semibold tracking-[-0.01em] text-foreground">
                {name}
              </h2>
              {badge}
            </div>
            <p
              className="mt-0.5 text-[11.5px] leading-4 text-muted-foreground"
              data-testid="agent-detail-model-line"
            >
              {subtitle}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {dirty
            ? (
              <span
                className="text-[11.5px] text-muted-foreground"
                data-testid="agent-detail-dirty"
              >
                {t("agents.detail.unsaved", { defaultValue: "Unsaved changes" })}
              </span>
            )
            : null}
          <Button
            type="button"
            onClick={onSave}
            disabled={saving || !canSave || !dirty}
            className="h-8 rounded-full px-4 text-[12px]"
            data-testid="agent-detail-save"
          >
            {saving
              ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              : <Save className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
            {t("agents.editor.save", { defaultValue: "Save" })}
          </Button>
        </div>
      </div>

      {notice}
      {error
        ? (
          <p
            className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
            data-testid="agent-detail-error"
          >
            {error}
          </p>
        )
        : null}

      <div
        className="flex flex-wrap items-center gap-x-5 border-b border-border/50 px-1"
        role="tablist"
        aria-label={t("agents.detail.tabs", { defaultValue: "Agent settings" })}
      >
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            onClick={() => onTab(item.key)}
            className={cn(
              "-mb-px border-b-2 px-0.5 pb-2 pt-1 text-[13px] transition-colors",
              tab === item.key
                ? "border-foreground font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="rounded-[22px] bg-settings-surface px-4 py-4 sm:px-5">{children}</div>
    </section>
  );
}
