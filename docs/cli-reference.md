# CLI Reference

Use this page when you know what you want to run and need the command shape. For a guided first run, start with [`quick-start.md`](./quick-start.md).

## Choose a Command

| Goal | Command | Notes |
|---|---|---|
| Check the install | `nanoinfra --version` | If this fails, try `python -m nanoinfra --version` |
| Create or refresh config | `nanoinfra onboard` | Creates `~/.nanoinfra/config.json` and `~/.nanoinfra/workspace/` |
| Refresh config non-interactively | `nanoinfra onboard --refresh` | Preserves existing values and adds missing default fields without prompting |
| Use guided setup | `nanoinfra onboard --wizard` | Best when you prefer prompts over hand-editing JSON |
| Open the browser workbench | `nanoinfra webui` | Prepares local WebUI settings, starts the gateway, and opens the browser |
| Check readiness without calling a model | `nanoinfra status` | Summarizes config/workspace and validates the active provider/model configuration |
| Send one test message | `nanoinfra agent -m "Hello!"` | First proof that install, config, provider, model, and workspace all work |
| Chat in the terminal | `nanoinfra agent` | Interactive local chat; exit with `exit`, `/exit`, `:q`, or `Ctrl+D` |
| Run the gateway directly | `nanoinfra gateway` | Service/ops command for WebUI, chat apps, cron, and heartbeat |
| Deliver a local trigger | `nanoinfra trigger <id> "message"` | Created first with `/trigger <name>` in the target chat/session |
| Serve an OpenAI-compatible API | `nanoinfra serve` | Starts `/v1/chat/completions`, `/v1/models`, and `/health` |
| Check chat channel setup | `nanoinfra channels status` | Useful before starting `nanoinfra gateway` |
| Manage optional features | `nanoinfra plugins list` | Shows channels and optional capabilities you can turn on |
| Log in to QR/OAuth-style channels | `nanoinfra channels login <channel>` | Used by channels such as WhatsApp and WeChat |
| Log in to OAuth model providers | `nanoinfra provider login <provider>` | Used by OpenAI Codex, xAI subscription, and GitHub Copilot providers |

## Global

```bash
nanoinfra --help
nanoinfra --version
python -m nanoinfra --help
python -m nanoinfra --version
```

`python -m nanoinfra ...` is useful when the package is installed but the `nanoinfra` script is not on `PATH`.

## Common Patterns

Most day-to-day commands use the default config and workspace. Advanced or multi-instance runs usually pass both paths explicitly:

```bash
nanoinfra agent --config ./bot-a/config.json --workspace ./bot-a/workspace -m "Hello"
nanoinfra gateway --config ./bot-a/config.json --workspace ./bot-a/workspace
nanoinfra serve --config ./bot-a/config.json --workspace ./bot-a/workspace
```

Use `--verbose` on long-running processes when you need startup or runtime logs:

```bash
nanoinfra gateway --verbose
nanoinfra serve --verbose
```

Long-running commands keep working until you stop them. Press `Ctrl+C` in that terminal
to stop foreground `nanoinfra gateway` or `nanoinfra serve`. If you started the gateway
with `--background`, use `nanoinfra gateway stop`.

## Setup

| Command | Description |
|---|---|
| `nanoinfra onboard` | Initialize or refresh the default config and workspace |
| `nanoinfra onboard --refresh` | Refresh an existing config without prompting, preserving existing values |
| `nanoinfra onboard --wizard` | Use the interactive setup wizard |
| `nanoinfra onboard --config <path> --workspace <path>` | Initialize or refresh a specific instance |

Default paths:

| Path | Default |
|---|---|
| Config | `~/.nanoinfra/config.json` |
| Workspace | `~/.nanoinfra/workspace/` |

## Status

| Command | Description |
|---|---|
| `nanoinfra status` | Summarize the default config/workspace and check Agent provider/model readiness |
| `nanoinfra status --config <path>` | Check a specific config file |
| `nanoinfra status --workspace <path>` | Show status with a workspace override |

Status does not send a model request. On success, run the printed
`nanoinfra agent -m "Hello!"` command to verify network access and credentials. On failure,
follow the printed WebUI **Settings → Models** or `nanoinfra onboard --wizard` route.

## Agent CLI

| Command | Description |
|---|---|
| `nanoinfra agent -m "Hello!"` | Send one message and exit |
| `nanoinfra agent` | Start interactive terminal chat |
| `nanoinfra agent --session <id>` | Use a specific session key |
| `nanoinfra agent --workspace <path>` | Override workspace |
| `nanoinfra agent --config <path>` | Use a specific config file |
| `nanoinfra agent --no-markdown` | Print plain text instead of Rich-rendered Markdown |
| `nanoinfra agent --logs` | Show runtime logs while chatting |

In interactive mode, `Enter` sends the current message. Press `Alt+Enter` to add a newline before sending.

