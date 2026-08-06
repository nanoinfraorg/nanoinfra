"""Starter example shown the first time a workspace's Infra Diagrams gallery
is opened — an empty list with nothing to click into isn't useful on a fresh
install.

Only fires once per workspace: the check is "has this workspace's diagrams/
directory ever been created", not "is the list currently empty" — deleting
the example (or every other diagram) must never bring it back.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.diagrams.store import DiagramStore

# Mirrors the old client-only webui/src/components/diagrams/seedDiagram.ts
# (removed once diagrams moved to real backend persistence) plus the later
# Storage-group addition — a realistic, moderately complex example that
# exercises groups, nesting, and the Model source/Storage path connection
# linking in one diagram.
EXAMPLE_DIAGRAM: dict[str, Any] = {
    "name": "Example: web app with cache + replica",
    "targets": ["prod-web-01"],
    "nodes": [
        {
            "id": "client",
            "position": {"x": 40, "y": 20},
            "data": {"label": "User Client", "componentTypeId": "client", "providerId": "browser", "config": {}},
        },
        {
            "id": "dns",
            "position": {"x": 20, "y": 140},
            "data": {
                "label": "Cloudflare DNS",
                "componentTypeId": "dns",
                "providerId": "cloudflare",
                "config": {"zone": "example.com"},
            },
        },
        {
            "id": "lb",
            "position": {"x": 20, "y": 260},
            "data": {
                "label": "Application Load Balancer",
                "componentTypeId": "load_balancer",
                "providerId": "alb",
                "config": {},
            },
        },
        {
            "id": "web",
            "position": {"x": 20, "y": 380},
            "data": {
                "label": "Web Application Server",
                "componentTypeId": "web_server",
                "providerId": "nginx",
                "config": {"image": "nginx:1.27", "domains": "example.com, www.example.com"},
            },
        },
        {
            "id": "db",
            "position": {"x": -140, "y": 520},
            "data": {
                "label": "PostgreSQL Database",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "cache",
            "position": {"x": 180, "y": 520},
            "data": {
                "label": "Redis Cache",
                "componentTypeId": "cache",
                "providerId": "redis",
                "config": {"image": "redis:7"},
            },
        },
        {
            "id": "replica",
            "position": {"x": -140, "y": 640},
            "data": {
                "label": "Read Replica",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "storage-group",
            "type": "groupBox",
            "position": {"x": 380, "y": 380},
            "style": {"width": 300, "height": 420},
            "data": {
                "label": "Storage via Isilon",
                "componentTypeId": "__group__",
                "providerId": "generic",
                "config": {},
            },
        },
        {
            "id": "storage-web",
            "parentId": "storage-group",
            "position": {"x": 20, "y": 50},
            "data": {
                "label": "Storage",
                "componentTypeId": "storage",
                "providerId": "pvc",
                "config": {"claimName": "nginx-pvc", "size": "20Gi"},
            },
        },
        {
            "id": "storage-db",
            "parentId": "storage-group",
            "position": {"x": 20, "y": 170},
            "data": {
                "label": "Storage",
                "componentTypeId": "storage",
                "providerId": "pvc",
                "config": {"claimName": "postgres-pvc", "size": "200Gi"},
            },
        },
        {
            "id": "storage-cache",
            "parentId": "storage-group",
            "position": {"x": 20, "y": 290},
            "data": {
                "label": "Storage",
                "componentTypeId": "storage",
                "providerId": "pvc",
                "config": {"claimName": "redis-pvc", "size": "50Gi"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "client", "target": "dns", "label": "HTTPS"},
        {"id": "e2", "source": "dns", "target": "lb", "label": "Load Balance"},
        {"id": "e3", "source": "lb", "target": "web", "label": "Route HTTP"},
        {"id": "e4", "source": "web", "target": "db", "label": "Read/Write"},
        {"id": "e5", "source": "web", "target": "cache", "label": "Cache/Session"},
        {"id": "e6", "source": "db", "target": "replica", "label": "Replicate"},
        {"id": "e7", "source": "web", "target": "storage-web", "label": "Read/Write"},
        {"id": "e8", "source": "db", "target": "storage-db", "label": "Read/Write"},
        {"id": "e9", "source": "cache", "target": "storage-cache", "label": "Read/Write"},
    ],
}


def seed_example_diagram_if_new_workspace(store: DiagramStore) -> None:
    """Create the example diagram, but only the very first time this
    workspace's diagrams/ directory is touched. Call once, at startup —
    not per-request/per-tool-call, since every other caller of DiagramStore
    (agent tools, tests) must see an actually-empty store when it is empty.
    """
    if store.root.is_dir():
        return
    store.create(EXAMPLE_DIAGRAM)


__all__ = ["EXAMPLE_DIAGRAM", "seed_example_diagram_if_new_workspace"]
