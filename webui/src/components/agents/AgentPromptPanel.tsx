/**
 * The `Prompt` tab: what one agent is told, **as text**, editable where the text is this
 * deployment's to write -- nanoinfraorg/nanoinfra#256 for the record, #262 for the write path, and
 * this rework for the sentence that showed both of them had missed the point: *"PROMPT era para
 * editar el PROMPT, no sé qué carajo estoy viendo ahí."*
 *
 * What was there was an inventory: thirteen rows of section *names*, a permission badge on each, a
 * token figure in the column where a value goes, and a button reading `Replace`. Every one of
 * those is a fact about the prompt and none of them is the prompt. So:
 *
 * - **The text is on screen, not behind a click.** A section that has text renders it. You cannot
 *   decide whether to rewrite a paragraph you have not been shown, and `Replace` asked exactly
 *   that.
 * - **A pencil, not a button reading `Replace`.** The pencil turns the same text into a textarea in
 *   place, seeded with what is in force. There is no separate "write your version" field to
 *   reconcile with the version above it.
 * - **The warning is in the editor, never a refusal.** `Safety notes` and `Tool usage notes` used
 *   to be `fixed` here on the reasoning that an agent which no longer knows the rules retries a
 *   refused action instead of explaining it. That reasoning is true and it is now a sentence the
 *   operator reads while editing (`REPLACEMENT_WARNINGS` in `nanoinfra/agent/prompt_sections.py`),
 *   because the person editing a deployment's prompt owns that deployment's behaviour.
 * - **No size where a value goes.** `size varies per turn` sat next to the `Replace` button, in the
 *   slot a value occupies, reading as something you could change. A row that carries an editor
 *   carries no figure at all now, and a section a turn assembles says so in a sentence instead.
 * - **The addendum is not here.** It moved to `Basic`, where the reference product puts it, and
 *   this tab lists it -- with its text -- and points at the tab that edits it. Two boxes for one
 *   string is how a tabbed editor loses an edit.
 *
 * **Why both controls exist, which is also why this tab does.** An addendum can only *add*. A
 * sentence in the platform's own text that an operator disagrees with cannot be undone by
 * appending a correction to it: the model is handed both and they contradict, and which one it
 * follows is a coin toss nobody configured. Replacing the section is the only way to *remove*
 * something the platform says. So each control says which of the two it is -- the addendum's field
 * on `Basic`, and the pencil here.
 *
 * Two rules survive from #256 and are the reason this is a list of *every* section rather than of
 * the editable ones: a replaced section is still named and still marked, because a record that hid
 * a replacement would make two different prompts look identical; and a section's permission is
 * shown whether or not it has an editor, because the permission is a fact about the platform.
 *
 * This panel **holds no save of its own**. It is one tab of an agent saved whole from the frame
 * around the tabs, so every edit here writes the one draft.
 *
 * **What counts as a replacement is a comparison, not an emptiness test.** The box arrives holding
 * the platform's own text, so a rule that stored whatever was in it would fork the platform's copy
 * the first time anybody opened a section to read it -- and from the next upgrade onwards the agent
 * would be running last release's safety notes with nothing on screen saying so. Text equal to
 * `platform_text` removes the key; so does an empty box, because `resolve_overrides` in
 * `nanoinfra/agent/prompt_sections.py` drops an override that strips to nothing. An absent key is
 * how config spells *leave this alone*, which means an empty section is not a thing this product
 * can store -- and the editor says so rather than offering it.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Pencil, RotateCcw, TriangleAlert } from "lucide-react";

import { withPromptReplacement, withPromptSection } from "@/components/agents/agentValues";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { fetchAgentPrompt } from "@/lib/api";
import type { AgentPromptPayload, AgentPromptPermission, AgentPromptSection } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * The least a draft has to be for this panel to edit it.
 *
 * Structural rather than `NamedAgentValues`, because `agents.defaults` gained `addendum` and
 * `promptSections` in #265 and the deployment's own agent is edited by this same panel. Every rule
 * here -- the comparison against `platform_text`, the absent key, the badge following the draft --
 * is the same rule for both, and a second copy of the panel would be a second copy to keep true.
 */
