---
name: servers
description: Read, discuss, and manage the server inventory (host, connection method, which secret to authenticate with). Use when the user asks to add, view, edit, or remove a server, run a command on a server, or when a Diagram's target list needs a real server.
---

# Servers

Tools: `list_servers`, `get_server`, `create_server`, `update_server`, `delete_server`, `execute_on_server`.

## Execution

`execute_on_server` actually connects to a server and runs a command/action, via whichever provider that server uses. This is the highest-consequence tool available: **never** set `dry_run=false` without the user's explicit confirmation of the exact command and server shown in the preview, and never retry a failed/timed-out run with a different command without a fresh confirmation for that new command specifically.

A job's record (status, output, exit code) persists to disk before execution starts. If it comes back `timed_out` or `failed`, say so plainly -- don't retry silently.

## Credentials

A server's `secretRef` is a Secret's id. Secrets has no agent-facing tool at all -- you cannot look up, list, or resolve a secret by name from chat. Get the id from the user directly (they manage secrets from the WebUI's Secrets page), and never invent one or guess at a match. If the user asks you to store a password or key for them, tell them to use the WebUI's Secrets page -- there is no way to do this from chat.

## CRITICAL: preview, then wait for explicit confirmation

All mutating tools default to `dry_run=true`. Show the preview, wait for the user's next message to explicitly confirm, then call again with `dry_run=false` and the exact same arguments. `update_server` and `create_server` are full replacements, not deltas -- omitted fields are cleared, not left unchanged.
