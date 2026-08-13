# Common Gotchas

## Do not use `ruff format`

`CONTRIBUTING.md` mentions `ruff format`, but **do not run it** — it destroys git blame history. Only `ruff check` should be used.

## Config `${VAR}` References

`config/loader.py` resolves `${VAR}` patterns in `config.json` at load time. This is **not** a shell-like default-value syntax. If the environment variable is missing, `load_config` raises `ValueError` and the agent falls back to default configuration.

Example valid usage:
```json
{ "providers": { "openrouter": { "apiKey": "${OPENROUTER_KEY}" } } }
```

## Windows Is Not Supported

nanoinfra supports Linux and macOS only. The documentation and installer no longer cover Windows.

The platform branches are still in the code and are deliberately left alone — do not treat them as dead code to delete, and do not extend them either:
- `_IS_WINDOWS` in `shell.py` (roughly a dozen call sites: shell selection, process groups, signals, sandbox).
- Shell-launcher normalization for `npx`/`npm`/`pnpm`/`yarn`/`bunx` in `mcp.py`.
- The `win32` branch in `cli/commands.py` that forces `sys.stdout`/`stderr` to UTF-8 at startup.
- `spawn`-vs-`exec` selection in `command/builtin.py`.
- `sys_platform` dependency markers in `channels/matrix/manifest.py` and `channels/telegram/manifest.py`.

Bug reports about Windows behavior are out of scope. Always use `pathlib.Path` for path manipulation regardless; do not assume `/` separators.

## Prompt Templates

Agent system prompts and scenario-specific instructions live in `nanoinfra/templates/` as Jinja2 markdown files (`identity.md`, `platform_policy.md`, `HEARTBEAT.md`, `SOUL.md`, etc.). Changing these files alters agent behavior as directly as changing Python code. They are loaded by `utils/prompt_templates.py`.

Tool descriptions, skills, and replayed session history also shape model behavior. Treat changes to those surfaces like runtime code: keep them narrow, add a focused regression test when possible, and avoid teaching the model to repeat internal markers, local paths, or tool-call text.

## Context Pollution Persists

Anything written into memory, session history, or prompt inputs can be replayed into future LLM calls. Metadata such as timestamps, local media paths, tool-call echoes, and raw fallback dumps must be bounded and sanitized before they become examples for the model to imitate.

## Skills as Extension Point

Built-in skills live in `nanoinfra/skills/` (markdown + YAML frontmatter format). Agent capabilities that are "know-how" rather than code should be added as skills, not hardcoded into the agent loop. External skills can be published to and installed from ClawHub.

## Atomic Session Writes

`agent/memory.py` writes `history.jsonl` atomically (temp file + fsync + rename + directory fsync). This guarantees durability across crashes. Do not replace this with a plain `open(..., "w")` write.
