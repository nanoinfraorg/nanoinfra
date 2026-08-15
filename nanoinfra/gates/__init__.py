"""Capability gate internals -- nanoinfraorg/nanoinfra#2.

The pieces split by role, and each one stays testable on its own:

- ``nanoinfra/config/gates.py`` holds the policy an operator declares (#7).
- ``nanoinfra/agent/tools/capabilities.py`` holds the class vocabulary (#3).
- ``tokens.py`` binds one approval to one resolved action (#12).
- ``approvals.py`` decides whether one approval counts, and whether one can exist (#13, #38).
- ``pending.py`` holds one suspended action while a human answers (#38).
- ``prompt.py`` renders the payload that human reads, from resolver output alone (#14).
- ``audit.py`` records every decision, append-only (#16).

The gate itself must not live in a hook. ``AgentHook.before_execute_tool``
(nanoinfra/agent/hook.py) returns ``None`` and cannot deny a call. Enforcement belongs at
the point that opens the transport, which is ``ExecuteOnServerTool.execute`` today and a
separate executor process after #18.
"""