Interactive mode exits with `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.

## WebUI

| Command | Description |
|---|---|
| `nanoinfra webui` | Create config/workspace if needed, enable the local WebUI channel after confirmation, start the gateway, and open `http://127.0.0.1:8765` |
| `nanoinfra webui --background` | Start or reuse a background gateway, then open the WebUI |
| `nanoinfra webui --no-open` | Prepare and start the WebUI without opening a browser |
| `nanoinfra webui --port <port>` | Set the WebUI/WebSocket port |
| `nanoinfra webui --gateway-port <port>` | Override the gateway health port |
| `nanoinfra webui --yes` | Apply safe localhost WebUI defaults without confirmation; configure provider credentials in **Settings → Models** |

First-run WebUI setup binds to `127.0.0.1` by default. Use manual configuration and a WebUI password before exposing the WebSocket channel beyond localhost.

## Gateway

`nanoinfra gateway` starts enabled chat channels, WebUI/WebSocket when configured, cron-backed system jobs, Dream, heartbeat, and the health endpoint. Most local browser users should start with `nanoinfra webui`; use `gateway` directly for service management, chat app operation, and advanced deployment. By default it runs in the foreground, which keeps existing scripts and terminal workflows unchanged. Use `--background` when you want a local macOS, Linux, or Windows process that you can manage from the CLI.

| Command | Description |
|---|---|
| `nanoinfra gateway` | Start the gateway in the foreground with config defaults |
| `nanoinfra gateway --verbose` | Show verbose runtime output |
| `nanoinfra gateway --port <port>` | Override `gateway.port` for the health endpoint |
| `nanoinfra gateway --workspace <path>` | Override workspace |
| `nanoinfra gateway --config <path>` | Use a specific config file |
| `nanoinfra gateway --background` | Start the gateway as a background process |
| `nanoinfra gateway status` | Show the recorded background gateway PID, state file, and log file |
| `nanoinfra gateway logs --no-follow` | Print recent background gateway logs and exit |
| `nanoinfra gateway logs` | Follow background gateway logs |
| `nanoinfra gateway restart` | Restart the recorded background gateway with the current config |
| `nanoinfra gateway stop` | Stop the recorded background gateway |
| `nanoinfra gateway install-service` | Install a systemd user service or macOS LaunchAgent |
| `nanoinfra gateway install-service --dry-run` | Preview the generated service file and system commands |
| `nanoinfra gateway uninstall-service` | Remove the installed system service |

For custom instances, pass the same selector flags to management commands:

```bash
nanoinfra gateway --background --config ./bot-a/config.json --workspace ./bot-a/workspace
nanoinfra gateway status --config ./bot-a/config.json --workspace ./bot-a/workspace
nanoinfra gateway stop --config ./bot-a/config.json --workspace ./bot-a/workspace
nanoinfra gateway install-service --config ./bot-a/config.json --workspace ./bot-a/workspace --name bot-a
```

`--background` is a lightweight detached process. `install-service` is for
login/startup integration: Linux uses a systemd user service; macOS uses a
LaunchAgent plist. System services run the foreground gateway under the OS
supervisor rather than nesting another background process.

Default health endpoint:

```text
http://127.0.0.1:18790/health
```

The bundled WebUI is served by the WebSocket channel, usually on port `8765`, not by the gateway health endpoint.

## Local Triggers

`nanoinfra trigger` delivers one local message to a trigger that was created from
a chat/session with `/trigger <name>`.

```bash
nanoinfra trigger trg_8K4P2Q9X "Review PR #4502"
```

Keep `nanoinfra gateway` running so the message can be delivered to the linked
chat/session. The message is recorded as an automation turn in that session,
not as a normal chat message typed by the user.

The command writes to a workspace-local durable queue. If `nanoinfra gateway` is
not running yet, the message waits in that workspace. If the target session is
already running a turn, the trigger waits for that session to become idle. If the
gateway exits after claiming a delivery but before the linked turn completes,
the next gateway start requeues that delivery. The queue is at-least-once, not
exactly-once, so the same message can be delivered again after an interrupted
process. If the agent receives the delivery and the turn fails, the delivery is
marked failed instead of retried indefinitely. Each delivery also writes an
audit record under `<workspace>/triggers/runs`. Run one gateway consumer per
workspace; this local queue is not a distributed multi-consumer queue.

Use stdin when another local process generates the message:

```bash
generate-report | nanoinfra trigger trg_8K4P2Q9X
```

Options:

| Command | Description |
|---|---|
| `nanoinfra trigger <id> "message"` | Deliver one message through a trigger |
| `nanoinfra trigger <id>` | Read the message from stdin |
| `nanoinfra trigger --config <path> <id> "message"` | Use the workspace from a specific config |
| `nanoinfra trigger --workspace <path> <id> "message"` | Use a specific workspace |

Triggers are managed in the WebUI Automations view instead of through separate
`list`, `revoke`, or `delete` CLI subcommands. From there you can pause/resume,
rename, delete, search, and copy the command for each trigger.

