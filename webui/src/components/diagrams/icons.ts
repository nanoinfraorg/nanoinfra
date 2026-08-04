import {
  Cpu,
  Database,
  Flame,
  Gauge,
  Globe,
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
  database: Database,
  cache: Gauge,
  storage: HardDrive,
};
