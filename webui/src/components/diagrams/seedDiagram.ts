import { GROUP_COMPONENT_ID } from "./componentCatalog";
import type { Diagram } from "./diagramTypes";

export const SEED_DIAGRAM: Diagram = {
  id: "1c6e8754-0c69-4e08-a2cc-116fdea2131c",
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
        config: { image: "nginx:1.27", domains: "example.com, www.example.com" },
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
    {
      id: "storage-group",
      type: "groupBox",
      position: { x: 480, y: 620 },
      style: { width: 300, height: 420 },
      data: { label: "Storage via Isilon", componentTypeId: GROUP_COMPONENT_ID, providerId: "generic", config: {} },
    },
    {
      id: "storage-web",
      parentId: "storage-group",
      position: { x: 20, y: 50 },
      data: {
        label: "Storage",
        componentTypeId: "storage",
        providerId: "pvc",
        config: { claimName: "nginx-pvc", size: "20Gi" },
      },
    },
    {
      id: "storage-db",
      parentId: "storage-group",
      position: { x: 20, y: 170 },
      data: {
        label: "Storage",
        componentTypeId: "storage",
        providerId: "pvc",
        config: { claimName: "postgres-pvc", size: "200Gi" },
      },
    },
    {
      id: "storage-cache",
      parentId: "storage-group",
      position: { x: 20, y: 290 },
      data: {
        label: "Storage",
        componentTypeId: "storage",
        providerId: "pvc",
        config: { claimName: "redis-pvc", size: "50Gi" },
      },
    },
  ],
  edges: [
    { id: "e1", source: "client", target: "dns", label: "HTTPS" },
    { id: "e2", source: "dns", target: "lb", label: "Load Balance" },
    { id: "e3", source: "lb", target: "web", label: "Route HTTP" },
    { id: "e4", source: "web", target: "db", label: "Read/Write" },
    { id: "e5", source: "web", target: "cache", label: "Cache/Session" },
    { id: "e6", source: "db", target: "replica", label: "Replicate" },
    // These three connect a node to something inside the storage sidebar to
    // its right, not something below it — routing them via the side handles
    // keeps them a straight line across instead of a detour through
    // whatever node happens to sit between the two default top/bottom ones.
    { id: "e7", source: "web", target: "storage-web", label: "Read/Write", sourceHandle: "right", targetHandle: "left" },
    { id: "e8", source: "db", target: "storage-db", label: "Read/Write", sourceHandle: "right", targetHandle: "left" },
    { id: "e9", source: "cache", target: "storage-cache", label: "Read/Write", sourceHandle: "right", targetHandle: "left" },
  ],
};
