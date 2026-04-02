---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

- `memory/MEMORY.md` — Long-term facts (preferences, project context, relationships). Always loaded into your context.
- `memory/HISTORY.md` — Append-only event log. NOT loaded into context. Search it with grep-style tools or in-memory filters. Each entry starts with [YYYY-MM-DD HH:MM].

## Search Past Events

Choose the search method based on file size:

- Small `memory/HISTORY.md`: use `read_file`, then search in-memory
- Large or long-lived `memory/HISTORY.md`: use the built-in `grep` tool first
- For broad searches, start with `grep(..., output_mode="count")` or accept the default `files_with_matches` output to scope the result set before asking for full matching lines
- Use `head_limit` / `offset` when browsing long histories in chunks
- Use `exec` only as a last-resort fallback when you truly need shell-specific behavior

Examples:
- `grep(pattern="keyword", path="memory/HISTORY.md", case_insensitive=true)`
- `grep(pattern="[2026-04-02 10:00]", path="memory/HISTORY.md", fixed_strings=true)`
- `grep(pattern="keyword", path="memory/HISTORY.md", output_mode="count", case_insensitive=true)`
- `grep(pattern="token", path="memory", glob="*.md", output_mode="files_with_matches", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.md", case_insensitive=true)`
- Fallback shell examples:
  - **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
  - **Windows:** `findstr /i "keyword" memory\HISTORY.md`

Prefer the built-in `grep` tool for large history files; only drop to shell when the built-in search cannot express what you need.

## When to Update MEMORY.md

Write important facts immediately using `edit_file` or `write_file`:
- User preferences ("I prefer dark mode")
- Project context ("The API uses OAuth2")
- Relationships ("Alice is the project lead")

## Auto-consolidation

Old conversations are automatically summarized and appended to HISTORY.md when the session grows large. Long-term facts are extracted to MEMORY.md. You don't need to manage this.
