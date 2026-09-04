/**
 * The roster as a value, apart from the form that edits it -- nanoinfraorg/nanoinfra#262.
 *
 * Every function here is pure, and that is the point: the write path replaces the **whole** roster,
 * so the interesting rules are all about what a save carries rather than about what a dialog looks
 * like. Creating an agent has to send the agents it did not touch; editing one has to send the
 * fields the form never showed; deleting one is the roster without it. Each of those is a
 * one-expression claim that can be asserted without rendering anything.
 *
 * Validation is deliberately absent. The rules -- a delegate must name an agent that exists, an
 * agent may not list itself, a name has to survive being an `@agent:<name>` token -- live in
 * `nanoinfra/config/schema.py`, and a second copy here would be a copy that drifts. The one
 * exception is self-delegation, which the editor refuses by never offering the choice: a control
 * whose only outcome is a refusal is a bad control, not a validation.
 */
import type {
  AgentDefaultsSaveRequest,
  AgentDefaultsValues,
  NamedAgentRosterEntry,
  NamedAgentsSaveRequest,
  NamedAgentValues,
  SettingsPayload,
} from "@/lib/types";

/**
 * The model line under an agent's name, or the fact that there is not one.
 *
 * Nothing ships a model any more (#266), so `agent.model` is genuinely empty on a deployment that
 * has added no configuration -- and `" · auto"` is what that rendered as before this existed. An
 * empty model is not a formatting problem, it is the deployment's state, and the line says it.
 */
export function deploymentModelLine(
  model: string,
  provider: string,
  noModelLabel: string,
): string {
  return model.trim() ? `${model} · ${provider}` : noModelLabel;
}

/** One row of `model_presets`: a name, a label, and the model and provider it resolves to. */
type ModelPreset = SettingsPayload["model_presets"][number];

/**
 * A new agent: every field at the value config reads as *inherit the deployment default*.
 *
 * `null` on the narrowing lists, not `[]`. They are the same shape on the wire and opposite in
 * meaning -- `null` declares no ceiling and `[]` declares one that admits nothing -- so a blank
 * form built from `[]` would create an agent that can reach no tool group, no skill and no MCP
 * server, from a dialog that asked three questions and offered no ceiling at all.
 */
export function blankAgentValues(): NamedAgentValues {
  return {
    description: "",
    modelPreset: null,
    toolGroups: null,
    skills: null,
    connectors: null,
    mcpServers: null,
    delegates: [],
    addendum: "",
    promptSections: {},
  };
}

/**
 * One narrowing list as the editor holds it: the wire's three states, kept.
 *
 * `undefined` -- a gateway that does not report the list -- reads as `null` here, because this
 * function only reports what arrived and `gatewayReportsBindings` is what refuses the save.
 */
function declaredList(value: readonly string[] | null | undefined): string[] | null {
  return value == null ? null : [...value];
}

/**
 * One roster row read back as the editor's value.
 *
 * The binding lists are optional on the wire, and absent is **not** the same as empty: a payload
 * older than this editor carries no lists at all, and reading that as an agent with nothing bound
 * would delete the bindings on the next save. `gatewayReportsBindings` is what refuses that save;
 * this function only reports what arrived.
 *
 * `modelPreset` comes from what the agent *declared*, never from the preset that answers for it.
 * Prefilling from the effective one would pin a choice nobody made -- see
 * `model_preset_declared` in `types.ts`. An older payload has only the effective one, and it is
 * shown rather than invented; that payload cannot be saved anyway.
 */
export function agentValuesFromEntry(entry: NamedAgentRosterEntry): NamedAgentValues {
  return {
    description: entry.description ?? "",
    modelPreset: entry.model_preset_declared !== undefined
      ? entry.model_preset_declared
      : entry.model_preset ?? null,
    toolGroups: declaredList(entry.tool_groups),
    skills: declaredList(entry.skills),
    connectors: declaredList(entry.connectors),
    mcpServers: declaredList(entry.mcp_servers),
    delegates: [...(entry.delegates ?? [])],
    addendum: entry.addendum ?? "",
    promptSections: { ...(entry.prompt_sections ?? {}) },
  };
}

