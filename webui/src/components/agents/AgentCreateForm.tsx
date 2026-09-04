/**
 * Naming an agent, inline at the top of the list -- the shape the reference product uses.
 *
 * It used to be the agent page itself with `entry === null`, and the seams showed: a name field
 * that becomes read-only after the first save, a `Prompt` tab that can only say *this appears once
 * the agent is saved*, and catalogue pickers offering bindings for an agent that does not exist.
 * Creation asks three questions; configuration asks nine. Two forms.
 *
 * **Inline, not a dialog.** The list stays on screen behind it, which is the answer to the one
 * question this form cannot ask for you: whether the name you are typing is already taken. The
 * duplicate check is here as well, because the write **replaces** the roster -- a create that
 * reused an existing name would silently overwrite that agent with three empty fields, and config
 * would accept it.
 *
 * **Only the fields this product has.** The reference form also carries a `Type` block of radio
 * cards and a pair of toggles; `NamedAgentConfig` has no type and no booleans, and inventing either
 * would be a control whose value nothing reads. `Provider` is shown but not chosen: a preset
 * carries the model and the provider together, so the provider is the consequence of the model
 * beside it rather than a second decision to keep in step.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";

import {
  blankAgentValues,
  deploymentDefaultLabel,
  presetModelLine,
  rosterWithAgent,
} from "@/components/agents/agentValues";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { saveNamedAgents, serverReason } from "@/lib/api";
import type { NamedAgentRosterEntry, SettingsPayload } from "@/lib/types";

/** The sentinel for *inherit `agents.defaults`*, which is what an unset `modelPreset` means. */
const DEPLOYMENT_DEFAULT_PRESET = "";

export function AgentCreateForm({
  entries,
  modelPresets,
  token,
  base = "",
  onCancel,
  onCreated,
}: {
  /** The roster the new agent joins. It travels whole, so a create cannot empty a neighbour. */
  entries: NamedAgentRosterEntry[];
  modelPresets: SettingsPayload["model_presets"];
  token: string;
  base?: string;
  onCancel: () => void;
  /** The fresh payload and the name written, so the caller can open the row it produced. */
  onCreated: (payload: SettingsPayload, name: string) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [modelPreset, setModelPreset] = useState<string>(DEPLOYMENT_DEFAULT_PRESET);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = name.trim();
  const duplicate = entries.some((each) => each.name === trimmed);
  const line = presetModelLine(modelPreset || null, modelPresets);
  const inherited = deploymentDefaultLabel(null, modelPresets);

  const create = async () => {
    if (!trimmed || saving || duplicate) return;
    setSaving(true);
    setError(null);
    try {
      const payload = await saveNamedAgents(
        token,
        rosterWithAgent(entries, trimmed, {
          ...blankAgentValues(),
          description: description.trim(),
          modelPreset: modelPreset || null,
        }),
        base,
      );
      onCreated(payload, trimmed);
    } catch (err) {
      setError(serverReason(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="mb-3 space-y-4 rounded-[22px] bg-settings-surface px-4 py-4 sm:px-5"
      data-testid="agent-create-form"
      onSubmit={(event) => {
        event.preventDefault();
        void create();
      }}
    >
      <h3 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
        {tx("agents.detail.createTitle", "New agent")}
      </h3>

      <label className="block space-y-1.5">
        <span className="text-[12px] font-medium text-muted-foreground">
          {tx("agents.editor.fields.name", "Name")}
        </span>
        <Input
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
          placeholder={tx("agents.editor.namePlaceholder", "sre")}
          className="h-10 rounded-[12px] font-mono"
          data-testid="agent-create-name"
        />
        <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
          {tx(
            "agents.editor.nameHelp",
            "How the agent is addressed in a message, as @agent:<name>. Letters, digits, '-', '_' and '.'.",
          )}
        </span>
      </label>

      <label className="block space-y-1.5">
        <span className="text-[12px] font-medium text-muted-foreground">
          {tx("agents.editor.fields.description", "Description")}
        </span>
        <Input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={tx("agents.editor.descriptionPlaceholder", "hands-on checks on one host")}
          className="h-10 rounded-[12px]"
          data-testid="agent-create-description"
        />
        <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
          {tx(
            "agents.editor.descriptionHelp",
            "The line that explains this agent wherever it is offered, including to another agent deciding whether to delegate.",
          )}
        </span>
      </label>

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block space-y-1.5">
          <span className="text-[12px] font-medium text-muted-foreground">
            {tx("agents.editor.fields.modelPreset", "Model")}
          </span>
          <select
            value={modelPreset}
            onChange={(event) => setModelPreset(event.target.value)}
            className="h-10 w-full rounded-[12px] border border-input bg-background px-3 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="agent-create-model"
          >
            <option value={DEPLOYMENT_DEFAULT_PRESET}>
              {inherited
                ? tx("agents.editor.presetDefaultNamed", "Deployment default ({{name}})", {
                  name: inherited,
                })
                : tx("agents.editor.presetDefault", "Deployment default")}
            </option>
            {modelPresets.filter((preset) => !preset.is_default).map((preset) => (
              <option key={preset.name} value={preset.name}>
                {preset.label || preset.name}
              </option>
            ))}
          </select>
          <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
            {tx(
              "agents.editor.modelPresetHelp",
              "Which of this deployment's model presets answers for this agent. A preset carries the model, the provider and the limits together, so there is nothing to keep in step by hand.",
            )}
          </span>
        </label>

        <div className="space-y-1.5">
          <span className="text-[12px] font-medium text-muted-foreground">
            {tx("agents.editor.fields.provider", "Provider")}
          </span>
          <p
            className="flex h-10 items-center rounded-[12px] bg-background/70 px-3 text-[13px] text-foreground"
            data-testid="agent-create-provider"
          >
            {line ? line.provider : "—"}
          </p>
          <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
            {tx(
              "agents.editor.providerHelp",
              "Decided by the preset, not chosen here: a preset names the model and the provider that serves it together.",
            )}
          </span>
        </div>
      </div>

      {duplicate
        ? (
          <p
            className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
            data-testid="agent-create-duplicate"
          >
            {tx(
              "agents.editor.duplicate",
              "{{name}} already exists. Saving would replace it, so open that agent instead.",
              { name: trimmed },
            )}
          </p>
        )
        : null}
      {error
        ? (
          <p
            className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
            data-testid="agent-create-error"
          >
            {error}
          </p>
        )
        : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="submit"
          disabled={!trimmed || saving || duplicate}
          className="h-8 rounded-full px-4 text-[12px]"
          data-testid="agent-create-submit"
        >
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
          {tx("agents.create.submit", "Create")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={onCancel}
          disabled={saving}
          className="h-8 rounded-full px-3 text-[12px]"
          data-testid="agent-create-cancel"
        >
          {tx("agents.roster.deleteCancel", "Cancel")}
        </Button>
      </div>
    </form>
  );
}
