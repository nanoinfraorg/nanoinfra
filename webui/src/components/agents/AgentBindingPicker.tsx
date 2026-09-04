/**
 * One of an agent's binding lists, picked from this deployment's own vocabulary -- #262, made
 * three-state in #266.
 *
 * **There is no way to type a name here, and that is the whole point.** `toolGroups`, `skills`,
 * `connectors`, `mcpServers` and `delegates` are all names of things that already exist in this
 * deployment. A typed name either fails at save or -- for the lists config does not
 * cross-validate -- succeeds and binds nothing: a config value nothing reads, which is worse than
 * no panel at all, because it looks like it worked. So when a list has nothing to offer, this says
 * so and points at where that kind of thing is made, rather than offering a text field.
 *
 * **Two controls, because the list has three states and pills can only express two.**
 *
 * A row of pills has one degree of freedom: which ones are lit. Nothing lit therefore had to mean
 * something, and what it meant was *everything* -- so an agent could not be told to load no MCP
 * server, and a deployment paying for twelve of them in every first message had no control that
 * said what it wanted to say. Worse, the two readings are one keystroke apart: unpicking the last
 * pill would have silently changed a ceiling into its opposite.
 *
 * So the mode is its own choice -- `Everything` or `Only these` -- and the pills appear under the
 * second. `Everything` is `null` in config: nothing declared. `Only these` with nothing picked is
 * `[]`: declared, and empty, which is a real and useful agent. The panel says which one it is in
 * words, because this is the distinction the whole change is about and a control that made an
 * operator infer it would be repeating the mistake one level up.
 *
 * The pills themselves are the control the automation editor's "Skills to load" already uses, down
 * to the `aria-pressed` button: two multi-selects that look different in one product is a cost
 * with nothing on the other side of it.
 */
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function AgentBindingPicker({
  label,
  help,
  options,
  selected,
  onChange,
  emptyHint,
  emptyAction,
  everythingLabel,
  onlyTheseLabel,
  noneWarning,
  alwaysDeclared = false,
  testId,
}: {
  label: string;
  help?: string;
  /** What this deployment offers. Each entry's `name` is what config stores. */
  options: Array<{ name: string; description?: string }>;
  /** `null` is *nothing declared*; a list -- including an empty one -- is a declared ceiling. */
  selected: string[] | null;
  onChange: (next: string[] | null) => void;
  /** Shown instead of the pills when the deployment has none of these. */
  emptyHint: string;
  /** Where this kind of thing is made, when the answer is another settings panel. */
  emptyAction?: { label: string; onClick: () => void };
  /** What "declare nothing" means for *this* list, in this list's own words. */
  everythingLabel?: string;
  onlyTheseLabel?: string;
  /** What an empty declared list costs for this list, said before it is saved. */
  noneWarning?: string;
  /**
   * True for a list with no third state -- `delegates`, where membership *is* the grant.
   *
   * There the mode row would be two buttons for one meaning: "everything" is not a thing a
   * delegate list can say, because an agent that may ask every peer is an agent that was told
   * to. So the pills stand alone and an empty list is simply none.
   */
  alwaysDeclared?: boolean;
  testId: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const declared = alwaysDeclared || selected !== null;
  const picked = selected ?? [];
  const known = new Set(options.map((option) => option.name));
  // A selected name the catalogue does not offer is still shown, and first: it is in config now,
  // and a picker that hid it would drop it on the next save without saying so.
  const rows: Array<{ name: string; description?: string }> = [
    ...picked.filter((name) => !known.has(name)).map((name) => ({ name })),
    ...options,
  ];

  const toggle = (name: string) => {
    onChange(picked.includes(name) ? picked.filter((each) => each !== name) : [...picked, name]);
  };

  return (
    <div className="space-y-1.5" data-testid={testId}>
      <span className="text-[12px] font-medium text-muted-foreground">{label}</span>
      {help ? <p className="text-[11.5px] leading-4 text-muted-foreground/80">{help}</p> : null}

      {/*
        * The mode. Two buttons rather than a checkbox, so neither state is the unlabelled one --
        * "not everything" is not a sentence an operator should have to complete themselves.
        */}
      <div
        className={cn("flex flex-wrap gap-1.5 pt-1", alwaysDeclared && "hidden")}
        data-testid={`${testId}-mode`}
        hidden={alwaysDeclared}
      >
        <ModeButton
          pressed={!declared}
          onClick={() => onChange(null)}
          testId={`${testId}-mode-all`}
        >
          {everythingLabel ?? tx("agents.editor.bindingAll", "Everything")}
        </ModeButton>
        <ModeButton
          pressed={declared}
          onClick={() => onChange(picked)}
          testId={`${testId}-mode-only`}
        >
          {onlyTheseLabel ?? tx("agents.editor.bindingOnly", "Only these")}
        </ModeButton>
      </div>

      {!declared
        ? (
          <p
            className="text-[11.5px] leading-4 text-muted-foreground/70"
            data-testid={`${testId}-all-note`}
          >
            {tx(
              "agents.editor.bindingAllNote",
              "Nothing declared: this agent reaches every one this deployment has, and gains any you add later.",
            )}
          </p>
        )
        : rows.length === 0
        ? (
          <div className="flex flex-wrap items-center gap-2 pt-1" data-testid={`${testId}-empty`}>
            <p className="text-[11.5px] leading-4 text-muted-foreground/70">{emptyHint}</p>
            {emptyAction
              ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={emptyAction.onClick}
                  className="h-7 rounded-full px-3 text-[11.5px]"
                  data-testid={`${testId}-empty-action`}
                >
                  {emptyAction.label}
                </Button>
              )
              : null}
          </div>
        )
        : (
          <>
            <div className="flex flex-wrap gap-1.5 pt-1">
              {rows.map((row) => {
                const on = picked.includes(row.name);
                return (
                  <button
                    key={row.name}
                    type="button"
                    aria-pressed={on}
                    title={row.description}
                    onClick={() => toggle(row.name)}
                    className={cn(
                      "rounded-full px-2.5 py-1 text-[11.5px] leading-4 transition-colors",
                      on
                        ? "bg-primary/15 text-foreground"
                        : "bg-background/70 text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {row.name}
                  </button>
                );
              })}
            </div>
            {/*
              * Said while it is still a draft. An empty declared list is a legitimate agent and
              * this is not a refusal -- but it is the one state whose config file reads as an
              * accident, so the panel names it rather than letting a save be the explanation.
              */}
            {picked.length === 0 && !alwaysDeclared
              ? (
                <p
                  className="text-[11.5px] leading-4 text-amber-500/90"
                  data-testid={`${testId}-none-note`}
                >
                  {noneWarning
                    ?? tx(
                      "agents.editor.bindingNoneNote",
                      "Declared and empty: this agent reaches none of them.",
                    )}
                </p>
              )
              : null}
          </>
        )}
    </div>
  );
}

/** One of the two mode buttons. Same pill shape as the options, one weight louder. */
function ModeButton({
  pressed,
  onClick,
  children,
  testId,
}: {
  pressed: boolean;
  onClick: () => void;
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "rounded-full px-3 py-1 text-[11.5px] font-medium leading-4 transition-colors",
        pressed
          ? "bg-primary/20 text-foreground"
          : "bg-background/70 text-muted-foreground hover:text-foreground",
      )}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
