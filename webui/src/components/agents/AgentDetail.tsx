/**
 * One agent, open in the page, configured across tabs -- nanoinfraorg/nanoinfra#262, reshaped here
 * to match the product the maintainer put next to it.
 *
 * Until this existed, having an agent at all meant hand-editing `~/.nanoinfra/config.json`, while
 * every other object in the product -- servers, secrets, skills, automations, MCP servers,
 * connectors, standing grants -- was editable from the WebUI.
 *
 * **Tabs, in the page, not a dialog.** An agent has nine config keys across several kinds of
 * question, and one modal holding all of them is a scrolling form whose lower half is below the
 * fold: the fields you have not thought about yet are the ones you cannot see. A dialog is kept for
 * exactly one thing, in `AgentRoster`: the delete, which is destructive and wants an interruption.
 *
 * **The chrome is `AgentDetailFrame`, shared with the deployment's own agent.** Back arrow,
 * monospace name, badge, `model - provider`, the underlined tab strip and the one `Save` all live
 * there, because `agents.defaults` is edited through the same page now (#265) and two pages drawn
 * twice would drift on the day one of them grew a field. What stays here is what is specific to a
 * *named* agent: nine config keys, a roster that travels whole, and a delegate picker.
 *
 * **`Basic` carries the addendum.** It used to live on `Prompt`, which is the larger half of why
 * that tab confused: it opened on a form whose one editable field was an addendum, above an
 * inventory of twelve sections nobody could touch. The addendum is a property of the agent, like
 * its name and its description, so it sits with them -- and `Prompt` is now the prompt.
 *
 * **`Model` is its own tab.** One field, deliberately: the model is the first question anybody asks
 * of an agent and the answer used to be the third row of a `General` tab.
 *
 * **One draft, one save.** The tabs are views of a single `values`, and the save affordance is on
 * the frame around them rather than inside any tab -- a tabbed editor hides unsaved work by
 * definition, so *where* the unsaved state is shown is the design and not a detail. Every save
 * sends the whole roster, so moving between tabs cannot lose an edit and saving from `Tools` cannot
 * drop what was typed on `Prompt`.
 *
 * **The rules are config's.** An unknown delegate, a name that cannot be an `@agent:<name>` token:
 * each is refused by `nanoinfra/config/schema.py`, in a sentence naming the offending value, and
 * that sentence is rendered here verbatim. The one exception is self-delegation, which the delegate
 * picker prevents by offering the *other* agents -- a control whose only outcome is a refusal is a
 * bad control, not validation.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { AgentBindingPicker } from "@/components/agents/AgentBindingPicker";
import { AgentDetailFrame } from "@/components/agents/AgentDetailFrame";
import { AgentPromptBadge } from "@/components/agents/AgentPromptBadge";
import { AgentPromptPanel } from "@/components/agents/AgentPromptPanel";
import {
  agentValuesFromEntry,
  deploymentDefaultLabel,
  draftHasOwnPrompt,
  gatewayReportsBindings,
  presetModelLine,
  rosterWithAgent,
  toolGroupOptions,
} from "@/components/agents/agentValues";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useAgentCatalogues } from "@/components/agents/useAgentCatalogues";
import { saveNamedAgents, serverReason } from "@/lib/api";
import type { NamedAgentRosterEntry, NamedAgentValues, SettingsPayload } from "@/lib/types";

/**
 * The order somebody configures an agent in, which is why it is also the order of the strip.
 *
 * **No `MCP` tab, and that is a decision rather than an omission.** The reference product has one,
 * because there each source of tools has a panel of its own. Here `toolGroups`, `connectors` and
 * `mcpServers` are three answers to a single question -- what may this agent call -- and the
 * answers narrow each other: an MCP server bound to an agent whose tool groups exclude it reaches
 * nothing. Split across two tabs that contradiction is a tab-switch away from being visible, and
 * the tab it moved to holds one picker that is empty on every deployment which installs no MCP
 * server. So MCP stays the third picker on `Tools`.
 *
 * There is likewise no `Channels`, `Tasks`, `API Keys`, `Knowledge` or `Resources`: an agent has no
 * such binding in `NamedAgentConfig`, and a tab for a config key that does not exist is a promise
 * this page cannot keep.
 */
const TABS = ["basic", "model", "tools", "skills", "delegates", "prompt"] as const;
type AgentTab = (typeof TABS)[number];