interface PromptDraft {
  addendum: string;
  promptSections: Record<string, string>;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; payload: AgentPromptPayload }
  // The route may not be reachable on an older gateway, and an agent being created has no prompt
  // to report yet. Both are "nothing to show here", not an error banner over a working page.
  | { kind: "unavailable" }
  | { kind: "unsaved" };

/**
 * The draft this panel edits, and the setter that owns it.
 *
 * Optional as a pair, and the reason is the write path rather than a preference: an agent is saved
 * whole, from the frame around the tabs. Without a draft to write into there is nothing this panel
 * could save, so it reads -- which is exactly what it did before a write path existed.
 */
interface PromptDraftProps<T extends PromptDraft = PromptDraft> {
  values?: T;
  onChange?: (next: T) => void;
  /**
   * True while the draft differs from what is saved -- which is when a token figure measured by
   * the gateway describes text that is no longer on screen.
   */
  dirty?: boolean;
}

export function AgentPromptPanel<T extends PromptDraft>({
  agent,
  token,
  base = "",
  values,
  onChange,
  dirty = false,
}: {
  /**
   * The agent whose composition to read: a name, `null` for the deployment's own agent, or `""`
   * for an agent that does not exist yet and therefore has no prompt to report.
   *
   * The three are genuinely different answers and collapsing any two of them produces a wrong
   * sentence on screen: `agents.defaults` always exists, so telling its operator that "the
   * sections appear once this agent is saved" would be nonsense.
   */
  agent: string | null;
  token: string;
  base?: string;
} & PromptDraftProps<T>) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    if (agent === "") {
      setState({ kind: "unsaved" });
      return;
    }
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
    return (
      <AgentPromptSections
        payload={state.payload}
        values={values}
        onChange={onChange}
        dirty={dirty}
      />
    );
  }
  // No addendum field in the fallback any more: the addendum is on `Basic`, which does not depend
  // on this route and is reachable whether or not the gateway reports the composition.
  return <AgentPromptPlaceholder kind={state.kind} />;
}

function AgentPromptPlaceholder({ kind }: { kind: "loading" | "unavailable" | "unsaved" }) {
  const { t } = useTranslation();
  const text = kind === "loading"
    ? t("agents.prompt.loading", { defaultValue: "Reading the prompt composition…" })
    : kind === "unsaved"
    ? t("agents.prompt.unsaved", {
      defaultValue: "The prompt sections appear once this agent is saved.",
    })
    : t("agents.prompt.unavailable", {
      defaultValue: "This gateway does not report the prompt composition.",
    });
  return (
    <p
      className="px-1 py-3 text-[12px] leading-5 text-muted-foreground"
      data-testid={`agent-prompt-${kind}`}
    >
      {text}
    </p>
  );
}

/**
 * The text in force for one section, read from the draft first.
 *
 * The draft rather than the payload, because the payload describes the last save: a section the
 * operator just cleared would otherwise keep showing the replacement the next save is about to
 * remove. `null` is honest and means *a turn assembles this* -- the memory block, the bootstrap
 * files, the history -- which is a different thing from a section whose text is empty.
 */
function sectionText(
  section: AgentPromptSection,
  values?: PromptDraft,
): string | null {
  if (section.permission === "append_only") {
    return values ? values.addendum : section.text ?? null;
  }
  const draft = values?.promptSections[section.name];
  if (draft !== undefined) return draft;
  // The platform's own, deliberately, when the draft holds no replacement -- `text` is
  // override-or-platform as of the last save, and showing it would contradict a cleared draft.
  // An older gateway sends no `platform_text` at all, and there `text` is all there is.
  if (section.platform_text !== undefined) return section.platform_text;
  return section.text ?? null;
}

/**
 * The panel with the payload already in hand.
 *
 * Separated from the fetch so every rule it renders -- the text on screen, a pencil only where the
 * permission allows one, a warning in the editor rather than a refusal, no figure on a row that
 * carries an editor -- can be asserted without a network.
 */
