"""The stdio MCP host process and its wire -- nanoinfraorg/nanoinfra#22.

A stdio MCP server is a subprocess. The fetcher cannot exec, and #19 states that property as a
test. Both statements hold together only when the exec right moves to a process that owns it.
This package is that process.

What lives here:

- ``protocol.py`` frames the wire. A request names a configured server. It never names a program.
- ``server.py`` runs in the host. It resolves each server from its own config and starts the
  stdio child.
- ``client.py`` runs in the agent. It writes one frame and reads one frame.
- ``supervisor.py`` starts the host child. It runs on the supervisor's side of the split.

Three properties this package keeps, and ``tests/gates/test_mcp_host_isolation.py`` asserts each
one:

- The host holds no credential store. A compromise here yields no host credential.
- The host holds no HTTP transport. It refuses every server that is not stdio, so HTTP and SSE
  MCP transports stay in the agent behind the SSRF guards of ``.agent/security.md``.
- The agent starts no stdio child. ``nanoinfra/agent/tools/mcp.py`` imports the client and the
  protocol only.
"""
