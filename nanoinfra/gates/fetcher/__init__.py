"""The fetcher process and its wire -- nanoinfraorg/nanoinfra#19.

Untrusted content enters the system here. ``web_fetch`` and ``web_search`` need broad egress, and
a page they read is written by whoever serves it. So the process that reads it holds nothing worth
taking: no host credential, no transport to a host, and no way to run a program.

What this process does hold is a search provider key, because an egress call to a search API needs
one. That key buys search results and authorizes nothing on any host. The credential store, the
four execution transports, and the gate stay in the executor (#18), and this package imports none
of them. ``tests/gates/test_fetcher_isolation.py`` walks the syntax tree of every module this
process loads and asserts both properties, so they are checkable rather than merely intended.

"Cannot exec" is the property #22 must preserve. Stdio MCP servers are subprocesses, and a
subprocess needs exec, so the two cannot both be true here. #22 resolves that. Until then no
module in this process imports ``subprocess``, calls ``os.system``, or calls ``os.exec*``.

The SSRF guards in ``nanoinfra/security/network.py`` come with the fetcher and stay necessary.
The split changes what a compromise yields. It does not change whether a request needs a check.
"""