/**
 * True when this row carries the bindings themselves, and not only counts of them.
 *
 * **Absent is the test, and `null` is not absent.** An empty array is a real value an agent can
 * hold and must be editable -- it is how an agent says *no group at all*, which is how a
 * coordinator says it must ask a peer. `null` is the other real value: no ceiling declared. Only
 * a row with no `tool_groups` **key** is a payload older than this editor, and a whole-roster
 * write built from it would replace bindings the form never saw with nothing. So the editor says
 * the gateway needs updating, which is the one honest answer: the save is not safe, and nothing
 * the operator types makes it so.
 */
export function gatewayReportsBindings(entry: NamedAgentRosterEntry): boolean {
  return entry.tool_groups !== undefined
    && entry.skills !== undefined
    && entry.connectors !== undefined
    && entry.mcp_servers !== undefined
    && entry.delegates !== undefined;
}

/** The roster as a name-keyed map -- the shape the write path takes. */
function rosterMap(entries: readonly NamedAgentRosterEntry[]): Record<string, NamedAgentValues> {
  const agents: Record<string, NamedAgentValues> = {};
  entries.forEach((entry) => {
    agents[entry.name] = agentValuesFromEntry(entry);
  });
  return agents;
}

/**
 * The roster with one agent created or replaced.
 *
 * Both cases are the same expression, which is why there is no separate `create`: a create is a
 * name the map does not hold yet. The agents that were already there travel unchanged, so adding
 * an agent cannot be the request that emptied a neighbour.
 */
export function rosterWithAgent(
  entries: readonly NamedAgentRosterEntry[],
  name: string,
  values: NamedAgentValues,
): NamedAgentsSaveRequest {
  return { agents: { ...rosterMap(entries), [name]: values } };
}

/** The roster without one agent. There is no delete route: a delete is this map. */
export function rosterWithoutAgent(
  entries: readonly NamedAgentRosterEntry[],
  name: string,
): NamedAgentsSaveRequest {
  const agents = rosterMap(entries);
  delete agents[name];
  return { agents };
}

/**
 * The agents that delegate to this one, so a delete can say what it breaks.
 *
 * Config refuses a roster whose delegate does not exist, so deleting an agent somebody delegates
 * to is refused rather than silently accepted -- and a confirmation that did not say so would send
 * the operator to a 400 they had no way to predict.
 */
export function agentsDelegatingTo(
  entries: readonly NamedAgentRosterEntry[],
  name: string,
): string[] {
  return entries
    .filter((entry) => entry.name !== name && (entry.delegates ?? []).includes(name))
    .map((entry) => entry.name);
}

/**
 * One prompt-section override written, or cleared.
 *
 * Clearing removes the key rather than storing `""`. Both readings load the platform's text, but
 * only one of them says so: config spells *leave this alone* as an absent key, and a stored empty
 * string is a replacement that happens to be empty -- indistinguishable in the file from a
 * deployment that meant to blank the section.
 *
 * Generic over the draft rather than typed to `NamedAgentValues`, because `agents.defaults` now
 * carries `promptSections` too (#265) and the rule about an absent key is the same rule. Two
 * copies of it would be two copies to keep in step.
 */
export function withPromptSection<T extends { promptSections: Record<string, string> }>(
  values: T,
  section: string,
  text: string,
): T {
  const promptSections = { ...values.promptSections };
  if (text.trim()) promptSections[section] = text;
  else delete promptSections[section];
  return { ...values, promptSections };
}

/**
 * One replaceable section's text, stored as a replacement only when it **is** one.
 *
 * The comparison matters and an emptiness test will not do. The editor seeds its box with the text
 * in force, which for a section nobody has replaced is the platform's own -- so opening a section,
 * reading it and saving would otherwise store a verbatim copy of the platform's text under this
 * agent's name. That copy is not a replacement: it is a fork, and from the next upgrade onwards
 * the agent would be running last release's safety notes with nothing on screen saying so.
 *
 * Emptying the box removes the key too, and that is a fact about config rather than a choice made
 * here: `resolve_overrides` in `nanoinfra/agent/prompt_sections.py` drops an override whose text
 * strips to nothing, because `""` is how a config file spells *leave this alone*. An empty
 * section is therefore not a thing this product can store, and the editor says so instead of
 * offering it.
 */
export function withPromptReplacement<T extends { promptSections: Record<string, string> }>(
  values: T,
  section: string,
  text: string,
  platformText: string | null | undefined,
): T {
  if (platformText != null && text === platformText) return withPromptSection(values, section, "");
  return withPromptSection(values, section, text);
}

