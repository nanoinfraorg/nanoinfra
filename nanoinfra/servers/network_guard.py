"""Target validation for Server execution -- deliberately narrower than
nanoinfra/security/network.py's general SSRF guard.

That guard blocks all of RFC1918 by default (it's meant for arbitrary,
possibly-attacker-influenced URLs). Servers are pre-configured
infrastructure targets, and most of them legitimately live in RFC1918
space -- blocking it here would make the whole module useless. What
still needs blocking: an agent (compromised or prompt-injected) creating
a Server pointed at the cloud-metadata address and then executing
against it to exfiltrate cloud credentials. Loopback and link-local are
blocked for the same reason -- there's no legitimate "the target server
is literally this gateway process" case.
"""

from __future__ import annotations

import ipaddress
import socket

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, includes cloud metadata (169.254.169.254)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_server_target(host: str) -> tuple[bool, str]:
    """Validate a bare host/IP (no scheme, no port) is safe to connect to."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked(addr):
            return False, f"Blocked: {host} is a loopback/link-local/metadata address"
        return True, ""

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"Cannot resolve host: {host} ({exc})"

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked(addr):
            return False, f"Blocked: {host} resolves to a loopback/link-local/metadata address ({addr})"
    return True, ""


__all__ = ["validate_server_target"]
