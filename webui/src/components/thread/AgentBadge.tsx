import { Bot, ChevronDown, Check } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { NamedAgentSummary } from "@/lib/api";
import { cn } from "@/lib/utils";

interface AgentBadgeProps {
  /** The chosen agent, or `null` for the deployment's default one. */
  agent: string | null;
  agents: NamedAgentSummary[];
  onAgentChange?: (agent: string | null) => void;
  isHero?: boolean;
  disabled?: boolean;
}

/**
 * Which agent answers this conversation (#254).
 *
 * It sits where the model badge sits, and replaces it when the deployment names agents: once the
 * model belongs to the agent, offering a model here would offer a decision that is no longer the
 * turn's to make. A deployment that names no agent never renders this and keeps its model badge.
 *
 * The default agent is a real entry rather than an empty state, because "no agent chosen" and
 * "the deployment default" are the same thing and a menu that hides one of them reads as a
 * missing choice.
 */
export function AgentBadge({
  agent,
  agents,
  onAgentChange,
  isHero = false,
  disabled = false,
}: AgentBadgeProps) {
  const { t } = useTranslation();
  if (!agents.length) return null;

  const defaultLabel = t("thread.composer.agent.default", { defaultValue: "Default agent" });
  const chosen = agent ? agents.find((candidate) => candidate.name === agent) : undefined;
  // A name that is no longer configured still reads as itself rather than silently becoming the
  // default: the turn will fall back server-side, and a label that lied about it would hide that.
  const label = agent ? chosen?.name ?? agent : defaultLabel;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled || !onAgentChange}>
        <Button
          type="button"
          variant="ghost"
          data-thread-agent={agent ?? ""}
          aria-label={t("thread.composer.agent.ariaLabel", {
            defaultValue: "Agent: {{agent}}",
            agent: label,
          })}
          title={chosen?.description || undefined}
          className={cn(
            "thread-composer-agent touch-target min-w-0 max-w-[min(12.5rem,42vw)] whitespace-nowrap rounded-[10px] border border-transparent font-semibold shadow-none",
            "bg-transparent text-muted-foreground hover:bg-foreground/[0.045] hover:text-foreground dark:hover:bg-white/[0.06]",
            isHero ? "h-8 px-2.5 text-[12px]" : "h-9 px-3 text-[12.5px]",
          )}
        >
          <Bot className="mr-1.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span aria-hidden className="min-w-0 truncate">{label}</span>
          <ChevronDown className="ml-1.5 h-3 w-3 shrink-0" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <AgentMenuItem
          label={defaultLabel}
          description={t("thread.composer.agent.defaultHint", {
            defaultValue: "The deployment's own prompt, model and tools",
          })}
          selected={agent === null}
          onSelect={() => onAgentChange?.(null)}
        />
        {agents.map((candidate) => (
          <AgentMenuItem
            key={candidate.name}
            label={candidate.name}
            description={candidate.description}
            selected={candidate.name === agent}
            onSelect={() => onAgentChange?.(candidate.name)}
          />
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AgentMenuItem({
  label,
  description,
  selected,
  onSelect,
}: {
  label: string;
  description?: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <DropdownMenuItem
      onSelect={onSelect}
      aria-checked={selected}
      role="menuitemradio"
      className="items-start gap-2"
    >
      <Bot className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium">{label}</span>
        {description ? (
          <span className="truncate text-[12px] text-muted-foreground">{description}</span>
        ) : null}
      </span>
      {selected ? <Check className="ml-auto mt-0.5 h-4 w-4 shrink-0" aria-hidden /> : null}
    </DropdownMenuItem>
  );
}