export function AgentPromptSections<T extends PromptDraft>({
  payload,
  values,
  onChange,
  dirty = false,
}: { payload: AgentPromptPayload } & PromptDraftProps<T>) {
  const { t } = useTranslation();
  /**
   * Which section is open in its editor, and the text the box is holding.
   *
   * The text is kept here as well as in the draft for one reason: emptying the box has to leave it
   * empty. An empty replacement is stored as *no key*, so a box rendered from the draft alone
   * would refill itself with the platform's text the moment you selected all and deleted -- which
   * reads as the field fighting you rather than as config's own spelling of *leave this alone*.
   */
  const [editing, setEditing] = useState<{ section: string; text: string } | null>(null);
  const editable = Boolean(values && onChange);

  return (
    <div className="space-y-4" data-testid="agent-prompt-sections">
      <div className="space-y-1 px-1">
        <h3 className="text-[12px] font-semibold text-foreground/85">
          {t("agents.prompt.introTitle", { defaultValue: "What this agent is told" })}
        </h3>
        <p className="text-[12px] leading-5 text-muted-foreground">
          {t("agents.prompt.introHelp", {
            defaultValue:
              "Every section of the system prompt, in the order the platform assembles it. The pencil opens a section's text for editing, and editing is the only way to remove something the platform says -- an addendum can add to it but never take it back. The sections without a pencil are built from your config or from the workspace, so they change there.",
          })}
        </p>
      </div>
      <ul className="overflow-hidden rounded-[18px] bg-background/55">
        {payload.sections.map((section) => (
          <PromptSectionRow
            key={section.name}
            section={section}
            values={values}
            onChange={onChange}
            editable={editable}
            dirty={dirty}
            editing={editing?.section === section.name ? editing.text : null}
            onEdit={(text) => setEditing({ section: section.name, text })}
            onClose={() => setEditing(null)}
          />
        ))}
      </ul>
      <p className="px-1 text-[11.5px] leading-5 text-muted-foreground">
        {t("agents.prompt.estimateNote", {
          defaultValue:
            "Token figures are estimates for the sections that cost the same on every turn. What one turn actually carried is on the turn itself.",
        })}
      </p>
    </div>
  );
}

function PromptSectionRow<T extends PromptDraft>({
  section,
  values,
  onChange,
  editable,
  dirty,
  editing,
  onEdit,
  onClose,
}: {
  section: AgentPromptSection;
  editable: boolean;
  /** The text the open box holds, or `null` when this row's editor is shut. */
  editing: string | null;
  onEdit: (text: string) => void;
  onClose: () => void;
} & PromptDraftProps<T>) {
  const { t } = useTranslation();
  const addendum = section.permission === "append_only";
  /*
   * The permission decides the pencil, and nothing else does. `Memory` is replaceable and has no
   * platform text to show -- it is assembled from the agent's own files -- so it gets an editor
   * with a sentence in place of a default rather than losing the ability #262 shipped.
   */
  const editorOffered = editable && section.permission === "replaceable";
  const text = sectionText(section, values);
  // The draft's own state, not the payload's: the badge has to follow the edit rather than the
  // last save, or a replacement would look absent until the page reloaded.
  const replaced = values ? section.name in values.promptSections : section.overridden;
  // Recomputed from the draft for the addendum: the payload calls the section absent when the
  // *saved* addendum is empty, and a row reading "not present" above a paragraph is the panel
  // disagreeing with itself.
  const present = addendum && values ? Boolean(values.addendum.trim()) : section.present;
  /*
   * No figure on a row that carries an editor. `size varies per turn` sat in the slot a value
   * occupies, next to a button reading `Replace`, and read as something an operator could set. A
   * section a turn assembles says so in its body instead, in a sentence.
   *
   * The addendum's figure is dropped rather than recomputed while an unsaved edit could change it:
   * a count is the gateway's measurement of the saved text, and a browser-side recount would be a
   * second, differently-wrong number.
   */
  const size = editorOffered
    ? ""
    : addendum && dirty && values
    ? t("agents.prompt.tokensPending", { defaultValue: "counted after saving" })
    : section.tokens === null
    ? ""
    : t("agents.prompt.tokens", {
      tokens: section.tokens.toLocaleString(),
      defaultValue: "{{tokens}} tokens",
    });

  return (
    <li
      className="border-b border-border/40 last:border-b-0"
      data-testid={`agent-prompt-section-${section.name}`}
    >
      <div className="flex flex-col gap-1.5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span
            className={cn(
              "text-[13px] font-medium text-foreground",
              // Dimmed, never hidden: the permission is a fact about the platform, and a section
              // that happens to be empty for this agent still has one.
              !present && "text-muted-foreground",
            )}
          >
            {section.name}
          </span>
          <PermissionBadge permission={section.permission} />
          {replaced
            ? (
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
            )
            : null}
        </div>
        <span className="flex shrink-0 items-center gap-2">
          {size
            ? (
              <span
                className="text-[12px] tabular-nums text-muted-foreground"
                data-testid={`agent-prompt-size-${section.name}`}
              >
                {size}
              </span>
            )
            : null}
          {/*
            * An icon, because the maintainer asked for one and because the alternative was a
            * button labelled `Replace` sitting where a value goes. No control at all for a fixed,
            * derived or workspace section: the badge's tooltip says why, and an editor whose every
            * save comes back refused is a control that exists only to fail.
            */}
          {editorOffered
            ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => (editing === null ? onEdit(text ?? "") : onClose())}
                aria-expanded={editing !== null}
                aria-label={t("agents.prompt.editSection", {
                  section: section.name,
                  defaultValue: "Edit {{section}}",
                })}
                className="h-7 w-7 rounded-full p-0 text-muted-foreground hover:text-foreground"
                data-testid={`agent-prompt-edit-${section.name}`}
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )
            : null}
        </span>
      </div>

      {editing !== null && values && onChange
        ? (
          <SectionEditor
            section={section}
            text={editing}
            replaced={replaced}
            onChange={(next) => {
              onEdit(next);
              // A comparison, not an emptiness test: the box arrives holding the platform's own
              // text, so storing whatever is in it would fork the platform's copy the first time
              // somebody opened a section to read it.
              onChange(withPromptReplacement(values, section.name, next, section.platform_text));
            }}
            onRestore={() => {
              // Back to the platform's text in the box, and the key gone from config.
              onEdit(section.platform_text ?? "");
              onChange(withPromptSection(values, section.name, ""));
            }}
          />
        )
        : (
          <SectionBody
            section={section}
            text={text}
            addendumEditedElsewhere={addendum && editable}
          />
        )}
    </li>
  );
}