/**
 * The deployment's own agent read back as its editor's value -- `agents.defaults` (#265, #266).
 *
 * Every field `AgentDefaults` has that a deployment setting does not. Absent keys read as *nothing
 * declared* here and `gatewayReportsAgentDefaults` is what refuses the save: this function only
 * reports what arrived.
 */
export function agentDefaultsValues(agent: SettingsPayload["agent"]): AgentDefaultsValues {
  return {
    addendum: agent.addendum ?? "",
    promptSections: { ...(agent.prompt_sections ?? {}) },
    toolGroups: declaredList(agent.tool_groups),
    skills: declaredList(agent.skills),
    connectors: declaredList(agent.connectors),
    mcpServers: declaredList(agent.mcp_servers),
    delegates: [...(agent.delegates ?? [])],
  };
}

/**
 * True when the payload carries the agent's own fields, and not merely the deployment
 * settings that share the same block.
 *
 * **Absent is the test, not empty.** An empty addendum is a real value, and so is an empty
 * tool-group list -- that one is how config says *no group at all*. A payload with no `addendum`
 * key is a gateway older than `#265`'s write route, and a form built from that reading would show
 * a blank box over a paragraph the deployment actually has, then offer to save the blank. So the
 * panel says the gateway needs updating, which is the only honest answer: nothing the operator
 * types makes that save safe.
 *
 * `skills`, `connectors` and `mcp_servers` are checked too, and by the **`in`** operator rather
 * than by comparing to `undefined`: `null` is a value those keys legitimately carry, and the
 * question here is whether the key arrived at all.
 */
export function gatewayReportsAgentDefaults(agent: SettingsPayload["agent"]): boolean {
  return agent.addendum !== undefined
    && agent.prompt_sections !== undefined
    && "tool_groups" in agent
    && "skills" in agent
    && "connectors" in agent
    && "mcp_servers" in agent
    && agent.delegates !== undefined;
}

/**
 * The write for one edit of `agents.defaults`: **only the fields that changed**.
 *
 * The rule this function exists to keep is the route's own. `agents.defaults` holds twenty-six
 * fields and this editor shows seven, so the route writes the keys a payload carries and leaves
 * the rest alone -- and a client that sent all seven every time would still be correct about those
 * seven while being wrong about nothing. The reason to diff anyway is the *next* field: the moment
 * this form gains or loses one, a full-snapshot write is a write that resets whatever the form no
 * longer models. Sending the diff makes that class of bug unreachable rather than unlikely.
 *
 * A narrowing list travels as `null` when the draft declares none, which is a value and not an
 * omission: it is how a save *removes* a ceiling. Only a key the draft did not change is absent.
 *
 * An empty patch is a real answer -- nothing changed -- and the caller does not send it.
 */
export function agentDefaultsPatch(
  saved: AgentDefaultsValues,
  draft: AgentDefaultsValues,
): AgentDefaultsSaveRequest {
  const patch: AgentDefaultsSaveRequest = {};
  if (draft.addendum !== saved.addendum) patch.addendum = draft.addendum;
  if (JSON.stringify(draft.promptSections) !== JSON.stringify(saved.promptSections)) {
    patch.promptSections = { ...draft.promptSections };
  }
  const lists = [
    ["toolGroups", "toolGroups"],
    ["skills", "skills"],
    ["connectors", "connectors"],
    ["mcpServers", "mcpServers"],
    ["delegates", "delegates"],
  ] as const;
  lists.forEach(([key]) => {
    const next = draft[key];
    if (JSON.stringify(next) === JSON.stringify(saved[key])) return;
    // `null` is sent as `null`; a list is copied so the patch cannot alias the form's state.
    (patch[key] as string[] | null | undefined) = next === null ? null : [...next];
  });
  return patch;
}

/**
 * The tool groups nanoinfra ships, which exist whether or not config mentions them.
 *
 * Mirrors `BUILTIN_GROUPS` in `nanoinfra/agent/tools/groups.py`, and is only reached when the
 * gateway does not report `tool_groups` at all -- the payload carries the built-ins itself, marked
 * `declared: false`, so a gateway that sends the map is the authority and this list is not
 * consulted. Two names is a cheap thing to keep in step; an empty picker on a deployment that has
 * `@servers` today is not.
 */
const BUILTIN_TOOL_GROUPS = ["diagrams", "servers"];