For webhooks or other external systems, run your own small service and have it
call this CLI after it decides what message nanoinfra should receive.

See [Automations](./automations.md) for the broader automation model, WebUI
management, and delivery behavior.

## OpenAI-Compatible API

| Command | Description |
|---|---|
| `nanoinfra serve` | Start `/v1/chat/completions`, `/v1/models`, and `/health` |
| `nanoinfra serve --host <host>` | Override API bind host |
| `nanoinfra serve --port <port>` | Override API port |
| `nanoinfra serve --timeout <seconds>` | Override per-request timeout |
| `nanoinfra serve --verbose` | Show runtime logs |
| `nanoinfra serve --workspace <path>` | Override workspace |
| `nanoinfra serve --config <path>` | Use a specific config file |

Default API endpoint:

```text
http://127.0.0.1:8900
```

Public binds (`0.0.0.0` or `::`) require `api.apiKey`; send it as a Bearer token on API routes.

See [`openai-api.md`](./openai-api.md) for request examples.

## Status

```bash
nanoinfra status
```

Shows the config path, workspace path, active model, and provider summary without calling a model.

| Command | Description |
|---|---|
| `nanoinfra status` | Inspect the default instance |
| `nanoinfra status --config <path>` | Inspect a specific config |
| `nanoinfra status --config <path> --workspace <path>` | Inspect a specific config with a workspace override |

## Channels

| Command | Description |
|---|---|
| `nanoinfra channels status` | Show configured channel status |
| `nanoinfra channels status --config <path>` | Show channel status for a specific config |
| `nanoinfra channels login <channel>` | Run interactive login for supported channels |
| `nanoinfra channels login <channel> --force` | Re-authenticate even if credentials already exist |
| `nanoinfra channels login <channel> --config <path>` | Use a specific config file |
| `nanoinfra plugins list --config <path>` | Show plugin/channel enabled state for a specific config |

Examples:

```bash
nanoinfra channels login whatsapp
nanoinfra channels login weixin
nanoinfra channels status
```

See [`chat-apps.md`](./chat-apps.md) for channel-specific setup.

## Optional Features

Use these commands when you want nanoinfra to add or remove a built-in capability
without hand-editing JSON. Enabling may install the support package first.
Disabling is for channels such as Telegram, Matrix, or Slack; it keeps your
saved settings and turns the channel off.

The `plugins` command name is retained for compatibility, but these entries are
nanoinfra runtime support packages, not the user-invokable tools shown in WebUI
Apps. They cannot be attached to a chat turn with `@`.

| Feature name | What it enables |
|---|---|
| `api` | Dependencies required by the OpenAI-compatible `nanoinfra serve` process |
| `azure` | Azure identity support for Azure-hosted models |
| `bedrock` | AWS Bedrock model provider support |
| `langfuse` | Langfuse tracing support for OpenAI-compatible providers |
| `olostep` | Olostep web search provider support |
| A channel name such as `telegram` or `slack` | The connector package and saved channel enablement |

| Command | Description |
|---|---|
| `nanoinfra plugins list` | Show available channels and optional capabilities |
| `nanoinfra plugins enable <name>` | Install missing support and enable the feature or channel |
| `nanoinfra plugins enable <name> --logs` | Show package install logs while enabling |
| `nanoinfra plugins disable <channel>` | Turn off a channel without deleting its saved settings |
| `nanoinfra plugins list --config <path>` | Read a specific config file |
| `nanoinfra plugins enable <name> --config <path>` | Update a specific config file |
| `nanoinfra plugins disable <channel> --config <path>` | Turn off a channel in a specific config file |

Document and PDF reading are included in the standard installation. The old
`nanoinfra plugins enable documents` and `nanoinfra plugins enable pdf` commands
remain accepted as no-op compatibility aliases.

## Provider OAuth

| Command | Description |
|---|---|
| `nanoinfra provider login openai-codex --set-main` | Authenticate Codex and select its current default model |
| `nanoinfra provider login xai-grok --set-main` | Authenticate an eligible X Premium / Grok subscription and select Grok 4.5; hosted X Search is enabled for models that advertise support |
| `nanoinfra provider login github-copilot --set-main` | Authenticate GitHub Copilot and select its current default model |
| `nanoinfra provider logout openai-codex` | Remove OpenAI Codex OAuth state |
| `nanoinfra provider logout xai-grok --config <path>` | Remove the selected nanoinfra instance's xAI OAuth state |
| `nanoinfra provider logout github-copilot` | Remove GitHub Copilot OAuth state |

See [`providers.md`](./providers.md#oauth-providers) for when OAuth providers need explicit provider/model selection.

## Useful First Checks

```bash
nanoinfra --version
nanoinfra status
nanoinfra agent -m "Hello!"
```

If these fail, use [`troubleshooting.md`](./troubleshooting.md) before debugging WebUI, chat apps, Docker, systemd, or SDK integrations.
