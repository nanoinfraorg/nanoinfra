# Security Boundaries

The agent operates with significant power (file system, shell, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `apply_patch`) resolve paths through the workspace path resolver (`agent/tools/filesystem.py` / `agent/tools/path_utils.py`), which enforces that the resolved path must lie under the active workspace when workspace restriction is enabled. The media upload directory is always an internal extra read root while restricted.

Additional filesystem roots must be capability-specific. `extra_allowed_dirs` is a legacy read-only alias. Use `extra_read_allowed_dirs` for read-only roots, `extra_write_allowed_dirs` only when a write-capable tool is intentionally allowed to modify an extra directory, and exact file allowlists when a tool may modify only specific files.

Shell execution (`ExecTool`, `agent/tools/shell.py`) also respects `restrict_to_workspace` as an application-level guard: if enabled and `working_dir` is outside the workspace, the command is rejected before execution, and command text is checked for obvious workspace escapes. This is not process-level isolation; use an exec sandbox backend for that.

**Rule**: Any new path-handling logic must go through the workspace path resolver or perform an equivalent containment check with explicit read/write capability semantics.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through the shared URL guards in `security/network.py` (`validate_url_target` or `resolve_url_target`). By default they block loopback, RFC1918 private addresses, CGNAT ranges, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

For direct requests, the only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time. An explicitly configured `providers.<name>.proxy` is a separate user-authorized trust boundary for provider requests and provider-returned image URL downloads. Those downloads still reject malformed URLs and locally identifiable private/internal targets on every redirect, but hostnames unavailable to local DNS are delegated to the trusted proxy. The user-selected proxy owns final DNS resolution and network egress policy.

HTTP/SSE MCP transports are part of this boundary: validate configured MCP URLs before probing or constructing clients, and validate each outgoing HTTP request before redirects are followed. Local/private HTTP MCP endpoints are allowed only through the explicit SSRF whitelist. Stdio MCP servers are not part of the HTTP SSRF path.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

## Server Execution Backends

`execute_on_server` (`agent/tools/server_execution.py`) is a thin client after #18. It writes one request to a Unix domain socket and renders the reply. The executor (`gates/executor/server.py`) is the one place a `secretRef` is decrypted to a real credential, and it hands that value to a connection backend (`servers/execution/`). The tool must import no backend and no `SecretStore`, and `tests/gates/test_executor_client.py` walks its syntax tree to assert that. Two boundaries protect it, and one accepted risk remains recorded here.

Target validation goes through `servers/network_guard.py`, **not** `security/network.py`. That guard is deliberately narrower: it blocks loopback, link-local, cloud metadata (`169.254.0.0/16`), the unspecified address, and their IPv4-mapped IPv6 forms, but explicitly allows RFC1918, because inventoried infrastructure legitimately lives there. The guard lives in `_guard` in `gates/executor/server.py` after #18, and it must check exactly what the corresponding backend dials — a guard that validates a field the backend ignores is a bypass, not defense in depth (`ssm` has no dialed address at all and is authorized via IAM instead). An address field gets its one value checked. A pattern field is expanded first and every host it names is checked, because a validated label is not a validated host set. `ansible-runner` reads both `inventoryHost` and `group` as patterns. The `api` backend additionally pins each request to the operator-configured `baseUrl`'s origin, since `urljoin` would otherwise let an agent-supplied absolute path send the decrypted credential to an arbitrary host.

**Accepted risk — SSH host-key verification is disabled.** `servers/execution/ssh_backend.py` connects with `known_hosts=None`, so no host key is verified. There is no host-key trust store anywhere in this codebase to check against yet, and this was a deliberate design decision for the initial Servers execution engine rather than an oversight. The consequence: an attacker with a network position between the gateway and the target host can impersonate that host, capture the decrypted credential passed as an SSH password or private key, and tamper with the command output the agent reports back. Closing this needs a per-Server known-hosts store (or an operator-supplied `known_hosts` path in the Server config) validated before connecting.

**Rule**: Do not widen `network_guard.py` to accept loopback/link-local/metadata, do not route Server execution through `security/network.py` (it blocks RFC1918 and would break the module), and do not add a new provider without a case in the executor's `_guard` plus a case in `tests/gates/test_executor_guard_consistency.py`.

**Rule**: A new capability class needs an entry in the capability manifest (`CAPABILITY_CLASSES` in `agent/tools/capabilities.py`). Every tool in that class must also declare `capability_class`. `capability_class_of()` resolves an unlisted value to `mutate.remote`. A typo therefore fails closed and gates nothing new. Only the CI guard in `tests/agent/tools/test_capabilities.py` catches that typo.

**Rule**: A new execution provider needs a scope-resolver case beside its `_guard` case. The case must map the fields that provider dials to `host`, `group`, or `all`. The reason matches the host-field rule above: a scope nothing resolves is a blast radius nobody bounded. Scope resolution lives in `servers/scope.py` (nanoinfraorg/nanoinfra#4). Add the case with the provider, not after it.

## Shell Sandbox

`tools/sandbox.py` provides optional command wrapping. The only backend currently shipped is `bwrap` (bubblewrap), intended for containerized deployments. On macOS and bare-metal Linux without `bwrap`, commands run in the native shell with workspace restriction as an application-level guard only.

**Rule**: If adding a new sandbox backend, implement `_wrap_<name>(command, workspace, cwd) -> str` and register it in `_BACKENDS`.
