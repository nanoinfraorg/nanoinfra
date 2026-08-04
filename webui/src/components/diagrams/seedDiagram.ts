import type { Diagram } from "./diagramTypes";

export const SEED_DIAGRAM: Diagram = {
  id: "seed-1",
  name: "Example: web app with cache + replica",
  targets: ["prod-web-01"],
  nodes: [
    {
      id: "client",
      position: { x: 40, y: 20 },
      data: { label: "User Client", componentTypeId: "client", providerId: "browser", config: {} },
    },
    {
      id: "dns",
      position: { x: 20, y: 140 },
      data: { label: "Cloudflare DNS", componentTypeId: "dns", providerId: "cloudflare", config: { zone: "example.com" } },
    },
    {
      id: "lb",
      position: { x: 20, y: 260 },
      data: { label: "Application Load Balancer", componentTypeId: "load_balancer", providerId: "alb", config: {} },
    },
    {
      id: "web",
      position: { x: 20, y: 380 },
      data: {
        label: "Web Application Server",
        componentTypeId: "web_server",
        providerId: "nginx",
        config: { image: "nginx:1.27", domains: "example.com, www.example.com", storagePath: "/srv/websites/example" },
      },
    },
    {
      id: "db",
      position: { x: -140, y: 520 },
      data: { label: "PostgreSQL Database", componentTypeId: "database", providerId: "postgres", config: { image: "postgres:17" } },
    },
    {
      id: "cache",
      position: { x: 180, y: 520 },
      data: { label: "Redis Cache", componentTypeId: "cache", providerId: "redis", config: { image: "redis:7" } },
    },
    {
      id: "replica",
      position: { x: -140, y: 640 },
      data: { label: "Read Replica", componentTypeId: "database", providerId: "postgres", config: { image: "postgres:17" } },
    },
  ],
  edges: [
    { id: "e1", source: "client", target: "dns", label: "HTTPS" },
    { id: "e2", source: "dns", target: "lb", label: "Load Balance" },
    { id: "e3", source: "lb", target: "web", label: "Route HTTP" },
    { id: "e4", source: "web", target: "db", label: "Read/Write" },
    { id: "e5", source: "web", target: "cache", label: "Cache/Session" },
    { id: "e6", source: "db", target: "replica", label: "Replicate" },
  ],
};
