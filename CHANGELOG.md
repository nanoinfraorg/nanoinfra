# Changelog

All notable changes to nanoinfra are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A line names an effect a user can observe and carries a reference. The reasoning behind a change —
what was measured, what was wrong first — lives in the issue or the proposal that line points at,
not here.

## [Unreleased]

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

[Unreleased]: https://github.com/nanoinfraorg/nanoinfra/compare/v1.8.0...HEAD
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
