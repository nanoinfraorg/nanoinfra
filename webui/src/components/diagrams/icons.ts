import {
  Blocks,
  Cpu,
  Database,
  Flame,
  Gauge,
  Globe,
  Group,
  HardDrive,
  KeyRound,
  Server,
  ShieldCheck,
  User,
  Waypoints,
  type LucideIcon,
} from "lucide-react";

import type { IconKey } from "./componentCatalog";

export const COMPONENT_ICONS: Record<IconKey, LucideIcon> = {
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
};
