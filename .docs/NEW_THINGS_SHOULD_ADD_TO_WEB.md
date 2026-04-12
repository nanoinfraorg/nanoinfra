# Pending Web Documentation Updates

Items that need to be synced to `.web/nanobot-web/nanobot-web-page/docs/` when ready.

## Merged (ready to update)

### 1. Anthropic adaptive thinking mode (PR #2882)
- **What changed:** `reasoning_effort` now supports `"adaptive"` in addition to `"low"` / `"medium"` / `"high"`.
  When set to `"adaptive"`, the model decides when and how much to think (supported on claude-sonnet-4-6, claude-opus-4-6).
- **Where to update:**
  - `content.js` → `agents.defaults` reference section → `reasoningEffort` field description:
    current text lists `"low"`, `"medium"`, `"high"`, or `null` — add `"adaptive"`.
  - All 6 locale files (`zh-CN.js`, `zh-TW.js`, `ja.js`, `ko.js`, `es.js`, `fr.js`) → same field.
- **Source:** `nanobot/config/schema.py` line comment, `nanobot/providers/anthropic_provider.py` `_build_kwargs`.

## Not yet merged (update after merge)

### 2. Windows shell / cross-platform exec tool (PR #2926 + PR #2941)
- **What changed:** `exec` tool now works on Windows via `cmd.exe /c`. Environment isolation is
  platform-aware: Unix passes `HOME`/`LANG`/`TERM` (bash -l handles PATH); Windows passes a curated
  set of 15 system variables (`PATH`, `SYSTEMROOT`, `COMSPEC`, `USERPROFILE`, `HOMEDRIVE`,
  `HOMEPATH`, `TEMP`, `TMP`, `PATHEXT`, `APPDATA`, `LOCALAPPDATA`, `ProgramData`, `ProgramFiles`,
  `ProgramFiles(x86)`, `ProgramW6432`) while still excluding secrets. `bwrap` sandbox is gracefully
  skipped on Windows with a warning.
- **Where to update:**
  - `content.js` → Security section → exec tool environment description:
    current text says "only HOME, LANG, TERM" — needs platform-specific note listing the 15 Windows variables.
  - All 6 locale files → same section.
- **Source:** `nanobot/agent/tools/shell.py` `_build_env`, `_spawn`.

### 3. Channel Plugin Guide — Pydantic config requirement (PR #2850)
- **Status:** ✅ Already updated in this batch (v=20260407d).
- Code examples updated to use `WebhookConfig(Base)` Pydantic model.
- Warning note added in all 7 languages explaining `is_allowed()` silent failure with plain dict.

### 4. Telegram location sharing support (PR #2910)
- **What changed:** Telegram channel now handles location messages. When a user shares a
  location pin, coordinates are forwarded to the agent as `[location: lat, lon]` — consistent
  with the existing `[image: ...]` / `[transcription: ...]` conventions. This enables MCP tools
  that accept geo coordinates (maps, weather, nearby search) to be triggered from a Telegram
  location share.
- **Where to update:**
  - `content.js` → Telegram channel section → supported message types:
    current text lists text, images, voice, audio, documents — add location pins.
  - All 6 locale files → same section.
- **Source:** `nanobot/channels/telegram.py` — `filters.LOCATION` in handler, `message.location` extraction in `_on_message`.

### 5. Tool hint formatting for exec paths and dedup (PR #2926)
- **What changed:** Tool hints now fold file paths embedded in `exec` commands instead of blindly
  truncating them mid-path. This includes quoted paths with spaces on Unix and Windows. Consecutive
  hints are also deduplicated by the final formatted hint string, so different arguments are shown
  separately while truly identical calls still fold as `× N`.
- **Where to update:**
  - `content.js` → Agent loop / tool hint display section:
    explain that exec command previews abbreviate embedded paths for readability and that folding
    happens only for repeated identical rendered hints.
  - All 6 locale files → same section.
- **Source:** `nanobot/utils/tool_hints.py`, `tests/agent/test_tool_hint.py`.

### 6. Discord streaming replies enabled by default (PR #2939)
- **What changed:** Discord now supports the streaming reply path used by Telegram, and Discord
  config gains a `streaming` flag that defaults to `true`. This avoids the previous non-streaming
  fallback path that could end in an empty final response with some OpenAI-compatible gateways.
