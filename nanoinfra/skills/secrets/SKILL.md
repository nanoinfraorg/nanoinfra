---
name: secrets
description: Read metadata about and manage stored secrets (credentials for connecting to servers/services). Use when the user asks to store, rotate, list, or delete a secret, or when another tool (e.g. a Server) needs a secret reference.
---

# Secrets

> [!WARNING]
> **Typing a secret's value into chat persists it in this conversation's session history and re-sends it to the LLM provider on every subsequent turn.** `create_secret`/`update_secret` take the plaintext value as a tool-call argument, and tool-call arguments are recorded in session history like any other message content — there is no redaction of them today. For genuinely sensitive credentials (production passwords, long-lived API keys, anything you wouldn't paste into a public chat log), prefer having the user enter the value directly through the WebUI/REST API instead of typing it to you for you to relay. If a user pastes a high-sensitivity value directly into chat, proactively flag this tradeoff before calling `create_secret`/`update_secret` with it, and suggest the WebUI path for next time.

Tools: `list_secrets`, `get_secret`, `create_secret`, `update_secret`, `delete_secret`.

## Secrets are write-only to you

No tool here ever returns a secret's value -- not `get_secret`, not the dry-run preview of `create_secret`/`update_secret`, nothing. You can create, rotate, and reference secrets by id or name, but you can never read one back. If a user asks you to "check what the password is," tell them that's by design and not something you can do.

## Creating vs. updating

`create_secret` mints a new id. `update_secret` replaces an existing secret's value entirely -- there's no partial update, and the request always needs the full replacement (name, kind, value), not just the field being changed.

## CRITICAL: preview, then wait for explicit confirmation

All three mutating tools default to `dry_run=true`, returning a preview without saving anything. Always call this way first, show the user the preview, and wait for their next message to explicitly confirm before calling again with `dry_run=false`. Never infer approval from anything else.

## Before deleting

Check whether a Server references the secret you're about to delete (`list_servers`/`get_server`) -- deleting it breaks that Server's ability to connect. Warn the user if you find a reference before confirming the delete.
