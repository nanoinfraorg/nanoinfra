# Dream: Two-Stage Memory Consolidation

Dream is nanobot's memory management system. It automatically extracts key information from conversations and persists it as structured knowledge files.

## Architecture

```
Consolidator (per-turn)              Dream (cron-scheduled)           GitStore (version control)
+----------------------------+       +----------------------------+   +---------------------------+
| token over budget → LLM    |       | Phase 1: analyze history    |   | dulwich-backed .git repo  |
| summarize evicted messages  |──────▶|   vs existing memory files  |   | auto_commit on Dream run  |
| → history.jsonl            |       | Phase 2: AgentRunner        |   | /dream-log: view changes  |
| (plain text, no tool_call)  |       |   + read_file/edit_file     |   | /dream-restore: rollback  |
+----------------------------+       |   → surgical incremental    |   +---------------------------+
                                     |     edit of memory files    |
                                     +----------------------------+
```

### Consolidator

Lightweight, triggered on-demand after each conversation turn. When a session's estimated prompt tokens exceed 50% of the context window, the Consolidator sends the oldest message slice to the LLM for summarization and appends the result to `history.jsonl`.

Key properties:
- Uses plain-text LLM calls (no `tool_choice`), compatible with all providers
- Cuts messages at user-turn boundaries to avoid truncating multi-turn conversations
- Up to 5 consolidation rounds until the token budget drops below the safety threshold

### Dream

Heavyweight, triggered by a cron schedule (default: every 2 hours). Two-phase processing:

| Phase | Description | LLM call |
|-------|-------------|----------|
| Phase 1 | Compare `history.jsonl` against existing memory files, output `[FILE] atomic fact` lines | Plain text, no tools |
| Phase 2 | Based on the analysis, use AgentRunner with `read_file` / `edit_file` for incremental edits | With filesystem tools |

Key properties:
- Incremental edits — never rewrites entire files
- Cursor always advances to prevent re-processing
- Phase 2 failure does not block cursor advancement (prevents infinite loops)

### GitStore

Pure-Python git implementation backed by [dulwich](https://github.com/jelmer/dulwich), providing version control for memory files.

- Auto-commits after each Dream run
- Auto-generated `.gitignore` that only tracks memory files
- Supports log viewing, diff comparison, and rollback

## Data Files

```
workspace/
├── SOUL.md              # Bot personality and communication style (managed by Dream)
├── USER.md              # User profile and preferences (managed by Dream)
└── memory/
    ├── MEMORY.md        # Long-term facts and project context (managed by Dream)
    ├── history.jsonl    # Consolidator summary output (append-only)
    ├── .cursor          # Last message index processed by Consolidator
    ├── .dream_cursor    # Last history.jsonl cursor processed by Dream
    └── .git/            # GitStore repository
```

### history.jsonl Format

Each line is a JSON object:

```json
{"cursor": 42, "timestamp": "2026-04-03 00:02", "content": "- User prefers dark mode\n- Decided to use PostgreSQL"}
```

Searching history:

```bash
# Python (cross-platform)
python -c "import json; [print(json.loads(l).get('content','')) for l in open('memory/history.jsonl','r',encoding='utf-8') if l.strip() and 'keyword' in l.lower()][-20:]"

# grep
grep -i "keyword" memory/history.jsonl
```

### Compaction

When `history.jsonl` exceeds 1000 entries, it automatically drops entries that Dream has already processed (keeping only unprocessed entries).

## Configuration

Configure under `agents.defaults.dream` in `~/.nanobot/config.json`:

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "cron": "0 */2 * * *",
        "model": null,
        "max_batch_size": 20,
        "max_iterations": 10
      }
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cron` | string | `0 */2 * * *` | Cron expression for Dream run interval |
| `model` | string\|null | null | Optional model override for Dream |
| `max_batch_size` | int | 20 | Max history entries processed per run |
| `max_iterations` | int | 10 | Max tool calls in Phase 2 |

Dependency: `pip install dulwich`

## Commands

| Command | Description |
|---------|-------------|
| `/dream` | Manually trigger a Dream run |
| `/dream-log` | Show the latest Dream changes (git diff) |
| `/dream-log <sha>` | Show changes from a specific commit |
| `/dream-restore` | List the 10 most recent Dream commits |
| `/dream-restore <sha>` | Revert a specific commit (restore to its parent state) |

## Troubleshooting

### Dream produces no changes

Check whether `history.jsonl` has entries and whether `.dream_cursor` has caught up:

```bash
# Check recent history entries
tail -5 memory/history.jsonl

# Check Dream cursor
cat memory/.dream_cursor

# Compare: the last entry's cursor in history.jsonl should be > .dream_cursor
```

### Memory files contain inaccurate information

1. Use `/dream-log` to inspect what Dream changed
2. Use `/dream-restore <sha>` to roll back to a previous state
3. If the information is still wrong after rollback, manually edit the memory files — Dream will preserve your edits on the next run (it skips facts that already match)

### Git-related issues

```bash
# Check if GitStore is initialized
ls workspace/.git

# If missing, restart the gateway to auto-initialize

# View commit history manually (requires git)
cd workspace && git log --oneline
```
