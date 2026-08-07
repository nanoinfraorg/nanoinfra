# Secrets and Servers

Store credentials safely, keep an inventory of the machines you manage, and let the agent actually connect to one and run something — with the credential never passing through the agent itself.

These are two related modules: **Secrets** is an encrypted credential store; **Servers** is an inventory of hosts (each optionally pointing at a Secret to authenticate with) plus the ability to connect to one and run a command or action.

## Secrets

### Enable it

Secrets needs one environment variable, set in the same environment that starts the gateway (service unit, container, or shell profile) — nanoinfra never generates or stores this key itself:

```bash
export NANOINFRA_SECRETS_KEY="$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
```

Without it, the gateway still starts normally — every Secrets operation just returns a clean "not configured" error until the key is set. Back this key up somewhere safe outside the machine: if it's lost, every secret already stored under it becomes permanently undecryptable, by design (there's no recovery path and no way to reset it in place).

### Create one

**There is no agent tool for creating, editing, or rotating a secret — only the WebUI.** This is deliberate: the LLM never sees a plaintext credential value, not even for one turn. Open the WebUI's **Infrastructure → Secrets** page, click **New Secret**, and fill in:

- **Name** — must be unique (case-insensitive); this is what a Server's `secretRef` and the agent both refer to it by.
- **Kind** — `password`, `api_key`, `ssh_key`, or `token`. This is a UI hint only (which input widget to show); it doesn't change how the value is stored.
- **Provider** — `local` (one encrypted file per secret in the workspace) or `postgres` (shared across a deployment via `NANOINFRA_SECRETS_POSTGRES_DSN`, see [Configuration](./configuration.md#servers-module-execution-backends)). Pick per-secret, not instance-wide — a personal SSH key can stay local while a shared production password lives in Postgres.
- **Value** — write-only. Updating a secret always requires typing the full new value; there's no partial edit and the previous value is never shown back to you, by anyone, ever.

The agent has **no visibility into Secrets at all** — not even metadata, not even a list of names. If you want the agent to wire a Secret to a Server, tell it the secret's name yourself; it can't look one up.

## Servers

### Create one

Either the agent (`create_server`, `update_server`, `list_servers`, `get_server`, `delete_server` — all `dry_run`-gated, preview first) or the WebUI's **Infrastructure → Servers** page. Every server has:

- **Name** — unique; this is what the agent and the Diagrams target picker refer to it by.
- **Provider** — `ssh`, `ansible-runner`, `ssm`, or `api` (below).
- **Config** — provider-specific fields, exact keys below.
- **Secret** — optional, a dropdown of existing Secrets by name (WebUI) or a Secret's id (agent tool's `secretRef`). This is where a Server's credential comes from — inventory CRUD never decrypts it, only stores the reference.
- **Tags** — free-form, for your own grouping/filtering.

### Provider config fields

| Provider | Config fields | Notes |
|---|---|---|
| `ssh` | `host`, `port`, `username` | `port` defaults to `22` if omitted. No `username` means asyncssh falls back to the local process user — usually not what you want; set it explicitly. |
| `ansible-runner` | `inventoryHost`, `group`, `projectPath` | Targets `inventoryHost` if set, else `group`. At least one of the two is required — a server with neither refuses to execute (nothing concrete to validate or target). `projectPath` points at the Ansible project directory containing your inventory/playbooks. |
| `ssm` | `instanceId`, `region` | AWS Systems Manager Run Command — authenticates via IAM/instance-profile permissions, not a Secret. A `secretRef` is accepted but unused for this provider. |
| `api` | `baseUrl` | Calls a specific endpoint on this base URL — never an arbitrary shell command. The agent's `command` argument for this provider is `"<METHOD> <path>"` (method optional, defaults to `GET`); any request that would resolve to a different origin than `baseUrl` is refused before it's ever sent. |

## Connecting and running something

**There is no "run" button in the WebUI — execution only happens through the agent**, via `execute_on_server`. Inventory management (create/edit/list/delete) and execution are deliberately separate surfaces. To run something, just ask in a normal chat:

> "Connect to `<server name>` and run `uptime`"

The agent will:

1. Resolve the server and preview exactly what it's about to do (server, provider, resolved command) — nothing runs yet.
2. Wait for your explicit confirmation.
3. Only then connect, resolve the Secret in-process (the LLM never sees the decrypted value), run the command, and report the result.

This is the highest-consequence tool in the system — the agent is instructed to never infer approval and to ask again for a fresh confirmation before retrying with a different command.

### What happens behind the scenes

- **Durable job records.** Every execution attempt is written to disk before it starts (`queued`, then `running`, then a terminal status) — a gateway crash mid-run doesn't lose the record; it's reconciled to `failed` with an "interrupted by restart" note on the next startup.
- **Smart timeouts.** Not one fixed deadline: the idle clock resets on real activity (SSH's streaming output; Ansible Runner's completion signal) but an absolute 30-minute ceiling always applies regardless of activity. For `ansible-runner` and `ssm`, a reported timeout can't actually stop the remote work already in flight (there's no way to cancel a thread mid-blocking-call or recall an already-sent AWS command) — the agent will tell you the command may still be running rather than pretending otherwise.
- **Target guard.** Before connecting, the host/address is checked against a guard that blocks loopback, link-local, and the cloud metadata address, but deliberately allows private (RFC1918) ranges — most real infrastructure lives there. This exists specifically so a Server record can't be used to reach the metadata endpoint and exfiltrate cloud credentials.
- **SSH host-key verification is disabled** (no trust-on-first-use store exists yet) — an accepted, documented risk (see `.agent/security.md`), not an oversight. Don't point this at a host you don't already trust the network path to.