/**
 * A section's text, read-only, and the honest sentence when there is no text to read.
 *
 * "Every section that has text renders it" is the whole rework in one line: the tab used to be a
 * list of names, and a name does not tell you whether to rewrite the paragraph behind it.
 */
function SectionBody({
  section,
  text,
  addendumEditedElsewhere,
}: {
  section: AgentPromptSection;
  text: string | null;
  /** True for the addendum row while `Basic` owns the field, so this row points at it. */
  addendumEditedElsewhere: boolean;
}) {
  const { t } = useTranslation();
  if (text === null) {
    return (
      <p
        className="px-4 pb-3 text-[12px] leading-5 text-muted-foreground"
        data-testid={`agent-prompt-per-turn-${section.name}`}
      >
        {t("agents.prompt.perTurnBody", {
          defaultValue:
            "The platform assembles this section on every turn, from the workspace and the session, so there is no fixed text to show here.",
        })}
      </p>
    );
  }
  if (!text.trim()) {
    return (
      <p
        className="px-4 pb-3 text-[12px] leading-5 text-muted-foreground"
        data-testid={section.permission === "append_only"
          ? "agent-prompt-addendum-empty"
          : `agent-prompt-empty-${section.name}`}
      >
        {section.permission === "append_only"
          ? t("agents.prompt.addendumEmpty", { defaultValue: "This agent declares no addendum." })
          : t("agents.prompt.sectionEmpty", {
            defaultValue: "This section carries nothing for this agent.",
          })}
        {addendumEditedElsewhere ? ` ${t("agents.prompt.addendumElsewhere", {
          defaultValue: "The addendum is edited on the Basic tab.",
        })}` : ""}
      </p>
    );
  }
  return (
    <div className="space-y-1.5 px-4 pb-3">
      <pre
        className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-[12px] bg-muted/45 px-3 py-2.5 font-mono text-[11.5px] leading-5 text-foreground/90"
        data-testid={`agent-prompt-text-${section.name}`}
      >
        {text}
      </pre>
      {addendumEditedElsewhere
        ? (
          <p className="text-[11.5px] leading-4 text-muted-foreground/80">
            {t("agents.prompt.addendumElsewhere", {
              defaultValue: "The addendum is edited on the Basic tab.",
            })}
          </p>
        )
        : null}
    </div>
  );
}

