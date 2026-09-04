/**
 * What one agent is told, section by section, with the permission on each -- nanoinfraorg/nanoinfra#256.
 *
 * The alternative was one textarea holding the whole system prompt, and it is worth recording why
 * that is wrong: an operator can delete the tool contract and the safety notes, after which the
 * gate still refuses the action but the model no longer knows the rules it is supposed to be
 * following. The refusal stops being explicable. "Addendum only" fails the other way -- replacing
 * a persona is a real need and that section is exactly the right place for it.
 *
 * So each section carries a permission, decided server-side in `nanoinfra/agent/prompt_sections.py`
 * and rendered here. This panel is a **read**: an agent is authority, and authority is edited in
 * the config file a human reviews, not through a settings write.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchAgentPrompt } from "@/lib/api";
import type { AgentPromptPayload, AgentPromptPermission } from "@/lib/api";
import { cn } from "@/lib/utils";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; payload: AgentPromptPayload }
  // The route may not be reachable on an older gateway. That is "nothing to show here", not an
  // error banner over a page whose other half works.
  | { kind: "unavailable" };

export function AgentPromptPanel({
  agent,
  token,
  base = "",
}: {
  agent: string;
  token: string;
  base?: string;
}) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    if (!token) {
      setState({ kind: "unavailable" });
      return;
    }
    let cancelled = false;
    setState({ kind: "loading" });
    void (async () => {
      try {
        const payload = await fetchAgentPrompt(agent, token, base);
        if (!cancelled) setState({ kind: "ready", payload });
      } catch {
        if (!cancelled) setState({ kind: "unavailable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agent, token, base]);

  if (state.kind === "ready") {
    return <AgentPromptSections payload={state.payload} />;
  }
  return <AgentPromptPlaceholder kind={state.kind} />;
}

function AgentPromptPlaceholder({ kind }: { kind: "loading" | "unavailable" }) {
  const { t } = useTranslation();
  return (
    <p
      className="px-1 py-3 text-[12px] leading-5 text-muted-foreground"
      data-testid={`agent-prompt-${kind}`}
    >
      {kind === "loading"
        ? t("agents.prompt.loading", { defaultValue: "Reading the prompt composition…" })
        : t("agents.prompt.unavailable", {
          defaultValue: "This gateway does not report the prompt composition.",
        })}
    </p>
  );
}

/**
 * The panel with the payload already in hand.
 *
 * Separated from the fetch so the rules it renders -- a permission on every section, a replaced
 * section still named and marked -- can be tested without a network at all.
 */
