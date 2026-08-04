import {
  Activity,
  Archive,
  Blocks,
  Box,
  Cpu,
  Database,
  Flame,
  Gauge,
  Globe,
  Group,
  HardDrive,
  KeyRound,
  Lock,
  Network,
  Route,
  ScrollText,
  Server,
  ShieldCheck,
  User,
  Waypoints,
  type LucideIcon,
} from "lucide-react";

// `iconKey` is free-form server data now (see componentCatalog.ts), not a
// closed TS union — a workspace catalog file can reference any key it
// likes. This map only covers the keys the built-in catalog ships with;
// getComponentIcon() falls back to a generic icon for anything else,
// instead of rendering `<Icon />` with `Icon === undefined` (which throws).
export const COMPONENT_ICONS: Record<string, LucideIcon> = {
  client: User,
  dns: Globe,
  loadBalancer: Waypoints,
  reverseProxy: ShieldCheck,
  firewall: Flame,
  vpn: KeyRound,
  webServer: Server,
  compute: Cpu,
  application: Blocks,
  database: Database,
  cache: Gauge,
  storage: HardDrive,
  group: Group,
  ingress: Route,
  auth: Lock,
  monitoring: Activity,
  logging: ScrollText,
  k8sService: Network,
  backup: Archive,
};

export const DEFAULT_COMPONENT_ICON: LucideIcon = Box;

export function getComponentIcon(iconKey: string): LucideIcon {
  return COMPONENT_ICONS[iconKey] ?? DEFAULT_COMPONENT_ICON;
}