/**
 * One replaceable section's text, open for editing in place.
 *
 * Seeded with what is in force -- the deployment's replacement if there is one, otherwise the
 * platform's own -- so editing starts from the default instead of from a blank box that quietly
 * deletes a paragraph nobody had read.
 *
 * The warning is here rather than at the gate: `Safety notes` and `Tool usage notes` are
 * replaceable now, and what they cost is a sentence the operator reads while typing. The
 * placeholders are here for the same reason -- `Runtime` carries the paths to the agent's own
 * memory as `{{ }}` names, and a replacement that drops them leaves the model without them.
 */
function SectionEditor({
  section,
  text,
  replaced,
  onChange,
  onRestore,
}: {
  section: AgentPromptSection;
  text: string;
  replaced: boolean;
  onChange: (text: string) => void;
  onRestore: () => void;
}) {
  const { t } = useTranslation();
  const placeholders = section.placeholders ?? [];
  return (
    <div className="space-y-2.5 border-t border-border/40 px-4 py-3">
      {section.warning
        ? (
          <p
            className="flex gap-2 rounded-[12px] bg-muted/60 px-3 py-2 text-[11.5px] leading-5 text-foreground/85"
            data-testid={`agent-prompt-warning-${section.name}`}
          >
            <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <span>{section.warning}</span>
          </p>
        )
        : null}
      {placeholders.length > 0
        ? (
          <div
            className="flex flex-wrap items-center gap-1.5"
            data-testid={`agent-prompt-placeholders-${section.name}`}
          >
            <span className="text-[11.5px] text-muted-foreground">
              {t("agents.prompt.placeholders", {
                defaultValue: "Keep these placeholders in your text:",
              })}
            </span>
            {placeholders.map((name) => (
              <code
                key={name}
                className="rounded-[6px] bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground/85"
              >
                {`{{ ${name} }}`}
              </code>
            ))}
          </div>
        )
        : null}
      {/*
        * No placeholder. The box arrives full -- that is the whole point of this rework -- so a
        * placeholder would only ever be read after somebody emptied it, and the sentence it used
        * to carry ("leave empty to keep the platform's text") described a control that no longer
        * exists: empty is now one way of asking for the default, not the way.
        */}
      <Textarea
        value={text}
        rows={12}
        autoFocus
        aria-label={t("agents.prompt.sectionEditorLabel", {
          section: section.name,
          defaultValue: "Your text for {{section}}",
        })}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-[12px] font-mono text-[11.5px] leading-5"
        data-testid={`agent-prompt-editor-${section.name}`}
      />
      {/*
        * Two different empty boxes, and saying which is which is the whole of this rework in
        * miniature. A section with platform text opens holding it, so an *emptied* box is a
        * question about what emptying means. A section the platform assembles per turn -- `Memory`
        * is the one -- has no constant to prefill with at all, and a blank box with nothing said
        * about it is precisely what the maintainer was looking at when he asked what he was
        * seeing.
        */}
      {section.platform_text == null
        ? (
          <p
            className="text-[11.5px] leading-4 text-muted-foreground"
            data-testid={`agent-prompt-no-default-${section.name}`}
          >
            {t("agents.prompt.noDefaultText", {
              defaultValue:
                "The platform assembles this section on every turn, so there is no default text to start from. What you write here stands in its place.",
            })}
          </p>
        )
        : !text.trim()
        ? (
          // Config cannot hold an empty section, so an emptied box saves as the platform's text --
          // and an operator who meant to delete the section should learn that here rather than
          // from a diff of the config file.
          <p
            className="text-[11.5px] leading-4 text-muted-foreground"
            data-testid={`agent-prompt-empty-note-${section.name}`}
          >
            {t("agents.prompt.emptyIsDefault", {
              defaultValue:
                "An empty section is not something config can store, so saving this restores the platform's own text.",
            })}
          </p>
        )
        : null}
      {replaced
        ? (
          <div className="flex justify-end">
            {/*
              * Removes the key rather than storing `""`. Both readings load the platform's text,
              * but only one of them says so in the file.
              */}
            <Button
              type="button"
              variant="ghost"
              onClick={onRestore}
              className="h-8 rounded-full px-3 text-[12px] text-muted-foreground hover:text-foreground"
              data-testid={`agent-prompt-restore-${section.name}`}
            >
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {t("agents.prompt.restoreDefault", { defaultValue: "Restore default" })}
            </Button>
          </div>
        )
        : null}
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
