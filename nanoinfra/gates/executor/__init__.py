"""The executor process and its wire -- nanoinfraorg/nanoinfra#18.

The agent holds nothing to reach a host with. This package holds the credential store, the four
transports, the target guard, the scope resolver, and the gate itself. The agent submits a
structured request over a Unix domain socket and renders the reply.

Import direction is the enforcement. ``nanoinfra/agent/tools/server_execution.py`` imports
``client`` and ``protocol`` only, and a test asserts that it reaches neither a backend nor the
secret store. So the property is checkable rather than merely intended.

The package binds two sockets after #38. ``server`` answers the agent on the execute socket, and
``operator_socket`` answers a human on a socket of its own. An answer on the execute socket would
let a compromised agent approve its own action, so the two never mix.
"""