/**
 * The tool groups an editor may offer, and nothing else.
 *
 * Three sources, all of them groups that **exist**: what the gateway reports in `tool_groups` --
 * which carries the declared groups and the built-ins nobody declared in one map -- the built-ins
 * this build knows about, and the names already bound across the roster. There is deliberately no
 * way to type a fourth: a typed group name that matches nothing produces config that gates nothing
 * and reports no error, which is worse than no control at all because it looks like it worked.
 *
 * Only the key and the description are read. What a group *contains* is the Tool groups panel's
 * business, and this reader would not notice that shape changing -- which is the point of taking
 * the slice as `unknown`.
 *
 * Sorted, because three sources concatenated have no order an operator would recognise.
 */
export function toolGroupOptions(
  declared: Record<string, unknown> | undefined,
  entries: readonly NamedAgentRosterEntry[],
): Array<{ name: string; description?: string }> {
  const names = new Set<string>(declared ? Object.keys(declared) : BUILTIN_TOOL_GROUPS);
  entries.forEach((entry) => (entry.tool_groups ?? []).forEach((group) => names.add(group)));
  return [...names]
    .sort((left, right) => left.localeCompare(right))
    .map((name) => {
      // Config's own wording first, then nanoinfra's for a built-in nobody described.
      const row = (declared?.[name] ?? {}) as {
        description?: unknown;
        builtin_description?: unknown;
      };
      const description = [row.description, row.builtin_description]
        .find((text) => typeof text === "string" && text.trim());
      return description ? { name, description: String(description) } : { name };
    });
}

/**
 * The preset that actually answers, given what an agent declared.
 *
 * `null` declared means *inherit*, and what it inherits is the deployment's active preset -- so
 * that is the row this returns rather than nothing. A name this build does not list falls through
 * to `undefined`, because inventing a row for it would be inventing a model.
 */
function effectivePreset(
  declared: string | null | undefined,
  presets: readonly ModelPreset[],
): ModelPreset | undefined {
  if (declared) return presets.find((preset) => preset.name === declared);
  return presets.find((preset) => preset.active) ?? presets.find((preset) => preset.is_default);
}

/**
 * What to call the preset that answers when an agent declares none.
 *
 * The select's sentinel used to read `Deployment default`, and the complaint about it was exact:
 * it reads like the name of a model, and this deployment has no model called that. So the sentinel
 * names what will actually run -- `Deployment default (Kimi General)` -- and this is the part in
 * the brackets.
 *
 * `default` is the synthetic row `settings_api.py` puts first, labelled `Default`; that label is
 * the same non-answer, so the model identifies it instead. Empty when this build lists no presets
 * at all, and the caller then falls back to the bare sentinel rather than to empty brackets.
 */
export function deploymentDefaultLabel(
  declared: string | null | undefined,
  presets: readonly ModelPreset[],
): string {
  const preset = effectivePreset(declared, presets);
  if (!preset) return declared ?? "";
  const label = preset.label.trim();
  if (label && label.toLowerCase() !== "default") return label;
  return preset.model || preset.name;
}

/**
 * The `model - provider` line under an agent's name, or `null` when the preset is not this
 * build's to resolve.
 *
 * A preset *name* is what config stores and what the roster payload carries, and a name is not
 * what an operator is choosing between: two agents on `primary` and `cheap` say nothing until the
 * line says which model each of those is. `resolved_provider` first, because `provider: "auto"` is
 * a rule for picking one rather than the answer.
 */
export function presetModelLine(
  declared: string | null | undefined,
  presets: readonly ModelPreset[],
): { model: string; provider: string } | null {
  const preset = effectivePreset(declared, presets);
  if (!preset) return null;
  return { model: preset.model, provider: preset.resolved_provider || preset.provider };
}

/**
 * True when this agent's prompt is partly its own: an addendum, or a section it replaced.
 *
 * The card's badge, and it is deliberately a fact the payload already carries rather than a
 * status field invented for the slot. A named agent has no enabled/disabled state in config
 * (`NamedAgentConfig` in `nanoinfra/config/schema.py`), so a badge reading `Active` on every card
 * would be decoration that looks like data.
 */
export function hasOwnPrompt(entry: NamedAgentRosterEntry): boolean {
  return entry.has_addendum || Object.keys(entry.prompt_sections ?? {}).length > 0;
}

/** The same question asked of the draft, so the detail header follows an unsaved edit. */
export function draftHasOwnPrompt(values: NamedAgentValues): boolean {
  return Boolean(values.addendum.trim()) || Object.keys(values.promptSections).length > 0;
}