- **Where to update:**
  - `content.js` → Discord channel section → config reference:
    add the `streaming` field, note that it defaults to `true`, and explain it can be disabled to
    force non-streaming replies.
  - All 6 locale files → same section.
- **Source:** `nanobot/channels/discord.py`, `tests/channels/test_discord_channel.py`, `README.md`.

### 7. WebSocket server channel (PR #2964)
- **What changed:** New `websocket` channel that runs a WebSocket server, allowing external clients
  (web apps, CLIs, Chrome extensions, scripts) to interact with the agent in real time via persistent
  connections. Supports streaming (`delta` + `stream_end` events), token-based authentication
  (static tokens and short-lived issued tokens via HTTP endpoint), per-connection sessions,
  TLS/SSL (WSS), and client allow-list.
- **Where to update:**
  - `content.js` → Channels section:
    add a new WebSocket channel subsection covering configuration (`channels.websocket`), wire
    protocol (`ready`, `message`, `delta`, `stream_end` events), authentication modes (static token,
    issued tokens via `tokenIssuePath`), and common deployment patterns.
  - All 6 locale files → same section.
  - README → supported channels list: add WebSocket.
- **Source:** `nanobot/channels/websocket.py`, `docs/WEBSOCKET.md` (comprehensive standalone doc).

### 8. Exec tool `allowed_env_keys` config (PR #2962)
- **What changed:** New `allowed_env_keys` field in `tools.exec` config. Users can list host
  environment variable names (e.g. `["GOPATH", "JAVA_HOME"]`) to selectively forward into the
  sandboxed subprocess. Default is an empty list — no behavior change for existing users. Works
  on both Unix and Windows.
- **Where to update:**
  - `content.js` → Security section → exec tool environment description:
    current text describes the default allow-list (HOME/LANG/TERM on Unix, 15 vars on Windows).
    Add a note about `allowed_env_keys` for passing additional env vars.
  - All 6 locale files → same section.
- **Source:** `nanobot/config/schema.py` (`ExecToolConfig.allowed_env_keys`), `nanobot/agent/tools/shell.py` (`_build_env`).

### 9. Discord proxy support (PR #2960)
- **What changed:** Discord channel config gains `proxy`, `proxy_username`, and `proxy_password`
  fields. When set, the Discord bot connection is routed through the specified HTTP proxy,
  optionally with BasicAuth. Partial credentials (only username or only password) are logged
  as a warning and ignored.
- **Where to update:**
  - `content.js` → Discord channel section → config reference:
    add the three proxy fields, note that `proxy_username`/`proxy_password` are both required
    for auth, and that partial credentials are ignored with a warning.
  - All 6 locale files → same section.
- **Source:** `nanobot/channels/discord.py` (`DiscordConfig`, `DiscordChannel.start`).

### 10. Feishu streaming enhancements: resuming, inline tool hints, done emoji (PR #2993)
- **What changed:** Three Feishu channel improvements:
  1. `doneEmoji` config field — optional completion emoji (e.g. `"DONE"`) added after `reactEmoji` is removed when the bot finishes processing.
  2. `toolHintPrefix` config field — configurable prefix for inline tool hints (default: `🔧`).
  3. Streaming resuming — mid-turn tool calls flush text to the streaming card without closing it, so the next text segment continues on the same card. Tool hints are inlined into active streaming cards instead of sent as separate messages.
- **Where to update:**
  - `content.js` → Feishu channel section → config reference:
    add `doneEmoji` (optional string, emoji name for completion reaction) and `toolHintPrefix` (string, default `🔧`).
    Note streaming resuming behavior for mid-turn tool calls.
  - All 6 locale files → same section.
  - README → already updated in this PR with config example.
- **Source:** `nanobot/channels/feishu.py` (`FeishuConfig.done_emoji`, `FeishuConfig.tool_hint_prefix`, `send_delta` resuming logic, `send` tool hint inline logic).