export function AgentPromptSections({ payload }: { payload: AgentPromptPayload }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4" data-testid="agent-prompt-sections">
      <ul className="overflow-hidden rounded-[18px] bg-settings-surface">
        {payload.sections.map((section) => (
          <li
            key={section.name}
            className="flex flex-col gap-1.5 border-b border-border/40 px-4 py-3 last:border-b-0 sm:flex-row sm:items-center sm:justify-between"
            data-testid={`agent-prompt-section-${section.name}`}
          >
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span
                className={cn(
                  "text-[13px] font-medium text-foreground",
                  // Dimmed, never hidden: the permission is a fact about the platform, and a
                  // section that happens to be empty for this agent still has one.
                  !section.present && "text-muted-foreground",
                )}
              >
                {section.name}
              </span>
              <PermissionBadge permission={section.permission} />
              {section.overridden ? (
                <span
                  className="rounded-full bg-primary/15 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-primary"
                  data-testid={`agent-prompt-replaced-${section.name}`}
                  title={t("agents.prompt.replacedHelp", {
                    defaultValue:
                      "This deployment replaced the platform's text for this section. The section is still named here, because a record that hid the replacement would make two different prompts look identical.",
                  })}
                >
                  {t("agents.prompt.replaced", { defaultValue: "Replaced" })}
                </span>
              ) : null}
            </div>
            <span className="shrink-0 text-[12px] tabular-nums text-muted-foreground">
              {section.tokens === null
                ? t("agents.prompt.perTurn", { defaultValue: "size varies per turn" })
                : t("agents.prompt.tokens", {
                  tokens: section.tokens.toLocaleString(),
                  defaultValue: "{{tokens}} tokens",
                })}
            </span>
          </li>
        ))}
      </ul>

      <div className="space-y-2">
        <h3 className="px-1 text-[12px] font-semibold text-foreground/85">
          {t("agents.prompt.addendumTitle", { defaultValue: "Addendum" })}
        </h3>
        <p className="px-1 text-[12px] leading-5 text-muted-foreground">
          {t("agents.prompt.addendumHelp", {
            defaultValue:
              "The addendum is appended after the sections above and can replace none of them. Edit it in the config file, where an agent is declared.",
          })}
        </p>
        {payload.addendum.trim()
          ? (
            <pre
              className="max-h-64 overflow-auto whitespace-pre-wrap rounded-[18px] bg-settings-surface px-4 py-3 text-[12px] leading-5 text-foreground"
              data-testid="agent-prompt-addendum"
            >
              {payload.addendum}
            </pre>
          )
          : (
            <p
              className="px-1 text-[12px] leading-5 text-muted-foreground"
              data-testid="agent-prompt-addendum-empty"
            >
              {t("agents.prompt.addendumEmpty", {
                defaultValue: "This agent declares no addendum.",
              })}
            </p>
          )}
      </div>

      <p className="px-1 text-[11.5px] leading-5 text-muted-foreground">
        {t("agents.prompt.estimateNote", {
          defaultValue:
            "Token figures are estimates for the sections that cost the same on every turn. What one turn actually carried is on the turn itself.",
        })}
      </p>
    </div>
  );
}

/**
 * One section's permission, as a badge with the reasoning in its tooltip.
 *
 * The labels are decided here rather than server-side: the server states the *rule*, and a rule
 * has to read the same in eight languages. A permission this build has no label for falls back to
 * its own value, because showing `append_only` beats showing an empty badge.
 */
function PermissionBadge({ permission }: { permission: AgentPromptPermission }) {
  const { t } = useTranslation();
  const labels: Record<AgentPromptPermission, string> = {
    replaceable: t("agents.prompt.permission.replaceable", { defaultValue: "Yours" }),
    workspace: t("agents.prompt.permission.workspace", { defaultValue: "Workspace" }),
    derived: t("agents.prompt.permission.derived", { defaultValue: "From config" }),
    append_only: t("agents.prompt.permission.appendOnly", { defaultValue: "Appended" }),
    fixed: t("agents.prompt.permission.fixed", { defaultValue: "Fixed" }),
  };
  const help: Record<AgentPromptPermission, string> = {
    replaceable: t("agents.prompt.permissionHelp.replaceable", {
      defaultValue: "This section is the deployment's own text to write.",
    }),
    workspace: t("agents.prompt.permissionHelp.workspace", {
      defaultValue: "Already yours by another route: the workspace's own instruction files.",
    }),
    derived: t("agents.prompt.permissionHelp.derived", {
      defaultValue: "Computed from config, so config is where it changes.",
    }),
    append_only: t("agents.prompt.permissionHelp.appendOnly", {
      defaultValue: "Added after the platform's sections; it can replace none of them.",
    }),
    fixed: t("agents.prompt.permissionHelp.fixed", {
      defaultValue:
        "The tool contract and the safety notes. Deleting them would leave the gate refusing actions the model no longer knows the rules for.",
    }),
  };
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide",
        permission === "fixed"
          ? "bg-muted text-muted-foreground"
          : "bg-secondary text-secondary-foreground",
      )}
      data-testid={`agent-prompt-permission-${permission}`}
      title={help[permission] ?? permission}
    >
      {labels[permission] ?? permission}
    </span>
  );
}
