# Changelog

All notable changes to nanoinfra are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A line names an effect a user can observe and carries a reference. The reasoning behind a change —
what was measured, what was wrong first — lives in the issue or the proposal that line points at,
not here.

## [Unreleased]

## [2.0.4] — 2026-09-04

### Fixed

- `@agent:<name>` in a message answers as that agent. The composer offered the token, completed
  it, and then ignored it: the turn ran as the deployment default, so naming an agent looked like
  it did nothing. ([#269](https://github.com/nanoinfraorg/nanoinfra/issues/269))

## [2.0.3] — 2026-09-04

### Fixed

- The WebUI inbox is no longer warned about as an unreachable chat channel. A deployment whose
  approver sits there was told on every poll that a suspended action reaches nobody, while the
  inbox had the request. ([#267](https://github.com/nanoinfraorg/nanoinfra/issues/267))

## [2.0.2] — 2026-09-04

### Fixed

- The `Agents` destination is in the sidebar whether or not the deployment names an agent. It was
  gated on the roster, so on a fresh install nothing led to the page that configures the agent
  answering every turn. ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))

### Changed

- `Abilities` is no longer gated on the roster either, and it opens expanded — one sidebar shape
  whatever the deployment holds, with Apps and Skills still one click away.
  ([#253](https://github.com/nanoinfraorg/nanoinfra/issues/253))

## [2.0.1] — 2026-09-04

### Fixed

- nanoinfra imports on Python 3.11 again. A dataclass field on `RequestContext` defaulted to a
  `mappingproxy`, which 3.11 refuses for any default whose type is unhashable — so 2.0.0 failed at
  import on the minimum supported version.
  ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))

## [2.0.0] — 2026-09-04

### Added

- One approval can become a standing grant. The grant is derived from the payload the executor
  actually rendered, defaults to expiring, and asks once more before it never does.
  ([#217](https://github.com/nanoinfraorg/nanoinfra/issues/217))
- Every server keeps notes the agent and the operator both write, and a turn that names a server
  reads them. A note does not expire; one that disagrees with what you see is evidence the
  infrastructure changed. ([#222](https://github.com/nanoinfraorg/nanoinfra/issues/222))
- A queryable record of every tool call: which tool, by whom, what the gate decided, and how it
  ended. It stores the *address* of the arguments in the session history, never the arguments.
  ([#231](https://github.com/nanoinfraorg/nanoinfra/issues/231))
- The agent can search your own documents and answer with citations. Drop files in
  `<workspace>/knowledge/`; nothing reaches a prompt on a turn that does not ask.
  ([#237](https://github.com/nanoinfraorg/nanoinfra/issues/237))
- A deployment can name more than one agent, each with its own model, tools, skills and
  instructions. An empty `agents.named` is exactly the single agent every deployment has today.
  ([#247](https://github.com/nanoinfraorg/nanoinfra/issues/247))
- An agent can hand one task to a peer and wait for its answer. Membership in
  `agents.named[x].delegates` is the grant, and delegation is one level deep.
  ([#250](https://github.com/nanoinfraorg/nanoinfra/issues/250))
- A delegated action records the human who asked, the agent that delegated and the peer that
  acted, so a reader can answer "who authorised this" without opening a second file.
  ([#251](https://github.com/nanoinfraorg/nanoinfra/issues/251))
- Every assistant turn says which agent answered it, beside the turn's cost.
  ([#248](https://github.com/nanoinfraorg/nanoinfra/issues/248))
- The composer offers the agents a message may ask for, as `@agent:<name>`. The token stays in the
  text, because it is a preference the answering agent reads and not an invocation.
  ([#255](https://github.com/nanoinfraorg/nanoinfra/issues/255))
- A turn that delegates shows its plan as one object in the thread — a row per peer with its
  outcome and its own cost — and a reload shows the same plan. ([#252](https://github.com/nanoinfraorg/nanoinfra/issues/252))
- An Agents destination lists the agents a deployment names, and an Abilities grouping collects
  Apps and Skills in the menu. ([#253](https://github.com/nanoinfraorg/nanoinfra/issues/253))
- Each agent's Prompt tab shows the prompt's sections with the permission on each, and the
  addendum that appends after them. ([#256](https://github.com/nanoinfraorg/nanoinfra/issues/256))
- An automation can name the agent it runs as, and that agent is the ceiling: its tool groups cap
  the turn and its skills bound the job's own picker. ([#257](https://github.com/nanoinfraorg/nanoinfra/issues/257))
- The approvals inbox names the agent that will act, and the agent that delegated to it.
  ([#258](https://github.com/nanoinfraorg/nanoinfra/issues/258))
- Agents are created, edited and deleted in the browser. Each one gets a page with tabs — model,
  tools, skills, delegates, prompt — and every binding is picked from what this deployment has.
  ([#262](https://github.com/nanoinfraorg/nanoinfra/issues/262))
- The deployment's own agent is one more agent: same page, same tabs, editable down to its skills,
  MCP servers and delegates. It still cannot be deleted.
  ([#265](https://github.com/nanoinfraorg/nanoinfra/issues/265),
  [#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))
- The Prompt tab edits the prompt. The three sections that are prose can be replaced, each with the
  text in force shown and what replacing it costs said before you do.
  ([#256](https://github.com/nanoinfraorg/nanoinfra/issues/256))
- Settings → Prompts reads and writes the two prompts that run unattended, `dream` and
  `evaluator`. ([#264](https://github.com/nanoinfraorg/nanoinfra/issues/264))
- An agent's tool groups, skills, MCP servers and connectors now narrow **every** turn it answers,
  not only a scheduled one — so choosing an agent is how a conversation stops paying for every
  server and skill installed. ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))

### Changed

- Where a deployment names agents, the composer chooses an agent instead of a model — the model
  belongs to the agent. A deployment that names none keeps its model selector unchanged.
  ([#254](https://github.com/nanoinfraorg/nanoinfra/issues/254))
- The prompt's safety notes are their own fixed section, so replacing the runtime section can no
  longer delete them. ([#256](https://github.com/nanoinfraorg/nanoinfra/issues/256))
- A replaced prompt section is still named in the prompt manifest and marked as overridden — a
  record that hid a replacement would make two different prompts look identical.
  ([#256](https://github.com/nanoinfraorg/nanoinfra/issues/256))
- The executor protocol is version 7, carrying the delegation chain. A deployment running the
  executor as a separate process must restart it alongside the gateway. ([#251](https://github.com/nanoinfraorg/nanoinfra/issues/251))
- **Nothing ships a model.** `agents.defaults.model` was `anthropic/claude-opus-4-5`, so an
  unconfigured deployment looked configured on a provider it had no credential for. The first
  model configuration a deployment adds is now the primary one, and it answers until something
  else is chosen. ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))
- An agent's empty binding list means *none of them*, not *all of them*. Declaring nothing is what
  means everything, and the two are now separate answers everywhere: config, the tool filter and
  the picker. ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))
- A persona can write `{{ agent_name }}`, `{{ agent_role }}` or `{{ agent_description }}` in
  `SOUL.md` and each agent fills it in. Any other `{{ }}` survives verbatim.
  ([#265](https://github.com/nanoinfraorg/nanoinfra/issues/265))

### Removed

- The config warning about a dead `agents.defaults.model`. The turn fell back to the primary preset
  either way, so the line reported a difference nothing acts on — and the field ships empty now.
  ([#205](https://github.com/nanoinfraorg/nanoinfra/issues/205))

### Fixed

- A system job kept its state across a restart. `dream` and `evaluator` were re-registered as
  brand new on every boot, so on a deployment that restarts they never ran.
  ([#263](https://github.com/nanoinfraorg/nanoinfra/issues/263))
- An agent's addendum and its replaced prompt sections reach the model. Both were stored, shown,
  editable — and never passed to the prompt builder by its only call site.
  ([#265](https://github.com/nanoinfraorg/nanoinfra/issues/265))
- A tool refused for want of a secret says when the encryption key is the wrong one, instead of
  failing silently. ([#217](https://github.com/nanoinfraorg/nanoinfra/issues/217))
- A trigger whose agent declared no tool groups is no longer capped to the ungrouped tools.
  ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))
- The default agent has a card on a fresh install. It hid until the deployment named an agent, so
  the agent that answers every turn was reachable only after naming one you did not want.
  ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))
- A deployment with no model at all says so at boot and on the settings page, instead of failing
  a turn with `No provider is configured for model ''`.
  ([#266](https://github.com/nanoinfraorg/nanoinfra/issues/266))

### Security

- A delegated turn can no longer reach a capability class the turn that spawned it did not hold,
  and a capped turn hands that ceiling to anything it spawns. ([#251](https://github.com/nanoinfraorg/nanoinfra/issues/251))

## [1.9.1] — 2026-09-03

### Removed

- The sidebar no longer lists conversations from other channels, reverting 1.9.0. Sessions that
  never held a conversation — an empty WhatsApp session, `cli:direct` — showed up as `New topic`
  rows and buried the chats, which is worse than the problem it set out to fix.
  ([#216](https://github.com/nanoinfraorg/nanoinfra/issues/216))

## [1.9.0] — 2026-09-03

### Added

- A conversation held over the API — or from a chat channel, or by an automation — appears in the
  WebUI sidebar with its channel badged, and opens as a readable thread built from the session
  history. Read-only: the composer still refuses a session it does not own, and delete,
  file-preview and automations stay closed to other channels.
  ([#216](https://github.com/nanoinfraorg/nanoinfra/issues/216))

## [1.8.0] — 2026-09-03

### Added

- `api.enabled` lets the gateway serve `/v1` on `api.port` itself: one process and one agent loop
  instead of two, sharing the MCP and connector hosts it already booted. Default `false`, so
  upgrading opens no port. `nanoinfra serve` is unchanged.
  ([#214](https://github.com/nanoinfraorg/nanoinfra/issues/214))
- The API server logs one line per request — method, path, status, duration, and why a request was
  refused — and its own logger stays audible without `--verbose`. Never the body or the
  `Authorization` header.
  ([#215](https://github.com/nanoinfraorg/nanoinfra/issues/215))

## [1.7.4] — 2026-09-03

### Fixed

- `nanoinfra serve` drains the agent's outbound event bus, so a turn that emits more than a
  thousand progress or stream events finishes instead of stalling mid-flight and leaving its HTTP
  request unanswered. `gateway` and `nanoinfra agent` already drained; the API server did not.
  ([#211](https://github.com/nanoinfraorg/nanoinfra/issues/211))

## [1.7.3] — 2026-09-02

### Fixed

- Both API routes accept a client that resends its own transcript: what follows the last assistant
  message is the turn, and earlier messages are dropped as the client's copy of a history the
  server already keeps. A `system` message beside the prompt is joined into the turn rather than
  refused, so a Responses or Chat Completions client no longer gets a 400 on its first request.
  ([#211](https://github.com/nanoinfraorg/nanoinfra/issues/211))

## [1.7.2] — 2026-09-02

### Added

- The prompt breakdown says which request it describes when a turn made more than one, and names
  the largest request the turn reached.
  ([#208](https://github.com/nanoinfraorg/nanoinfra/issues/208))
- An activity cluster names what the provider calls inside it cost — input, cache share and output
  — beside the duration it already showed. The cache share counts only the calls that reported
  one. ([#208](https://github.com/nanoinfraorg/nanoinfra/issues/208))
- `tools.groups` declares groups of built-in tools with the `always | mention` modes an MCP server
  and a connector already had, so `@diagrams` can load 2,438 tokens of diagram schemas only for a
  turn that asks for them. `diagrams` and `servers` are predefined; both default to `always`.
  ([#210](https://github.com/nanoinfraorg/nanoinfra/issues/210))
- `POST /v1/responses` answers the same agent over the Responses wire, so a client that defaults
  to that protocol no longer gets a 404. The caller's `tools` and `instructions` are ignored: the
  agent runs its own tools behind the capability gate.
  ([#211](https://github.com/nanoinfraorg/nanoinfra/issues/211))
- `agents.defaults.midTurnMessages` decides what happens to a message that arrives while a turn is
  running. The new default, `queue`, gives it a turn of its own with its own answer; `inject` keeps
  the previous behaviour of folding it into the turn in flight.
  ([#209](https://github.com/nanoinfraorg/nanoinfra/issues/209))

### Changed

- A message sent while the agent is still working now gets its own turn and its own reply, instead
  of being folded into the turn already running and answered only as part of it.
  ([#209](https://github.com/nanoinfraorg/nanoinfra/issues/209))

### Fixed

- Each activity cluster reports its own duration instead of the whole turn's, so eight consecutive
  steps no longer all read the same figure.
  ([#208](https://github.com/nanoinfraorg/nanoinfra/issues/208))

## [1.7.1] — 2026-09-02

### Added

- `/compact` archives a session's history on request instead of waiting for an idle timer or a
  budget threshold, and reports how many messages it archived and how many stay raw.
  ([#212](https://github.com/nanoinfraorg/nanoinfra/issues/212))

## [1.7.0] — 2026-09-01

### Added

- OpenAI requests carry `prompt_cache_key`, one key per chat, so its automatic prefix cache is
  reached instead of every session landing in the bucket its first 256 tokens hash to. Opt-in per
  provider: it is a body field, and a provider that rejects an unknown one answers 400.
- `google_calendar_freebusy` answers whether a calendar is busy in a range — busy blocks only,
  no event detail — so availability and slot-finding no longer mean reading every event. A
  connector operation can now declare `read_via_post` for a read whose query needs a POST body;
  the invariant that a POST is a write otherwise still holds.
- `google_calendar_update_event` changes an event by id. A partial PATCH, so an omitted field
  keeps its value — sending only `start`/`end` moves an event and keeps the rest. `mutate.remote`.
- `google_calendar_delete_event` removes an event by id. It carries `mutate.remote`, the same class
  as creating one, so it asks a person in an interactive turn and needs a standing grant to run
  unattended.

## [1.6.1] — 2026-09-01

### Fixed

- Every turn of one chat reaches the same xAI server, so its prompt prefix can be cached.
  `x-grok-conv-id` was a fresh UUID per request, which announced each call as a new conversation.

### Added

- `CHANGELOG.md`, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form, covering every
  version since 1.0.0. A release body is now this file's section for that version.
- A tag whose version has no changelog section fails the release workflow.

## [1.6.0] — 2026-09-01

### Added

- The prompt breakdown's tool count opens the list of tools behind it, each with its size, largest
  first. ([#203](https://github.com/nanoinfraorg/nanoinfra/issues/203))

### Changed

- A tool row in the breakdown reads as `google-calendar` with a `connector` tag, rather than as the
  raw `connector:google-calendar`. ([#203](https://github.com/nanoinfraorg/nanoinfra/issues/203))

## [1.5.2] — 2026-09-01

### Fixed

- Every reference prefix keeps a row in the mention menu. The palette reserved two, so a deployment
  with a data connector — `server:`, `diagram:`, `calendar:` — lost one.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))

## [1.5.1] — 2026-09-01

No user-visible changes. A route test pinned the arguments the marketplace search is called with,
and 1.5.0 added one.

## [1.5.0] — 2026-09-01

Requires skills-server v0.4.0.

### Added

- **Apps → Connectors browses the catalog and installs from it.** Each row names every operation
  with its capability class, the hosts a token could reach, and the scopes it would carry, before
  the install button. ([#207](https://github.com/nanoinfraorg/nanoinfra/issues/207))
- An install reports what is still missing: a connector has no credential and no entry in
  `connectors.active` until an operator adds them.
  ([#207](https://github.com/nanoinfraorg/nanoinfra/issues/207))

### Changed

- The marketplace reads the catalog's `kind` and installs each package where its own subsystem
  reads it — `skills/`, `plugins/`, `connector-packages/`. The kind is asked of the catalog and
  never taken from the caller. ([#207](https://github.com/nanoinfraorg/nanoinfra/issues/207))

### Fixed

- A connector already on disk read as *not installed*, because `installed` was asked of the skills
  loader for every kind. ([#207](https://github.com/nanoinfraorg/nanoinfra/issues/207))

### Security

- Workspace containment runs before anything walks the install destination. A symlinked `skills/`
  was read through before the boundary check.
  ([#207](https://github.com/nanoinfraorg/nanoinfra/issues/207))

## [1.4.0] — 2026-09-01

### Added

- **`attach: "always" | "mention"` on a data connector.** A `mention` connector stays active with
  its credential and grants, the prompt carries one line naming it, and its operations load only
  for a turn that names it — `@<name>`, or a `@<kind>:<id>` object of one of its kinds.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))
- An automation declares `connectors: [...]`, since an unattended turn types no `@`.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))

### Fixed

- The composer palette silently dropped candidates whose kind was missing from its group list.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))
- A paused MCP server named in message text was still tokenised and still sent.
  ([#206](https://github.com/nanoinfraorg/nanoinfra/issues/206))

## [1.3.2] — 2026-09-01

### Fixed

- The connector mention prefix reads the cached object listing, so `@calendar:` is in the menu on
  the first keystroke instead of waiting on a live API call that took seconds and failed silently.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))

## [1.3.1] — 2026-09-01

### Fixed

- A paused MCP server is no longer offered in the `@` palette. Picking one read as an attachment on
  a server nanoinfra never connected. ([#206](https://github.com/nanoinfraorg/nanoinfra/issues/206))

## [1.3.0] — 2026-09-01

### Added

- **`attach` on an MCP server.** A `mention` server is advertised in one line — name, tool count,
  how to attach — roughly 50 tokens against a couple of thousand, with its schemas sent only to a
  turn that names it. ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))
- An automation declares `mcpPresets: [...]`.
  ([#204](https://github.com/nanoinfraorg/nanoinfra/issues/204))
- Config that is ignored says so, at boot in the log and on the Models page: a dead
  `agents.defaults.model` beside a `modelPreset` that overrides it, and a selected preset whose
  provider has no credential. ([#205](https://github.com/nanoinfraorg/nanoinfra/issues/205))
- The Apps page's tool count opens the tool list.

### Changed

- The Apps page opens on **Ready** rather than the CLI tab.

### Fixed

- A paused row counts its saved allowlist instead of reporting `0 tools` for a server holding
  fifteen.

## [1.2.3] — 2026-09-01

### Fixed

- **Pausing an MCP server stops it connecting.** `AgentLoop.from_config` seeded the live map from
  the unfiltered config, so a paused server reconnected on the next restart.
  ([#206](https://github.com/nanoinfraorg/nanoinfra/issues/206))
- A plugin-declared MCP server reached the loop only after a hot reload.
  ([#140](https://github.com/nanoinfraorg/nanoinfra/issues/140))

## [1.2.2] — 2026-09-01

### Changed

- The MCP pause is a switch on the row; the row's checkmark becomes an explicit `⋯` menu, and the
  second line names what the server costs instead of naming its transport.
  ([#206](https://github.com/nanoinfraorg/nanoinfra/issues/206))

## [1.2.1] — 2026-09-01

### Added

- **An MCP server can be paused without losing its configuration** — `enabled: false`. Its command,
  arguments, environment, headers and `enabledTools` list stay; its schemas leave every prompt.
  ([#206](https://github.com/nanoinfraorg/nanoinfra/issues/206))

## [1.2.0] — 2026-08-31

### Added

- **A per-turn prompt breakdown**, collapsed under each turn: where the input tokens went, by
  section and by tool source, largest first. Recorded while the prompt is assembled, because the
  attribution is gone by the time a request reaches a provider. Names and sizes only, never
  content. ([#203](https://github.com/nanoinfraorg/nanoinfra/issues/203))

## [1.1.4] — 2026-08-31

### Added

- Settings leads with the token numbers: 30-day tokens, calls, failures, today, the measured share,
  the peak day, and a per-model breakdown with time to first token.
  ([#176](https://github.com/nanoinfraorg/nanoinfra/issues/176),
  [#177](https://github.com/nanoinfraorg/nanoinfra/issues/177))

### Fixed

- A migrated day topped the per-model breakdown with a day's worth of tokens against 19 "calls".
  It counts in the day totals and not in a breakdown it cannot answer.
- Every fallback row was labelled `fallback` rather than the provider that actually answered.

## [1.1.3] — 2026-08-31

### Fixed

- A connector's stale `last_error` can be retracted. A successful test or a successful listing
  clears it; every success had been passing an empty value, which the merge ignores by design.

## [1.1.2] — 2026-08-31

### Fixed

- **Every connector call failed with `[Errno 13] Permission denied`** on any deployment that had
  activated one. Marketplace package discovery pointed at the connector *state* directory, which
  the executor cannot traverse, and `Path.is_file()` propagates a permission error rather than
  answering `False`.
- The connector row in Apps spans both grid columns, so its name is readable and its capability
  classes read as chips rather than as a vertical wall.

### Changed

- Connector packages get their own root, `<workspace>/connector-packages/`. A package is read by
  three accounts; a state file is written by one.

## [1.1.1] — 2026-08-31

### Added

- A connector against a public API runs with no credential — `credential.kind: "none"` activates
  with no binding, mints nothing and sends no `Authorization` header. It is still gated per
  operation.
- `examples/connectors/hello-world/`: one `read` against a public API, no credential, no setup.
  Ships in the sdist, because a `pip` user has no checkout to copy from.

## [1.1.0] — 2026-08-31

### Added

- **Every finished turn shows its cost in the footer** — tokens in, out, cache share and latency —
  persisted beside the latency so a reloaded thread shows what the live turn showed.
- **One content-free row per provider attempt** in `~/.nanoinfra/llm-usage.sqlite3`, retained 400
  days. No prompts, no responses, no reasoning, no tool payloads, no session keys; a test asserts
  the schema has no column content could go in.
- **A connector can arrive from the catalog** as a declarative `connector.json` with nothing
  importable, and its calls run in a fourth confined process (`nanoinfra-connector`, outbound 443
  only) whose group holds the executor and not the agent.

### Changed

- `LLMResponse.usage` and both hook contexts carry `LLMUsage | None` instead of a dict.
  `to_turn_dict()` is the projection the OpenAI-compatible API, the SDK and the WebUI read.
- `~/.nanoinfra/webui/token-usage.json` is migrated at first start and renamed, not deleted.

### Fixed

- Three usage-accounting defects the typed contract exposed: a cache count present on one call and
  absent on another summed as though both were measured; two calls carrying 40k of context each read
  as a turn carrying 80k; the over-budget finalisation path mutated the accumulator through its
  argument.
- Three call sites reached a provider without passing through the agent loop — WebUI title
  generation, the evaluator, Dream consolidation — so their tokens were charged by the provider and
  counted by nothing.

### Security

- A connector credential is bound to the hosts it may address, checked at activation with both
  hosts named. A package declaring Google scopes and its own `baseUrl` would otherwise receive a
  live Google token and a process that forwards it.

## [1.0.6] — 2026-08-31

### Fixed

- A secret record written through the executor took the writer's primary group, so a group-readable
  file in the wrong group answered `EACCES` to the gateway: a create succeeded and the next listing
  raised. The store sets the group explicitly when the directory shares one.

## [1.0.5] — 2026-08-31

### Added

- **The Secrets page writes on a container deployment.** Protocol version 6 carries a third request
  kind: the gateway encrypts because it holds the key, and the executor writes ciphertext it cannot
  read. Three verbs — create, update, delete — and no read.
- **Connector consent completes in the browser**, returning to this deployment's own origin with
  the credential and the activation written. A consent that fails writes nothing.
- **Reload connectors** reconciles the running registry with config, without a restart.

### Fixed

- The no-tools request path had no timeout while the tool-carrying path wrapped the same call, so a
  stall held the per-session lock forever.
- A truncated consolidation was accepted as history, taking whatever it had not reached with it.
- `find_files` and `grep` bounded their results and nothing about their scan, and walked inside an
  async `execute` — one call on a slow filesystem held the event loop for every session. Both carry
  an entry budget and a wall clock, and a partial answer says so.
- A cron job froze the workspace path one person's turn resolved, naming their identity directory,
  into `jobs.json`.

### Security

- A Slack file download followed any URL the workspace handed it, redirects included, with no SSRF
  validation and no DNS pinning.

## [1.0.4] — 2026-08-30

### Added

- **Data connectors.** A connector reaches one data source with a **capability class per
  operation**, so reading a calendar and writing to it are two decisions — where an MCP tool
  declares nothing and every one of them resolves to the fail-closed `mutate.remote`.
- `google-calendar` ships first, with three `read` operations and one `mutate.remote`.
- The call runs in the executor: protocol version 5 carries a second request kind, and the method,
  path, class and scopes come from the installed manifest — so a frame cannot describe a call the
  package never declared, and the agent process holds no token.
- A standing grant can name a connector, so an unattended write is expressible rather than
  permanently denied.

## [1.0.3] — 2026-08-29

### Fixed

- The test that pinned the previous dependency policy now states the current one. v1.0.2 moved four
  packages into the base install and left an assertion saying the opposite, so the release shipped
  and CI failed.

## [1.0.2] — 2026-08-29

### Changed

- `asyncssh`, `ansible-runner`, `boto3` and `aiohttp` are base dependencies. The container image
  installs no extras, so the published image came up as an agent for infrastructure that could not
  reach infrastructure. `nanoinfra[servers]` and `nanoinfra[api]` still resolve.

## [1.0.1] — 2026-08-29

No behaviour changes. A `cast()` that narrowed nothing failed strict typing, so this tag is a tree
that passes every check.

## [1.0.0] — 2026-08-29

### Added

- **A verified identity gets its own workspace, its own sessions, and a way out.** The workspaces
  root becomes that person's own directory, which narrows the switcher, the sidebar and every
  session key at once. Storage keys on `(issuer, subject)` and never on the address.
- The sidebar names the signed-in person and offers **Sign out** when `signOutPath` is set.
- A personal workspace is seeded like any other, once. The credential store stays out.
- **The container image is published**: `ghcr.io/nanoinfraorg/nanoinfra`.
- `examples/auth/` runs behind Caddy, with the three details that do not fail loudly written down.

### Security

- Another person's workspace answers `403 that workspace is not yours`; so does the shared
  `default`. Somebody else's session answers `404` rather than `403`, because a 403 says it exists.

## Before 1.0.0

Thirty-four tags ending at v0.17.5, from before this repository took its current shape and mixed
with upstream imports. That history lives in the
[release archive](https://docs.nanoinfra.org/release-archive) and on the
[releases page](https://github.com/nanoinfraorg/nanoinfra/releases).

[Unreleased]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.9.1...HEAD
[1.9.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.9.0...v1.9.1
[1.9.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.7.4...v1.8.0
[1.7.4]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.7.3...v1.7.4
[1.7.3]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.7.2...v1.7.3
[1.7.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.7.1...v1.7.2
[1.7.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.6.1...v1.7.0
[1.6.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.5.2...v1.6.0
[1.5.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.3.2...v1.4.0
[1.3.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.1.4...v1.2.0
[1.1.4]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.6...v1.1.0
[1.0.6]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/nanoinfraorg/nanoinfra/releases/tag/v1.0.0
