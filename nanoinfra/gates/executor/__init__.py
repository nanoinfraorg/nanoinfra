"""The executor process and its wire -- nanoinfraorg/nanoinfra#18.

The agent holds nothing to reach a host with. This package holds the credential store, the four
transports, the target guard, the scope resolver, and the gate itself. The agent submits a
structured request over a Unix domain socket and renders the reply.

Import direction is the enforcement. ``nanoinfra/agent/tools/server_execution.py`` imports
``client`` and ``protocol`` only, and a test asserts that it reaches neither a backend nor the
secret store. So the property is checkable rather than merely intended.

The package binds three sockets. ``server`` answers the agent on the execute socket, and
``operator_socket`` answers a human on a socket of its own (#38). An answer on the execute socket
would let a compromised agent approve its own action, so the two never mix.

``scrub`` answers the agent on the third socket (#41). The agent used to build its redaction
sentinels itself, which decrypted every secret of the workspace inside the process the model runs
in. That work lives here now, and the agent sends one transcript text at a time.
"""