### 11. Unified session across channels (PR #2900)
- **What changed:** New `unifiedSession` toggle in `config.json` (`agents.defaults`). When set to
  `true`, all incoming messages — regardless of which channel they arrive on — share a single
  session key (`unified:default`). Switching from Telegram to Discord continues the same
  conversation. Defaults to `false` — zero behavior change for existing users. Existing
  `session_key_override` (e.g. Telegram thread) is respected and not overwritten.
- **Where to update:**
  - `content.js` → `agents.defaults` reference section:
    add `unifiedSession` field, type `boolean`, default `false`, explain single-user multi-device
    use case and that it merges all channel sessions into one.
  - All 6 locale files → same section.
  - README → config example or feature list, mention cross-channel unified session.
- **Source:** `nanobot/config/schema.py` (`unified_session`), `nanobot/agent/loop.py` (`UNIFIED_SESSION_KEY`, `_dispatch`).

### 12. Auto compact config rename + recent live suffix retention (PR #3007)
- **What changed:** Auto compact now preserves a recent legal suffix of live session messages while
  summarizing the older unconsolidated prefix, instead of clearing the entire live session. The
  preferred config key is now `idleCompactAfterMinutes`; legacy `sessionTtlMinutes` remains accepted
  as a backward-compatible alias.
- **Where to update:**
  - `content.js` → `agents.defaults` reference section:
    rename the field to `idleCompactAfterMinutes`, note that `sessionTtlMinutes` is a legacy alias,
    and explain that auto compact keeps recent live context instead of replacing the whole session
    with only a summary.
  - All 6 locale files → same section.
  - Any auto-compact behavior notes:
    update wording from "session cleared" to "older context summarized, recent live suffix retained".
- **Source:** `nanobot/config/schema.py` (`AgentDefaults.session_ttl_minutes` aliases),
  `nanobot/agent/auto_compact.py` (`_split_unconsolidated`, `_archive`), `README.md` Auto Compact section.

### 13. Kagi web search provider (PR #2945)
- **What changed:** `tools.web.search.provider` now accepts `kagi`, using `apiKey` / `KAGI_API_KEY`
  to call Kagi's Search API through the built-in `web_search` tool.
- **Where to update:**
  - `content.js` → web tools / search provider section:
    add `kagi` to the provider list, note that it uses the standard `apiKey` field or `KAGI_API_KEY`.
  - All 6 locale files → same section.
  - Any provider comparison tables:
    add Kagi alongside Brave, Tavily, Jina, SearXNG, and DuckDuckGo.
- **Source:** `nanobot/agent/tools/web.py` (`_search_kagi`),
  `nanobot/config/schema.py` (`WebSearchConfig.provider` comment), `README.md` web tools section.

### 14. Mid-turn follow-up injection for active agent runs (PR #3042)
- **What changed:** If a user sends another message while the agent is still working on the same
  session, the follow-up can now be injected into the current agent turn instead of waiting behind
  the per-session lock as a separate later turn. Streaming channels keep the active reply open when
  the turn resumes, so the follow-up answer can continue in the same live response flow.
- **Where to update:**
  - `content.js` → agent loop / streaming behavior section:
    explain that same-session follow-ups during an active turn may be folded into the in-flight
    response instead of always starting a brand-new queued turn.
  - All 6 locale files → same section.
- **Source:** `nanobot/agent/loop.py` (`_pending_queues`, unified-session routing, leftover re-publish),
  `nanobot/agent/runner.py` (injection checkpoints, resumed stream end handling).

### 15. Disable built-in/workspace skills via config (PR #2959)
- **What changed:** New `disabledSkills` field under `agents.defaults`. Users can provide a list of
  skill directory names to exclude from loading, so selected built-in or workspace skills no longer
  appear in the main agent or subagent skill summaries and are not auto-injected as always-on skills.
- **Where to update:**
  - `content.js` -> `agents.defaults` reference section:
    add `disabledSkills` as an array of skill names, explain that names match skill directory names,
    and note that disabled skills are hidden from both the main agent and subagents.
  - All 6 locale files -> same section.
- **Source:** `nanobot/config/schema.py` (`AgentDefaults.disabled_skills`),
  `nanobot/agent/context.py` (`ContextBuilder`), `nanobot/agent/subagent.py` (`SubagentManager._build_subagent_prompt`),
  `nanobot/agent/skills.py` (`SkillsLoader` filtering).
