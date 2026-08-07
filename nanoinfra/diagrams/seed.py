"""Starter examples shown the first time a workspace's Infra Diagrams gallery
is opened — an empty list with nothing to click into isn't useful on a fresh
install.

Only fires once per workspace: the check is "has this workspace's diagrams/
directory ever been created", not "is the list currently empty" — deleting
the examples (or every other diagram) must never bring them back.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.diagrams.store import DiagramStore

# Mirrors the old client-only webui/src/components/diagrams/seedDiagram.ts
# (removed once diagrams moved to real backend persistence) plus the later
# Storage-group addition — a realistic, moderately complex example that
# exercises groups, nesting, and the Model source/Storage path connection
# linking in one diagram.
_WEB_APP_DIAGRAM: dict[str, Any] = {
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

# A primary/replica DB cluster behind a connection pooler -- the pooler is
# represented as a Load Balancer node (there's no dedicated "DB proxy"
# catalog type) with its own label/config renamed to PgBouncer; the
# component's role is the free-text `data.label`, not the provider id.
_DB_CLUSTER_DIAGRAM: dict[str, Any] = {
    "name": "Example: PostgreSQL HA cluster",
    "targets": [],
    "nodes": [
        {
            "id": "app-servers",
            "position": {"x": 40, "y": 40},
            "data": {
                "label": "App servers",
                "componentTypeId": "application",
                "providerId": "custom-docker-app",
                "config": {"image": "myorg/api:latest"},
            },
        },
        {
            "id": "pgbouncer",
            "position": {"x": 380, "y": 40},
            "data": {
                "label": "PgBouncer (connection pool)",
                "componentTypeId": "load_balancer",
                "providerId": "nginx-lb",
                "config": {"image": "pgbouncer:1.22"},
            },
        },
        {
            "id": "pg-cluster",
            "type": "groupBox",
            "position": {"x": 40, "y": 210},
            "style": {"width": 340, "height": 420},
            "data": {
                "label": "PostgreSQL HA Cluster (Patroni)",
                "componentTypeId": "__group__",
                "providerId": "generic",
                "config": {},
            },
        },
        {
            "id": "pg-primary",
            "parentId": "pg-cluster",
            "position": {"x": 20, "y": 50},
            "data": {
                "label": "Primary",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "pg-replica-1",
            "parentId": "pg-cluster",
            "position": {"x": 20, "y": 170},
            "data": {
                "label": "Replica 1",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "pg-replica-2",
            "parentId": "pg-cluster",
            "position": {"x": 20, "y": 290},
            "data": {
                "label": "Replica 2",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "app-servers", "target": "pgbouncer", "label": "Query"},
        {"id": "e2", "source": "pgbouncer", "target": "pg-primary", "label": "Write"},
        {"id": "e3", "source": "pgbouncer", "target": "pg-replica-1", "label": "Read"},
        {"id": "e4", "source": "pgbouncer", "target": "pg-replica-2", "label": "Read"},
        {"id": "e5", "source": "pg-primary", "target": "pg-replica-1", "label": "Replicate"},
        {"id": "e6", "source": "pg-primary", "target": "pg-replica-2", "label": "Replicate"},
    ],
}

# Active/passive NFS pair with block-level replication and a failover
# watchdog -- Pacemaker/Corosync isn't a real catalog provider either, so
# it's represented the same way: an Automation/cron node whose label and
# config describe what it actually does instead of a literal cron schedule.
_NFS_HA_DIAGRAM: dict[str, Any] = {
    "name": "Example: NFS high availability",
    "targets": [],
    "nodes": [
        {
            "id": "nfs-clients",
            "position": {"x": 40, "y": 40},
            "data": {
                "label": "App servers (NFS clients)",
                "componentTypeId": "application",
                "providerId": "custom-docker-app",
                "config": {"image": "myorg/worker:latest"},
            },
        },
        {
            "id": "vip-keepalived",
            "position": {"x": 380, "y": 40},
            "data": {
                "label": "Virtual IP (keepalived)",
                "componentTypeId": "load_balancer",
                "providerId": "nginx-lb",
                "config": {"image": "keepalived:2.3"},
            },
        },
        {
            "id": "nfs-ha-group",
            "type": "groupBox",
            "position": {"x": 40, "y": 210},
            "style": {"width": 340, "height": 460},
            "data": {
                "label": "NFS HA Pair (DRBD + Pacemaker)",
                "componentTypeId": "__group__",
                "providerId": "generic",
                "config": {},
            },
        },
        {
            "id": "nfs-active",
            "parentId": "nfs-ha-group",
            "position": {"x": 20, "y": 50},
            "data": {
                "label": "NFS server (active)",
                "componentTypeId": "storage",
                "providerId": "local-volume",
                "config": {"path": "/export/data"},
            },
        },
        {
            "id": "nfs-standby",
            "parentId": "nfs-ha-group",
            "position": {"x": 20, "y": 170},
            "data": {
                "label": "NFS server (standby)",
                "componentTypeId": "storage",
                "providerId": "local-volume",
                "config": {"path": "/export/data"},
            },
        },
        {
            "id": "pacemaker",
            "parentId": "nfs-ha-group",
            "position": {"x": 20, "y": 290},
            "data": {
                "label": "Pacemaker + Corosync (failover watchdog)",
                "componentTypeId": "automation",
                "providerId": "cron",
                "config": {
                    "command": "monitors nfs-active, promotes standby on failure",
                    "schedule": "continuous",
                },
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "nfs-clients", "target": "vip-keepalived", "label": "NFS mount"},
        {"id": "e2", "source": "vip-keepalived", "target": "nfs-active", "label": "active"},
        {"id": "e3", "source": "nfs-active", "target": "nfs-standby", "label": "DRBD sync (block replication)"},
        {"id": "e4", "source": "pacemaker", "target": "nfs-active", "label": "health check"},
        {"id": "e5", "source": "pacemaker", "target": "vip-keepalived", "label": "moves VIP on failover"},
    ],
}

# Cross-region failover with a warm standby -- there's no dedicated "DR"
# catalog type either, so this leans on what already fits the role: Route 53
# for failover DNS, a second region as a plain group, and Backup/Storage(S3)
# for the actual cross-region data path a DR plan depends on.
_DR_DIAGRAM: dict[str, Any] = {
    "name": "Example: disaster recovery (cross-region failover)",
    "targets": [],
    "nodes": [
        {
            "id": "dns-failover",
            "position": {"x": 40, "y": 40},
            "data": {
                "label": "Route 53 (failover routing)",
                "componentTypeId": "dns",
                "providerId": "route53",
                "config": {"hostedZoneId": "Z0PRIMARY"},
            },
        },
        {
            "id": "primary-region",
            "type": "groupBox",
            "position": {"x": 40, "y": 210},
            "style": {"width": 340, "height": 300},
            "data": {
                "label": "Primary Region (us-east-1)",
                "componentTypeId": "__group__",
                "providerId": "generic",
                "config": {},
            },
        },
        {
            "id": "app-primary",
            "parentId": "primary-region",
            "position": {"x": 20, "y": 50},
            "data": {
                "label": "App (primary)",
                "componentTypeId": "application",
                "providerId": "custom-docker-app",
                "config": {"image": "myorg/api:latest"},
            },
        },
        {
            "id": "db-primary",
            "parentId": "primary-region",
            "position": {"x": 20, "y": 170},
            "data": {
                "label": "PostgreSQL (primary)",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "dr-region",
            "type": "groupBox",
            "position": {"x": 420, "y": 210},
            "style": {"width": 340, "height": 300},
            "data": {
                "label": "DR Region (us-west-2) — standby",
                "componentTypeId": "__group__",
                "providerId": "generic",
                "config": {},
            },
        },
        {
            "id": "app-standby",
            "parentId": "dr-region",
            "position": {"x": 20, "y": 50},
            "data": {
                "label": "App (standby, scaled to 0)",
                "componentTypeId": "application",
                "providerId": "custom-docker-app",
                "config": {"image": "myorg/api:latest"},
            },
        },
        {
            "id": "db-standby",
            "parentId": "dr-region",
            "position": {"x": 20, "y": 170},
            "data": {
                "label": "PostgreSQL (warm standby)",
                "componentTypeId": "database",
                "providerId": "postgres",
                "config": {"image": "postgres:17"},
            },
        },
        {
            "id": "velero",
            "position": {"x": 800, "y": 40},
            "data": {
                "label": "Velero (scheduled backups)",
                "componentTypeId": "backup",
                "providerId": "velero",
                "config": {"schedule": "0 2 * * *", "retention": "30d"},
            },
        },
        {
            "id": "backup-s3",
            "position": {"x": 800, "y": 210},
            "data": {
                "label": "Cross-region backup (S3)",
                "componentTypeId": "storage",
                "providerId": "s3",
                "config": {"bucket": "acme-dr-backups"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "dns-failover", "target": "app-primary", "label": "primary (active)"},
        {"id": "e2", "source": "dns-failover", "target": "app-standby", "label": "failover (standby)"},
        {"id": "e3", "source": "app-primary", "target": "db-primary", "label": "Read/Write"},
        {"id": "e4", "source": "app-standby", "target": "db-standby", "label": "Read/Write (standby)"},
        {"id": "e5", "source": "db-primary", "target": "db-standby", "label": "Cross-region replication"},
        {"id": "e6", "source": "velero", "target": "db-primary", "label": "backs up"},
        {"id": "e7", "source": "velero", "target": "backup-s3", "label": "stores snapshot"},
        {"id": "e8", "source": "backup-s3", "target": "db-standby", "label": "restores from (DR drill)"},
    ],
}

_AWS_ECOMMERCE_DIAGRAM: dict[str, Any] = {
    "name": "Example: e-commerce on AWS",
    "targets": [],
    "nodes": [
        {
            "id": "dns-route53",
            "position": {"x": 40, "y": 20},
            "data": {"label": "Route 53", "componentTypeId": "dns", "providerId": "route53", "config": {}},
        },
        {
            "id": "alb",
            "position": {"x": 20, "y": 140},
            "data": {
                "label": "Application Load Balancer",
                "componentTypeId": "load_balancer",
                "providerId": "alb",
                "config": {"targetGroup": "storefront-tg"},
            },
        },
        {
            "id": "ecs-app",
            "position": {"x": 20, "y": 260},
            "data": {
                "label": "Storefront API (ECS Fargate)",
                "componentTypeId": "application",
                "providerId": "ecs-fargate",
                "config": {"image": "myorg/storefront:latest", "taskCpu": "2 vCPU / 4GB"},
            },
        },
        {
            "id": "rds-orders",
            "position": {"x": -140, "y": 400},
            "data": {
                "label": "Orders DB (RDS PostgreSQL)",
                "componentTypeId": "database",
                "providerId": "rds",
                "config": {"instanceId": "orders-db-prod"},
            },
        },
        {
            "id": "elasticache-sessions",
            "position": {"x": 180, "y": 400},
            "data": {
                "label": "Session cache (ElastiCache)",
                "componentTypeId": "cache",
                "providerId": "elasticache",
                "config": {"clusterId": "sessions-cache"},
            },
        },
        {
            "id": "s3-assets",
            "position": {"x": 20, "y": 520},
            "data": {
                "label": "Product images (S3)",
                "componentTypeId": "storage",
                "providerId": "s3",
                "config": {"bucket": "acme-shop-assets"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "dns-route53", "target": "alb", "label": "HTTPS"},
        {"id": "e2", "source": "alb", "target": "ecs-app", "label": "Route HTTP"},
        {"id": "e3", "source": "ecs-app", "target": "rds-orders", "label": "Read/Write"},
        {"id": "e4", "source": "ecs-app", "target": "elasticache-sessions", "label": "Cache/Session"},
        {"id": "e5", "source": "ecs-app", "target": "s3-assets", "label": "Read/Write"},
    ],
}

_AZURE_BLOG_DIAGRAM: dict[str, Any] = {
    "name": "Example: blog on Azure",
    "targets": [],
    "nodes": [
        {
            "id": "azure-dns",
            "position": {"x": 40, "y": 20},
            "data": {
                "label": "Azure DNS",
                "componentTypeId": "dns",
                "providerId": "azure-dns",
                "config": {"resourceGroup": "rg-blog-prod"},
            },
        },
        {
            "id": "app-gateway",
            "position": {"x": 20, "y": 140},
            "data": {
                "label": "Application Gateway",
                "componentTypeId": "load_balancer",
                "providerId": "app-gateway",
                "config": {"backendPool": "blog-app-pool"},
            },
        },
        {
            "id": "app-service",
            "position": {"x": 20, "y": 260},
            "data": {
                "label": "Blog (App Service)",
                "componentTypeId": "application",
                "providerId": "app-service",
                "config": {"image": "myorg/blog:latest", "planTier": "P1v3"},
            },
        },
        {
            "id": "azure-postgresql",
            "position": {"x": -140, "y": 400},
            "data": {
                "label": "Posts DB (Azure PostgreSQL)",
                "componentTypeId": "database",
                "providerId": "azure-postgresql",
                "config": {"serverName": "blog-pg-prod"},
            },
        },
        {
            "id": "azure-blob",
            "position": {"x": 180, "y": 400},
            "data": {
                "label": "Media (Blob Storage)",
                "componentTypeId": "storage",
                "providerId": "azure-blob",
                "config": {"container": "blog-media"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "azure-dns", "target": "app-gateway", "label": "HTTPS"},
        {"id": "e2", "source": "app-gateway", "target": "app-service", "label": "Route HTTP"},
        {"id": "e3", "source": "app-service", "target": "azure-postgresql", "label": "Read/Write"},
        {"id": "e4", "source": "app-service", "target": "azure-blob", "label": "Read/Write"},
    ],
}

# The "something else" pick -- a GKE-native microservices API, not another
# blog/e-commerce shape. Uses cloud-managed services at the edge/data layer
# (Cloud DNS/LB, Cloud SQL, Memorystore, Cloud Storage) but self-hosted
# in-cluster tooling for everything K8s-internal (ingress, service, API
# gateway, metrics) -- the realistic split for an actual GKE deployment, and
# the first seed example to touch ingress_controller/k8s_service/
# auth_gateway/monitoring at all.
_GCP_MICROSERVICES_DIAGRAM: dict[str, Any] = {
    "name": "Example: GKE microservices API on GCP",
    "targets": [],
    "nodes": [
        {
            "id": "cloud-dns",
            "position": {"x": 40, "y": 20},
            "data": {
                "label": "Cloud DNS",
                "componentTypeId": "dns",
                "providerId": "cloud-dns",
                "config": {"project": "acme-platform"},
            },
        },
        {
            "id": "gcp-lb",
            "position": {"x": 20, "y": 140},
            "data": {
                "label": "Cloud Load Balancing",
                "componentTypeId": "load_balancer",
                "providerId": "gcp-lb",
                "config": {"backendService": "orders-api-backend"},
            },
        },
        {
            "id": "nginx-ingress",
            "position": {"x": 20, "y": 260},
            "data": {
                "label": "Ingress Controller",
                "componentTypeId": "ingress_controller",
                "providerId": "nginx-ingress",
                "config": {"image": "registry.k8s.io/ingress-nginx/controller:v1.11.2", "rateLimit": "100 req/s per IP"},
            },
        },
        {
            "id": "kong",
            "position": {"x": 20, "y": 380},
            "data": {
                "label": "API Gateway (Kong)",
                "componentTypeId": "auth_gateway",
                "providerId": "kong",
                "config": {"image": "kong:3.7", "authType": "JWT"},
            },
        },
        {
            "id": "k8s-svc",
            "position": {"x": 20, "y": 500},
            "data": {
                "label": "orders-api Service",
                "componentTypeId": "k8s_service",
                "providerId": "cluster-ip",
                "config": {"port": "8000", "targetPort": "8000"},
            },
        },
        {
            "id": "orders-api",
            "position": {"x": 20, "y": 620},
            "data": {
                "label": "Orders API (GKE pod)",
                "componentTypeId": "application",
                "providerId": "custom-docker-app",
                "config": {"image": "myorg/orders-api:latest", "replicas": "3–10"},
            },
        },
        {
            "id": "cloud-sql",
            "position": {"x": -220, "y": 760},
            "data": {
                "label": "Orders DB (Cloud SQL)",
                "componentTypeId": "database",
                "providerId": "cloud-sql",
                "config": {"instanceConnectionName": "acme-platform:us-central1:orders-db"},
            },
        },
        {
            "id": "memorystore",
            "position": {"x": 80, "y": 760},
            "data": {
                "label": "Session cache (Memorystore)",
                "componentTypeId": "cache",
                "providerId": "memorystore",
                "config": {"instanceId": "orders-cache"},
            },
        },
        {
            "id": "gcs",
            "position": {"x": 380, "y": 760},
            "data": {
                "label": "Order exports (Cloud Storage)",
                "componentTypeId": "storage",
                "providerId": "gcs",
                "config": {"bucket": "acme-orders-exports"},
            },
        },
        {
            "id": "prometheus",
            "position": {"x": 500, "y": 620},
            "data": {
                "label": "Prometheus (metrics)",
                "componentTypeId": "monitoring",
                "providerId": "prometheus",
                "config": {"image": "prom/prometheus:v2.54.1", "scrapeInterval": "15s"},
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "cloud-dns", "target": "gcp-lb", "label": "DNS"},
        {"id": "e2", "source": "gcp-lb", "target": "nginx-ingress", "label": "Route HTTP"},
        {"id": "e3", "source": "nginx-ingress", "target": "kong", "label": "Proxy"},
        {"id": "e4", "source": "kong", "target": "k8s-svc", "label": "Authenticated request"},
        {"id": "e5", "source": "k8s-svc", "target": "orders-api", "label": "Route to pod"},
        {"id": "e6", "source": "orders-api", "target": "cloud-sql", "label": "Read/Write"},
        {"id": "e7", "source": "orders-api", "target": "memorystore", "label": "Cache/Session"},
        {"id": "e8", "source": "orders-api", "target": "gcs", "label": "Read/Write"},
        {"id": "e9", "source": "prometheus", "target": "orders-api", "label": "scrapes metrics"},
    ],
}

EXAMPLE_DIAGRAMS: list[dict[str, Any]] = [
    _WEB_APP_DIAGRAM,
    _DB_CLUSTER_DIAGRAM,
    _NFS_HA_DIAGRAM,
    _DR_DIAGRAM,
    _AWS_ECOMMERCE_DIAGRAM,
    _AZURE_BLOG_DIAGRAM,
    _GCP_MICROSERVICES_DIAGRAM,
]


def seed_example_diagram_if_new_workspace(store: DiagramStore) -> None:
    """Create the example diagrams, but only the very first time this
    workspace's diagrams/ directory is touched. Call once, at startup —
    not per-request/per-tool-call, since every other caller of DiagramStore
    (agent tools, tests) must see an actually-empty store when it is empty.
    """
    if store.root.is_dir():
        return
    for raw in EXAMPLE_DIAGRAMS:
        store.create(raw)


__all__ = ["EXAMPLE_DIAGRAMS", "seed_example_diagram_if_new_workspace"]
