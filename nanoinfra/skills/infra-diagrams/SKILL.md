---
name: infra-diagrams
description: Read, discuss, and update a user's saved Infra Diagrams (nodes/edges describing their infrastructure topology). Use when the user asks to view, discuss, add to, or change a saved infra diagram.
---

# Infra Diagrams

Tools: `list_diagrams`, `get_diagram`, `list_diagram_components`, `update_diagram`.

## Resolve the diagram first

If the user hasn't attached one via `/infradiagrams` and didn't give you an id, call `list_diagrams` to find it by name. Then **always call `get_diagram`** before proposing any change — never build a diff from what was said earlier in the conversation. `get_diagram` returns the exact current `position`/`style`/`data` for every node; copy those forward verbatim for anything you're not intentionally changing.

`update_diagram` takes the **full replacement** node/edge list, not a delta — dropping a field you didn't mean to touch silently loses it. Concretely: dropping a `groupBox` node's `style` resets it to a tiny default size and makes its children overlap (this happened for real in the visual editor before it was fixed) — never omit `style` on a node that already had one.

## Never invent a component

Before using any `componentTypeId`/`providerId` in `update_diagram`, call `list_diagram_components`. It returns the exact catalog the visual palette uses — every valid type, its providers, and each provider's config `fields`. `update_diagram` itself rejects unknown pairs and saves nothing, but don't rely on that as your only check — look the id up first.

If the user wants something that isn't in the catalog (e.g. "add a PowerDNS component under DNS"), **don't fabricate an id**. Tell them it's not in the catalog yet, and offer to add it yourself — you already have file-write tools, and a workspace catalog file needs no restart, no code change, and takes effect on your very next `list_diagram_components` call. Two shapes, saved as JSON under `<workspace>/diagrams/catalog/`:

Brand-new component type (key is `id`):
```json
{
  "id": "message_queue",
  "label": "Message Queue",
  "category": "Data",
  "iconKey": "backup",
  "providers": [
    { "id": "rabbitmq", "label": "RabbitMQ", "kind": "docker", "integration": { "type": "internal" },
      "fields": [{ "key": "image", "label": "Image tag", "placeholder": "rabbitmq:4-management", "kind": "text" }] }
  ]
}
```

Add a provider to an *existing* type (key is `componentTypeId`, not `id` — no need to repeat that type's other providers):
```json
{
  "componentTypeId": "dns",
  "providers": [
    { "id": "powerdns", "label": "PowerDNS", "kind": "api", "integration": { "type": "api" }, "fields": [] }
  ]
}
```

## CRITICAL: preview, then wait for explicit confirmation

`update_diagram` defaults to `dry_run=true`, which validates and returns a plain-text diff **without saving anything**. Always call it this way first, show the user that diff, and explicitly ask whether to save. Only call it again with `dry_run=false` — same `diagram_id`, same `nodes`/`edges` — after the user's *next message* clearly confirms. Never set `dry_run=false` on your first call, and never infer approval from anything other than an explicit reply — if the answer is unclear, ask again instead of saving.
