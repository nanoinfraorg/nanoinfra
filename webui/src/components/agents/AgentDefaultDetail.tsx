/**
 * The deployment's own agent, edited -- `agents.defaults` (#265).
 *
 * This was a read-only expandable row an hour ago, and the reason it was read-only is worth
 * keeping: there was no route that wrote `agents.defaults`, so a `Save` here could only ever come
 * back refused. There is one now -- `GET /api/settings/agents/defaults` -- and `AgentDefaults`
 * gained every field a named agent had and it lacked: `addendum`, `promptSections`, `toolGroups`,
 * `skills`, `connectors`, `mcpServers` and `delegates`.
 *
 * **Why this matters more than the named agents' version of it.** Until the same hour, a named
 * agent's addendum and its replaced sections never reached a turn: `build_system_prompt` accepted
 * both parameters and the only call site passed neither, so everything the Prompt tab edited was
 * stored, shown, editable -- and inert. That is fixed (`AgentLoop._agent_prompt_for`, pinned by
 * `tests/agent/test_turn_attribution.py`), and the same wiring carries the default agent's. So
 * what is edited here changes what the model is actually told on **every turn that names no
 * agent**, which in a deployment with no named agents is every turn there is. The panel says so
 * rather than leaving it to be discovered.
 *
 * **It is one more agent, and #266 is where that stopped being a slogan.** This page had three
 * tabs and each absence had a written reason: no skills, because the default agent's skills were
 * the inverse list in the Skills panel; no delegates, because `AgentDefaults` had no such field;
 * no MCP and no connectors, because nothing narrowed them. Every one of those reasons was a
 * description of a missing field rather than an argument for missing it, and the person who
 * pointed that out had the concrete version of it: *if every MCP server and every skill I have
 * installed loads, one conversation spends the context.* The agent that answers when nobody picks
 * was the only agent that could not be narrowed, and it is the one that answers most.
 *
 * So the tabs are the named agents' tabs, and what is edited here narrows a real turn: the tool
 * groups cap the tool schemas, the skills replace the catalogue with the ones named, and the MCP
 * and connector lists decide which servers' schemas load at all. `AgentLoop` puts the acting
 * agent's ceilings on the turn -- see `_record_acting_agent_binding` -- so this is the same
 * machinery an automation's agent has always used, pointed at the turn a person starts.
 *
 * **Two absences remain, and these are decisions.**
 *
 * - No `Model`. The default agent answers with the preset this deployment activates, edited in
 *   `Settings > Models`; a second control for it here would be a second place for one value.
 *   `Basic` links there instead, alongside the rest of what this agent *is*.
 * - No name and no description. There is one default agent, and a description exists to explain
 *   an agent to the peer that might delegate to it.
 *
 * **The write is a patch, not a snapshot.** `agents.defaults` holds twenty-six fields; this form
 * shows seven. The route writes the keys a request carries and leaves the rest, so sending the
 * form's whole local state would be a request to reset the timezone, the tool-iteration cap and
 * the subagent limit to whatever this client last read. `agentDefaultsPatch` sends the diff.
 *
 * **And a save is refused outright while the gateway cannot report what it would overwrite.** A
 * payload with no `addendum` key predates the write route; a form built from that reading shows a
 * blank box over a paragraph the deployment actually has, and then offers to save the blank. The
 * same rule the roster applies to a payload carrying counts without bindings.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { AgentBindingPicker } from "@/components/agents/AgentBindingPicker";
import { AgentDetailFrame } from "@/components/agents/AgentDetailFrame";
import { AgentPromptBadge } from "@/components/agents/AgentPromptBadge";
import { AgentPromptPanel } from "@/components/agents/AgentPromptPanel";
import {
  agentDefaultsPatch,
  agentDefaultsValues,
  deploymentModelLine,
  gatewayReportsAgentDefaults,
  toolGroupOptions,
} from "@/components/agents/agentValues";
import { useAgentCatalogues } from "@/components/agents/useAgentCatalogues";
import { Textarea } from "@/components/ui/textarea";
import { saveAgentDefaults, serverReason } from "@/lib/api";
import type {
  AgentDefaultsValues,
  NamedAgentRosterEntry,
  SettingsPayload,
} from "@/lib/types";

const TABS = ["basic", "tools", "skills", "delegates", "prompt"] as const;
type DefaultsTab = (typeof TABS)[number];

export function AgentDefaultDetail({
  agent,
  entries,
  declaredToolGroups,
  token,
  base = "",
  onClose,
  onSaved,
  onNavigateToModels,
  onNavigateToSkills,
  onNavigateToToolGroups,
}: {
  /** `agents.defaults` as the settings payload reports it. */
  agent: SettingsPayload["agent"];
  /** The named agents, only so the tool-group picker can offer every group in use. */
  entries: NamedAgentRosterEntry[];
  declaredToolGroups?: Record<string, unknown>;
  token: string;
  base?: string;
  onClose: () => void;
  onSaved: (payload: SettingsPayload) => void;
  onNavigateToModels?: () => void;
  onNavigateToSkills?: () => void;
  onNavigateToToolGroups?: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const catalogues = useAgentCatalogues(token, base);
  const [tab, setTab] = useState<DefaultsTab>("basic");
  const [values, setValues] = useState<AgentDefaultsValues>(() => agentDefaultsValues(agent));
  const [saved, setSaved] = useState<AgentDefaultsValues>(() => agentDefaultsValues(agent));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keyed on the payload, never on the tab: re-seeding when the strip changes is how a tabbed
  // editor loses the edit you made two tabs ago.
  useEffect(() => {
    const next = agentDefaultsValues(agent);
    setValues(next);
    setSaved(next);
    setError(null);
  }, [agent]);

  const stale = !gatewayReportsAgentDefaults(agent);
  const patch = agentDefaultsPatch(saved, values);
  const dirty = Object.keys(patch).length > 0;

  const save = async () => {
    if (saving || stale || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      // The diff, never the whole draft. See the note at the top of the file.
      const payload = await saveAgentDefaults(token, patch, base);
      setSaved(values);
      onSaved(payload);
    } catch (err) {
      setError(serverReason(err));
    } finally {
      setSaving(false);
    }
  };

  const tabLabels: Record<DefaultsTab, string> = {
    basic: tx("agents.detail.basic", "Basic"),
    tools: tx("agents.detail.tools", "Tools"),
    skills: tx("agents.detail.skills", "Skills"),
    delegates: tx("agents.detail.delegates", "Delegates"),
    prompt: tx("agents.detail.prompt", "Prompt"),
  };

  const modelLine = deploymentModelLine(
    agent.model,
    agent.resolved_provider || agent.provider,
    tx("agents.roster.noModel", "No model configured"),
  );

  return (
    <AgentDetailFrame
      testId="agent-default-detail"
      name={tx("settings.automations.agentDefault", "Default agent")}
      badge={
        <AgentPromptBadge
          own={Boolean(values.addendum.trim()) || Object.keys(values.promptSections).length > 0}
          testId="agent-default-detail-prompt-source"
        />
      }
      subtitle={modelLine}
      tabs={TABS.map((key) => ({ key, label: tabLabels[key] }))}
      tab={tab}
      onTab={(key) => setTab(key as DefaultsTab)}
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
            data-testid="agent-default-detail-stale"
          >
            {tx(
              "agents.defaults.staleGateway",
              "This gateway does not report the default agent's addendum, replaced sections or bindings, so saving would replace them with what this form never read. Update the gateway to edit them here.",
            )}
          </p>
        )
        : undefined}
    >
      <>
        {tab === "basic"
          ? (
            <div className="space-y-4" data-testid="agent-default-tab-basic">
              {/*
                * Said first, because it changes what this form is for. Until the turn wiring
                * landed, everything below was stored and shown and never reached a model.
                */}
              <p
                className="rounded-[12px] bg-muted/60 px-3 py-2 text-[11.5px] leading-5 text-foreground/85"
                data-testid="agent-default-reach"
              >
                {tx(
                  "agents.defaults.reachesEveryTurn",
                  "What you write here is added to the system prompt of every turn that names no agent — which is every turn, in a deployment that names none.",
                )}
              </p>

              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("agents.editor.fields.addendum", "Agent addendum")}
                </span>
                <Textarea
                  value={values.addendum}
                  rows={8}
                  onChange={(event) =>
                    setValues((prev) => ({ ...prev, addendum: event.target.value }))}
                  placeholder={tx(
                    "agents.prompt.addendumPlaceholder",
                    "Prefer read-only checks, and say what you did not check.",
                  )}
                  className="rounded-[12px] font-mono text-[11.5px] leading-5"
                  data-testid="agent-default-addendum-editor"
                />
                <span className="block text-[11.5px] leading-4 text-muted-foreground/80">
                  {tx(
                    "agents.editor.addendumHelp",
                    "Appended after the platform's own sections. It can only add: a sentence you disagree with cannot be undone by appending a correction, because the model is handed both. To remove one, edit that section on the Prompt tab.",
                  )}
                </span>
              </label>

              {/*
                * What is *not* this form's to change. It was four facts and is one: skills, MCP
                * servers and delegates all became tabs, so stating them here would be stating
                * them twice and in the weaker place. The model stays, because it genuinely is
                * another panel's -- and it is the first question this page gets asked.
                */}
              <AgentDefaultFacts
                modelPreset={agent.model_preset}
                modelLine={modelLine}
                onNavigateToModels={onNavigateToModels}
              />
            </div>
          )
          : null}

        {tab === "tools"
          ? (
            <div className="space-y-5" data-testid="agent-default-tab-tools">
              <AgentBindingPicker
                testId="agent-default-tool-groups"
                label={tx("agents.editor.fields.toolGroups", "Tool groups")}
                help={tx(
                  "agents.defaults.toolGroupsHelp",
                  "Everything is what a deployment has before it narrows anything. Only these caps every turn that names no agent — the tools outside the groups you pick are not offered and a call to one is refused.",
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
                testId="agent-default-mcp-servers"
                label={tx("agents.editor.fields.mcpServers", "MCP servers")}
                help={tx(
                  "agents.defaults.mcpHelp",
                  "This is the one that costs the most. Every installed server's schemas load in the first message of every conversation, so Only these is how a deployment with a dozen of them stops paying for eleven.",
                )}
                options={catalogues.mcpServers}
                selected={values.mcpServers}
                onChange={(mcpServers) => setValues((prev) => ({ ...prev, mcpServers }))}
                emptyHint={tx(
                  "agents.editor.mcpServersEmpty",
                  "This deployment installs no MCP servers.",
                )}
              />
              <AgentBindingPicker
                testId="agent-default-connectors"
                label={tx("agents.editor.fields.connectors", "Connectors")}
                help={tx(
                  "agents.defaults.connectorsHelp",
                  "Everything is whatever this deployment activates. Only these narrows the turn to the connectors you pick, and never widens it.",
                )}
                options={catalogues.connectors}
                selected={values.connectors}
                onChange={(connectors) => setValues((prev) => ({ ...prev, connectors }))}
                emptyHint={tx(
                  "agents.editor.connectorsEmpty",
                  "This deployment activates no connectors.",
                )}
              />
            </div>
          )
          : null}

        {tab === "skills"
          ? (
            <div data-testid="agent-default-tab-skills">
              <AgentBindingPicker
                testId="agent-default-skills"
                label={tx("agents.editor.fields.skills", "Skills to load")}
                help={tx(
                  "agents.defaults.skillsHelp",
                  "Everything summarises the whole catalogue, as before. Only these loads what you pick in full and summarises nothing else — the catalogue itself is what a long list of skills spends.",
                )}
                options={catalogues.skills}
                selected={values.skills}
                onChange={(skills) => setValues((prev) => ({ ...prev, skills }))}
                emptyHint={tx("agents.editor.skillsEmpty", "No skills installed")}
                emptyAction={onNavigateToSkills
                  ? {
                    label: tx("agents.roster.openSkills", "Open Skills"),
                    onClick: onNavigateToSkills,
                  }
                  : undefined}
                noneWarning={tx(
                  "agents.defaults.skillsNoneNote",
                  "Declared and empty: no skill loads and no catalogue is summarised either.",
                )}
              />
              <p
                className="pt-3 text-[11.5px] leading-5 text-muted-foreground/80"
                data-testid="agent-default-skills-vs-disabled"
              >
                {tx(
                  "agents.defaults.skillsVsDisabled",
                  "Separate from the skills this deployment disables, in Settings → Skills. That list removes a skill from every agent; this one chooses which of what remains this agent loads in full.",
                )}
              </p>
            </div>
          )
          : null}

        {tab === "delegates"
          ? (
            <div data-testid="agent-default-tab-delegates">
              <AgentBindingPicker
                testId="agent-default-delegates"
                alwaysDeclared
                label={tx("agents.editor.fields.delegates", "Delegates")}
                help={tx(
                  "agents.defaults.delegatesHelp",
                  "The agents this one may hand work to. It matters most here: this is the agent you talk to, so without a peer named the only way to reach one is to stop talking to it.",
                )}
                options={entries.map((each) => ({
                  name: each.name,
                  description: each.description,
                }))}
                selected={values.delegates}
                onChange={(delegates) =>
                  setValues((prev) => ({ ...prev, delegates: delegates ?? [] }))}
                emptyHint={tx(
                  "agents.defaults.delegatesEmpty",
                  "This deployment names no other agent yet.",
                )}
              />
            </div>
          )
          : null}

        {tab === "prompt"
          ? (
            <div data-testid="agent-default-tab-prompt">
              {/*
                * The same panel the named agents use, over this agent's own draft. It asks the
                * gateway for the composition with no agent name, which is how the deployment's
                * own agent is spelled on that route; a gateway that does not serve it yet says
                * so in the panel rather than here.
                */}
              <AgentPromptPanel
                agent={null}
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
 * What this page does *not* edit: the model, and the scope of the page itself.
 *
 * It was six facts when skills, MCP servers and delegates were things the default agent could not
 * have. They are tabs now, so what is left is the one binding that really does belong to another
 * panel -- a preset is chosen once for the deployment, and a second control for it here would be
 * a second place for one value.
 */
function AgentDefaultFacts({
  modelPreset,
  modelLine,
  onNavigateToModels,
}: {
  modelPreset: string | null;
  modelLine: string;
  onNavigateToModels?: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <dl
      className="space-y-2.5 border-t border-border/40 pt-3.5"
      data-testid="agent-default-facts"
    >
      <Fact
        term={tx("agents.editor.fields.modelPreset", "Model")}
        value={modelPreset ?? modelLine}
        detail={tx(
          "agents.roster.defaultModelFact",
          "The preset this deployment activates. Changing it changes every agent that names none of its own.",
        )}
        action={onNavigateToModels
          ? { label: tx("agents.roster.openModels", "Open Models"), onClick: onNavigateToModels }
          : undefined}
      />
      <p
        className="pt-1 text-[11.5px] leading-5 text-muted-foreground/80"
        data-testid="agent-default-not-editable"
      >
        {tx(
          "agents.defaults.scope",
          "This agent cannot be added, renamed or removed: it is what a deployment has before it names anything. Everything else about it is edited here, like any other agent. Its deployment settings — the timezone, the tool-iteration cap, the subagent limit — live in the panels that own them.",
        )}
      </p>
    </dl>
  );
}

/** One stated fact, with the panel that owns it when another panel does. */
function Fact({
  term,
  detail,
  value,
  action,
}: {
  term: string;
  detail: string;
  value?: string;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="flex flex-wrap items-center gap-2 text-[12px] font-medium text-foreground/85">
        {term}
        {value
          ? (
            <span className="font-mono text-[11.5px] font-normal text-muted-foreground">
              {value}
            </span>
          )
          : null}
      </dt>
      <dd className="text-[11.5px] leading-5 text-muted-foreground">
        {detail}
        {action
          ? (
            <button
              type="button"
              onClick={action.onClick}
              className="ml-1.5 rounded-[6px] text-[11.5px] font-medium text-primary underline-offset-2 hover:underline"
            >
              {action.label}
            </button>
          )
          : null}
      </dd>
    </div>
  );
}
