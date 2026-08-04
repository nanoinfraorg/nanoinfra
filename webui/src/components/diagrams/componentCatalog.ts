// Fake/mock catalog data for the Diagram Designer prototype.
// This stands in for the future "Infrastructure" Marketplace category —
// no backend call, no persistence, just enough shape to validate the UX.

// Sentinel id for the generic, nameable grouping container (e.g. "Kubernetes
// Cluster", "Scaling Group") — not a real Component Type/Provider, so it's
// handled separately from COMPONENT_TYPES wherever it's dropped/added.
export const GROUP_COMPONENT_ID = "__group__";

export type ProviderKind = "docker" | "api";

export interface ProviderField {
  key: string;
  label: string;
  placeholder?: string;
  kind: "text" | "secret";
}

export interface ComponentProvider {
  id: string;
  label: string;
  kind: ProviderKind;
  fields: ProviderField[];
}

export interface ComponentType {
  id: string;
  label: string;
  category: string;
  iconKey: IconKey;
  providers: ComponentProvider[];
}

export type IconKey =
  | "webServer"
  | "compute"
  | "application"
  | "database"
  | "cache"
  | "loadBalancer"
  | "reverseProxy"
  | "firewall"
  | "vpn"
  | "dns"
  | "storage"
  | "client";

export const COMPONENT_TYPES: ComponentType[] = [
  {
    id: "client",
    label: "Client",
    category: "Edge",
    iconKey: "client",
    providers: [{ id: "browser", label: "Browser / User", kind: "api", fields: [] }],
  },
  {
    id: "dns",
    label: "DNS",
    category: "Edge",
    iconKey: "dns",
    providers: [
      {
        id: "cloudflare",
        label: "Cloudflare DNS",
        kind: "api",
        fields: [{ key: "zone", label: "Zone", placeholder: "example.com", kind: "text" }],
      },
      { id: "route53", label: "AWS Route 53", kind: "api", fields: [{ key: "hostedZoneId", label: "Hosted zone ID", kind: "text" }] },
    ],
  },
  {
    id: "load_balancer",
    label: "Load Balancer",
    category: "Edge",
    iconKey: "loadBalancer",
    providers: [
      { id: "alb", label: "AWS Application Load Balancer", kind: "api", fields: [{ key: "targetGroup", label: "Target group", kind: "text" }] },
      { id: "nginx-lb", label: "nginx (upstream)", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "nginx:1.27", kind: "text" }] },
    ],
  },
  {
    id: "reverse_proxy",
    label: "Reverse Proxy",
    category: "Edge",
    iconKey: "reverseProxy",
    providers: [
      { id: "caddy", label: "Caddy", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "caddy:2", kind: "text" }] },
      { id: "nginx-proxy", label: "nginx", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "nginx:1.27", kind: "text" }] },
    ],
  },
  {
    id: "firewall",
    label: "Firewall",
    category: "Edge",
    iconKey: "firewall",
    providers: [
      { id: "ufw", label: "UFW (host firewall)", kind: "docker", fields: [{ key: "rules", label: "Allowed ports", placeholder: "22, 80, 443", kind: "text" }] },
      { id: "security-group", label: "AWS Security Group", kind: "api", fields: [{ key: "groupId", label: "Security group ID", kind: "text" }] },
    ],
  },
  {
    id: "vpn",
    label: "VPN",
    category: "Edge",
    iconKey: "vpn",
    providers: [
      { id: "wireguard", label: "WireGuard", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "linuxserver/wireguard", kind: "text" }] },
      { id: "tailscale", label: "Tailscale", kind: "api", fields: [{ key: "authKey", label: "Auth key", kind: "secret" }] },
    ],
  },
  {
    id: "web_server",
    label: "Web Server",
    category: "Compute",
    iconKey: "webServer",
    providers: [
      {
        id: "nginx",
        label: "nginx",
        kind: "docker",
        fields: [
          { key: "image", label: "Image tag", placeholder: "nginx:1.27", kind: "text" },
          { key: "domains", label: "Domains", placeholder: "example.com, www.example.com", kind: "text" },
          { key: "storagePath", label: "Storage path", placeholder: "/srv/websites/example", kind: "text" },
        ],
      },
      {
        id: "caddy-web",
        label: "Caddy",
        kind: "docker",
        fields: [
          { key: "image", label: "Image tag", placeholder: "caddy:2", kind: "text" },
          { key: "domains", label: "Domains", placeholder: "example.com", kind: "text" },
        ],
      },
    ],
  },
  {
    id: "compute",
    label: "GPU / NPU",
    category: "Compute",
    iconKey: "compute",
    providers: [
      { id: "nvidia-docker", label: "NVIDIA (Docker + CUDA)", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "nvidia/cuda:12.5-runtime", kind: "text" }] },
      { id: "ec2-gpu", label: "AWS EC2 GPU instance", kind: "api", fields: [{ key: "instanceType", label: "Instance type", placeholder: "g5.xlarge", kind: "text" }] },
    ],
  },
  {
    id: "application",
    label: "Application",
    category: "Applications",
    iconKey: "application",
    providers: [
      {
        id: "vllm",
        label: "vLLM",
        kind: "docker",
        fields: [
          { key: "image", label: "Image tag", placeholder: "vllm/vllm-openai:latest", kind: "text" },
          { key: "model", label: "Model", placeholder: "meta-llama/Llama-3.1-8B-Instruct", kind: "text" },
        ],
      },
      {
        id: "custom-docker-app",
        label: "Custom app (Docker)",
        kind: "docker",
        fields: [{ key: "image", label: "Image tag", placeholder: "myorg/myapp:latest", kind: "text" }],
      },
      {
        id: "custom-api-app",
        label: "Custom app (API)",
        kind: "api",
        fields: [{ key: "endpoint", label: "Endpoint", placeholder: "https://api.example.com", kind: "text" }],
      },
    ],
  },
  {
    id: "database",
    label: "Database",
    category: "Data",
    iconKey: "database",
    providers: [
      {
        id: "postgres",
        label: "PostgreSQL",
        kind: "docker",
        fields: [
          { key: "image", label: "Image tag", placeholder: "postgres:17", kind: "text" },
          { key: "password", label: "Admin password", kind: "secret" },
        ],
      },
      {
        id: "rds",
        label: "AWS RDS",
        kind: "api",
        fields: [{ key: "instanceId", label: "Instance ID", kind: "text" }, { key: "credentials", label: "Credentials", kind: "secret" }],
      },
    ],
  },
  {
    id: "cache",
    label: "Cache",
    category: "Data",
    iconKey: "cache",
    providers: [
      { id: "redis", label: "Redis", kind: "docker", fields: [{ key: "image", label: "Image tag", placeholder: "redis:7", kind: "text" }] },
    ],
  },
  {
    id: "storage",
    label: "Storage",
    category: "Data",
    iconKey: "storage",
    providers: [
      { id: "local-volume", label: "Local volume", kind: "docker", fields: [{ key: "path", label: "Host path", placeholder: "/srv/websites", kind: "text" }] },
      { id: "s3", label: "AWS S3", kind: "api", fields: [{ key: "bucket", label: "Bucket", kind: "text" }] },
    ],
  },
];

export function findComponentType(id: string): ComponentType | undefined {
  return COMPONENT_TYPES.find((c) => c.id === id);
}

export function findProvider(componentTypeId: string, providerId: string): ComponentProvider | undefined {
  return findComponentType(componentTypeId)?.providers.find((p) => p.id === providerId);
}
