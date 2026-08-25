---
name: clawhub
description: Search and install agent skills from ClawHub, the public skill registry.
homepage: https://clawhub.ai
metadata: {"nanoinfra":{"emoji":"🦞"}}
---

# ClawHub

Public skill registry for AI agents. Search by natural language (vector search).

## When to use

Use this skill when the user asks any of:
- "find a skill for …"
- "search for skills"
- "install a skill"
- "what skills are available?"
- "update my skills"

## Search

```bash
npx --yes clawhub@latest search "web scraping" --limit 5
```

## Install

```bash
npx --yes clawhub@latest install <slug> --workdir <workspace>
```

Replace `<slug>` with the skill name from search results, and `<workspace>` with **this session's own workspace path** — it is in your runtime context, and the WebUI shows it under Settings as "Default workspace". Do not assume a literal: a fresh install uses `~/.nanoinfra/workspaces/default`, an older one `~/.nanoinfra/workspace`, and an operator may have set any path at all. The skill lands in `<workspace>/skills/`, which is where nanoinfra loads workspace skills from. Always include `--workdir`.

## Update

```bash
npx --yes clawhub@latest update --all --workdir <workspace>
```

## List installed

```bash
npx --yes clawhub@latest list --workdir <workspace>
```

## Notes

- Requires Node.js (`npx` comes with it).
- No API key needed for search and install.
- Login (`npx --yes clawhub@latest login`) is only required for publishing.
- `--workdir <workspace>` is critical — without it, skills install to the current directory instead of the nanoinfra workspace. Getting the path wrong is just as bad: the skill lands in a workspace this session does not load from.
- After install, remind the user to start a new session to load the skill.
