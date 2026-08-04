// Suggests a default relationship label when the user draws a new connection,
// based on the two component types being joined. This is only a starting
// point — the label is stored on the edge and always user-editable afterward,
// since a real relationship (protocol/port/purpose) can't be fully inferred.
const PAIR_DEFAULTS: Record<string, string> = {
  "client->dns": "HTTPS",
  "client->load_balancer": "HTTPS",
  "client->reverse_proxy": "HTTPS",
  "dns->load_balancer": "Load Balance",
  "dns->reverse_proxy": "Route",
  "load_balancer->web_server": "Route HTTP",
  "load_balancer->reverse_proxy": "Route HTTP",
  "reverse_proxy->web_server": "Proxy",
  "web_server->database": "Read/Write",
  "web_server->cache": "Cache/Session",
  "database->database": "Replicate",
  "web_server->storage": "Read/Write",
  "client->vpn": "VPN Tunnel",
  "vpn->load_balancer": "Encrypted",
  "vpn->web_server": "Encrypted",
  "dns->firewall": "Filtered",
  "firewall->load_balancer": "Filtered",
  "firewall->web_server": "Filtered",
  "web_server->compute": "Offload",
};

export function defaultEdgeLabel(sourceComponentTypeId: string, targetComponentTypeId: string): string {
  const key = `${sourceComponentTypeId}->${targetComponentTypeId}`;
  return PAIR_DEFAULTS[key] ?? "connects_to";
}
