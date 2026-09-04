/**
 * The small badge beside an agent's name, on the card and on its own page.
 *
 * The reference product puts a status badge here, and a named agent has no status: there is no
 * enabled, paused or draft field on `NamedAgentConfig` (`nanoinfra/config/schema.py`), so a badge
 * reading `Active` on every card would be decoration wearing the clothes of data -- and the first
 * person to trust it would be looking for the switch that turns it off.
 *
 * What the slot holds instead is a fact the roster payload already carries, and the one this whole
 * rework is about: whether this agent's instructions are partly its own. `Custom prompt` means an
 * addendum or a replaced section; `Default prompt` means the platform's text, unmodified. It
 * differs between agents, it is the first thing worth knowing about one, and clicking through to
 * `Prompt` is exactly what it invites.
 */
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export function AgentPromptBadge({
  own,
  testId,
}: {
  /** True when the agent declares an addendum or replaces a section. */
  own: boolean;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        own ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground",
      )}
      data-testid={testId}
      title={own
        ? t("agents.roster.promptCustomHelp", {
          defaultValue:
            "This agent adds to or replaces the platform's own prompt. The Prompt tab shows which sections.",
        })
        : t("agents.roster.promptDefaultHelp", {
          defaultValue: "This agent is told what the platform says, with nothing added or replaced.",
        })}
    >
      {own
        ? t("agents.roster.promptCustom", { defaultValue: "Custom prompt" })
        : t("agents.roster.promptDefault", { defaultValue: "Default prompt" })}
    </span>
  );
}
