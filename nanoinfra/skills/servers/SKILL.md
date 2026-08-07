---
name: servers
description: Read, discuss, and manage the server inventory (host, connection method, which secret to authenticate with). Use when the user asks to add, view, edit, or remove a server, or when a Diagram's target list needs a real server.
---

# Servers

Tools: `list_servers`, `get_server`, `create_server`, `update_server`, `delete_server`.

## What this is (and isn't) yet

This is inventory only: where a server is and how to reach it. There is no tool yet that actually connects to a server and runs something on it -- if the user asks you to do that, tell them it isn't built yet rather than attempting to fabricate a connection.

## Credentials

A server's `secretRef` is a Secret's id. Secrets has no agent-facing tool at all -- you cannot look up, list, or resolve a secret by name from chat. Get the id from the user directly (they manage secrets from the WebUI's Secrets page), and never invent one or guess at a match. If the user asks you to store a password or key for them, tell them to use the WebUI's Secrets page -- there is no way to do this from chat.

## CRITICAL: preview, then wait for explicit confirmation

All mutating tools default to `dry_run=true`. Show the preview, wait for the user's next message to explicitly confirm, then call again with `dry_run=false` and the exact same arguments. `update_server` and `create_server` are full replacements, not deltas -- omitted fields are cleared, not left unchanged.