/** The sentinel for *inherit `agents.defaults`*, which is what an unset `modelPreset` means. */
const DEPLOYMENT_DEFAULT_PRESET = "";

export function AgentDetail({
  entry,
  entries,
  modelPresets,
  declaredToolGroups,
  token,
  base = "",
  onClose,
  onSaved,
  onNavigateToToolGroups,
}: {
  /**
   * The agent being configured.
   *
   * Never `null` any more: an agent is created by the inline form on the index, the way the
   * reference product does it, and this page opens on the row that write produced. A page that
   * doubled as a create form had to render a name field that becomes read-only, an empty prompt
   * tab, and catalogue pickers for an agent that does not exist yet.
   */
  entry: NamedAgentRosterEntry;
  /** The roster this agent belongs to: the peers it may delegate to, and the rest of the write. */
  entries: NamedAgentRosterEntry[];
  modelPresets: SettingsPayload["model_presets"];
  /** `tools.groups` plus the built-ins, keyed by name, when this gateway reports them. */
  declaredToolGroups?: Record<string, unknown>;
  token: string;
  base?: string;
  onClose: () => void;
  /** The fresh payload, and the name that was saved, so a caller can re-key onto its new row. */
  onSaved: (payload: SettingsPayload, name: string) => void;
  /** Opens the Tool groups panel, which is where a group is made. */
  onNavigateToToolGroups?: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const [tab, setTab] = useState<AgentTab>("basic");
  const [values, setValues] = useState<NamedAgentValues>(() => agentValuesFromEntry(entry));
  const [saved, setSaved] = useState<NamedAgentValues>(() => agentValuesFromEntry(entry));
  const catalogues = useAgentCatalogues(token, base);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keyed on the agent, never on the tab: re-seeding when the strip changes is precisely how a
  // tabbed editor loses the edit you made two tabs ago.
  useEffect(() => {
    const next = agentValuesFromEntry(entry);
    setValues(next);
    setSaved(next);
    setError(null);
  }, [entry]);


  // A payload with no binding lists at all predates this editor, and a whole-roster write built
  // from it would blank what the form never saw. An *empty* list is a real value and edits
  // normally -- absence is the test. Said up front rather than discovered after a save.
  const stale = !gatewayReportsBindings(entry);
  const dirty = JSON.stringify(values) !== JSON.stringify(saved);

  const save = async () => {
    if (saving || stale) return;
    setSaving(true);
    setError(null);
    try {
      const payload = await saveNamedAgents(
        token,
        rosterWithAgent(entries, entry.name, values),
        base,
      );
      setSaved(values);
      onSaved(payload, entry.name);
    } catch (err) {
      setError(serverReason(err));
    } finally {
      setSaving(false);
    }
  };

  const tabLabels: Record<AgentTab, string> = {
    basic: tx("agents.detail.basic", "Basic"),
    model: tx("agents.editor.fields.modelPreset", "Model"),
    tools: tx("agents.detail.tools", "Tools"),
    skills: tx("agents.detail.skills", "Skills"),
    delegates: tx("agents.detail.delegates", "Delegates"),
    prompt: tx("agents.detail.prompt", "Prompt"),
  };

  const bindingHelp = tx(
    "agents.editor.bindingHelp",
    "Everything is whatever this deployment activates. Only these narrows this agent to what you pick, and never widens it -- an empty pick is an agent that loads none of them, which is what a schema budget is spent on.",
  );

  /*
   * The draft's own preset, so the line under the name follows an unsaved change of model --
   * including a change *to* inherit, where `null` is the question rather than a missing answer:
   * `presetModelLine` resolves it to the preset this deployment activates, which is what will
   * actually run. Passing `entry.model_preset` there instead would keep showing the preset the
   * agent used to declare until the save landed.
   */
  const line = presetModelLine(values.modelPreset, modelPresets);

  return (
    <AgentDetailFrame
      name={entry.name}
      badge={<AgentPromptBadge own={draftHasOwnPrompt(values)} testId="agent-detail-prompt-source" />}
      subtitle={line ? `${line.model} · ${line.provider}` : values.modelPreset ?? entry.model_preset}
      tabs={TABS.map((key) => ({ key, label: tabLabels[key] }))}
      tab={tab}
      onTab={(key) => setTab(key as AgentTab)}
      dirty={dirty}
      saving={saving}
      canSave={!stale}
      onSave={() => void save()}
      onClose={onClose}
      error={error}
      notice={stale
        ? (
          <p
            className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive-text"
            data-testid="agent-detail-stale"
          >
            {tx(
              "agents.editor.staleGateway",
              "This gateway reports how many tool groups, skills and delegates {{name}} has but not which ones, so saving would replace them with nothing. Update the gateway to edit this agent here.",
              { name: entry.name },
            )}
          </p>
        )
        : undefined}
    >
      <>
        {tab === "basic"
          ? (
            <div className="space-y-4" data-testid="agent-tab-basic">
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("agents.editor.fields.name", "Name")}
                </span>
                {/*
                  * Fixed after creation. An automation binds an agent by name
                  * (`nanoinfra/cron/agent_binding.py`), and a rename this panel could make would
                  * leave that binding pointing at an agent that no longer exists -- silently,
                  * because nothing else in config mentions it.
                  */}
                <p
                  className="flex h-10 items-center rounded-[12px] bg-background/70 px-3 font-mono text-[13px] text-foreground"
                  data-testid="agent-detail-name-fixed"
                >
                  {entry.name}
                </p>
                <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
                  {tx(
                    "agents.editor.nameFixedHelp",
                    "An agent keeps the name it was created with: automations and threads address it by that name.",
                  )}
                </span>
              </label>

              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("agents.editor.fields.description", "Description")}
                </span>
                <Input
                  value={values.description}
                  onChange={(event) =>
                    setValues((prev) => ({ ...prev, description: event.target.value }))}
                  placeholder={tx(
                    "agents.editor.descriptionPlaceholder",
                    "hands-on checks on one host",
                  )}
                  className="h-10 rounded-[12px]"
                />
                <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
                  {tx(
                    "agents.editor.descriptionHelp",
                    "The line that explains this agent wherever it is offered, including to another agent deciding whether to delegate.",
                  )}
                </span>
              </label>

              {/*
                * The addendum, here rather than on `Prompt`, with the sentence that says which of
                * the two prompt controls it is: this one adds, and the pencil on `Prompt` is the
                * one that can take something away.
                */}
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("agents.editor.fields.addendum", "Agent addendum")}
                </span>
                <Textarea
                  value={values.addendum}
                  rows={6}
                  onChange={(event) =>
                    setValues((prev) => ({ ...prev, addendum: event.target.value }))}
                  placeholder={tx(
                    "agents.prompt.addendumPlaceholder",
                    "Prefer read-only checks, and say what you did not check.",
                  )}
                  className="rounded-[12px] font-mono text-[11.5px] leading-5"
                  data-testid="agent-addendum-editor"
                />
                <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
                  {tx(
                    "agents.editor.addendumHelp",
                    "Appended after the platform's own sections. It can only add: a sentence you disagree with cannot be undone by appending a correction, because the model is handed both. To remove one, edit that section on the Prompt tab.",
                  )}
                </span>
              </label>
            </div>
          )
          : null}

        {tab === "model"
          ? (
            <div className="space-y-4" data-testid="agent-tab-model">
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("agents.editor.fields.modelPreset", "Model")}
                </span>
                <select
                  value={values.modelPreset ?? DEPLOYMENT_DEFAULT_PRESET}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      modelPreset: event.target.value === DEPLOYMENT_DEFAULT_PRESET
                        ? null
                        : event.target.value,
                    }))}
                  className="h-10 w-full rounded-[12px] border border-input bg-background px-3 text-[13px] text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {/*
                    * The sentinel names what will actually answer. `Deployment default` on its own
                    * reads like the name of a model, and no deployment has one called that -- so
                    * the preset that is inherited is in the brackets, and the option cannot be
                    * mistaken for a choice of model.
                    */}
                  <option value={DEPLOYMENT_DEFAULT_PRESET}>
                    {inheritedLabel(modelPresets, tx)}
                  </option>
                  {/*
                    * A preset this build does not list is still offered, because it is the one
                    * config holds: a select that dropped it would reset the agent's model on the
                    * next save without saying so.
                    */}
                  {values.modelPreset
                      && !modelPresets.some((preset) => preset.name === values.modelPreset)
                    ? <option value={values.modelPreset}>{values.modelPreset}</option>
                    : null}
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
            </div>
          )
          : null}

        {tab === "tools"
          ? (
            <div className="space-y-5" data-testid="agent-tab-tools">
              <AgentBindingPicker
                testId="agent-editor-tool-groups"
                label={tx("agents.editor.fields.toolGroups", "Tool groups")}
                help={tx(
                  "agents.editor.toolGroupsHelp",
                  "Everything means no ceiling. Only these narrows the agent to the groups you pick -- and picking none is how an agent is told it must ask a peer for anything grouped.",
                )}
                options={toolGroupOptions(declaredToolGroups, entries)}
                selected={values.toolGroups}
                onChange={(toolGroups) => setValues((prev) => ({ ...prev, toolGroups }))}
                emptyHint={tx(
                  "agents.editor.toolGroupsEmpty",
                  "This deployment has no tool groups yet.",
                )}
                emptyAction={onNavigateToToolGroups
                  ? {
                    label: tx("agents.editor.toolGroupsLink", "Open Tool groups"),
                    onClick: onNavigateToToolGroups,
                  }
                  : undefined}
              />
              <AgentBindingPicker
                testId="agent-editor-connectors"
                label={tx("agents.editor.fields.connectors", "Connectors")}
                help={bindingHelp}
                options={catalogues.connectors}
                selected={values.connectors}
                onChange={(connectors) => setValues((prev) => ({ ...prev, connectors }))}
                emptyHint={tx(
                  "agents.editor.connectorsEmpty",
                  "This deployment activates no connectors.",
                )}
              />
              <AgentBindingPicker
                testId="agent-editor-mcp-servers"
                label={tx("agents.editor.fields.mcpServers", "MCP servers")}
                help={bindingHelp}
                options={catalogues.mcpServers}
                selected={values.mcpServers}
                onChange={(mcpServers) => setValues((prev) => ({ ...prev, mcpServers }))}
                emptyHint={tx(
                  "agents.editor.mcpServersEmpty",
                  "This deployment installs no MCP servers.",
                )}
              />
            </div>
          )
          : null}

        {tab === "skills"
          ? (
            <div data-testid="agent-tab-skills">
              <AgentBindingPicker
                testId="agent-editor-skills"
                label={tx("agents.editor.fields.skills", "Skills to load")}
                help={tx(
                  "agents.editor.skillsHelp",
                  "Everything summarises the whole catalogue, as before. Only these loads what you pick in full and summarises nothing else -- which is what keeps a conversation from paying for every skill installed.",
                )}
                options={catalogues.skills}
                selected={values.skills}
                onChange={(skills) => setValues((prev) => ({ ...prev, skills }))}
                emptyHint={tx("agents.editor.skillsEmpty", "No skills installed")}
              />
            </div>
          )
          : null}

        {tab === "delegates"
          ? (
            <div data-testid="agent-tab-delegates">
              <AgentBindingPicker
                testId="agent-editor-delegates"
                label={tx("agents.editor.fields.delegates", "Delegates")}
                help={tx(
                  "agents.editor.delegatesHelp",
                  "The agents this one may hand work to. Membership is the authorization, so an agent reaches only the peers named here.",
                )}
                // The other agents. Never this one -- see the note at the top of the file.
                options={entries
                  .filter((each) => each.name !== entry.name)
                  .map((each) => ({ name: each.name, description: each.description }))}
                alwaysDeclared
                selected={values.delegates}
                onChange={(delegates) =>
                  setValues((prev) => ({ ...prev, delegates: delegates ?? [] }))}
                emptyHint={tx(
                  "agents.editor.delegatesEmpty",
                  "There is no other agent to delegate to yet.",
                )}
              />
            </div>
          )
          : null}

        {tab === "prompt"
          ? (
            <div data-testid="agent-tab-prompt">
              <AgentPromptPanel
                agent={entry.name}
                token={token}
                base={base}
                values={values}
                onChange={setValues}
                dirty={dirty}
              />
            </div>
          )
          : null}
      </>
    </AgentDetailFrame>
  );
}

/**
 * The sentinel option's label: `Deployment default (Kimi General)`.
 *
 * Two strings rather than one interpolation with an empty variable, because `Deployment default ()`
 * is worse than the bare sentence -- and a build that lists no presets at all has nothing true to
 * put in the brackets.
 */
function inheritedLabel(
  presets: SettingsPayload["model_presets"],
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  const inherited = deploymentDefaultLabel(null, presets);
  return inherited
    ? tx("agents.editor.presetDefaultNamed", "Deployment default ({{name}})", { name: inherited })
    : tx("agents.editor.presetDefault", "Deployment default");
}
