"""The record that knows a consent is in flight (#193).

This is the piece whose absence made a person carry the code by hand. A consent has two halves
separated by a browser: the URL goes out, and minutes later a redirect comes back carrying a
code that only means something to whoever issued the request. Between them somebody has to hold
the PKCE verifier, the state, the redirect that was used, and the scopes that were asked for.

In memory, with a TTL, and deliberately not on disk: a verifier outliving the process is a
secret sitting in a file for no reason, and a restart mid-flow costs one click.

The ``state`` is what authorises the callback. That route answers without the WebUI's bearer
token — Google redirects a browser carrying a cookie, not a header — so the only thing standing
between it and a stranger is a value that is unguessable, single-use, expiring, and names a
consent this deployment started. A callback matching no record answers 404 and records nothing.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

#: How long one consent may stay open. Long enough to pick an account and read a consent screen,
#: short enough that an abandoned flow does not keep a verifier around all afternoon.
CONSENT_TTL_S = 600.0

#: How many may be open at once. A bound because the start route is authenticated but a person
#: can still click twice, and an unbounded map is an unbounded map.
MAX_PENDING = 16


@dataclass(frozen=True, slots=True)
class PendingConsent:
    """One consent this deployment started and has not yet finished."""

    state: str
    connector: str
    credential: str
    client_id: str
    #: The client secret itself, for the one exchange this record exists to complete.
    #:
    #: Held here rather than stored and read back, and that is the difference between this
    #: module and one the agent process may not load: reading a plaintext out of the credential
    #: store is the thing `tests/agent/test_redaction_isolation.py` refuses, and it refuses it
    #: for a good reason. The value arrived in this process on the request that opened the flow,
    #: it stays for the consent's TTL, and it is written to the store only once the exchange has
    #: succeeded -- so an abandoned consent leaves nothing behind at all.
    client_secret: str
    verifier: str
    redirect_uri: str
    scopes: tuple[str, ...]
    created_at: float
    #: The person the WebUI authenticated when the flow started, for the record the write leaves.
    actor: str = ""

    def expired(self, now: float, ttl: float = CONSENT_TTL_S) -> bool:
        return now - self.created_at > ttl


@dataclass
class PendingConsentStore:
    """Every consent in flight, by state. One process, one store, no persistence."""

    ttl_s: float = CONSENT_TTL_S
    _entries: dict[str, PendingConsent] = field(default_factory=dict[str, PendingConsent])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def open(
        self,
        *,
        connector: str,
        credential: str,
        client_id: str,
        client_secret: str,
        verifier: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        actor: str = "",
    ) -> PendingConsent:
        """Record one consent and return it. The state it carries is the one to send to Google."""
        entry = PendingConsent(
            state=secrets.token_urlsafe(24),
            connector=connector,
            credential=credential,
            client_id=client_id,
            client_secret=client_secret,
            verifier=verifier,
            redirect_uri=redirect_uri,
            scopes=scopes,
            created_at=time.monotonic(),
            actor=actor,
        )
        with self._lock:
            self._prune(time.monotonic())
            if len(self._entries) >= MAX_PENDING:
                # Drop the oldest rather than refuse the newest: the person clicking now is
                # present, and the one who opened a flow ten minutes ago is not.
                oldest = min(self._entries.values(), key=lambda item: item.created_at)
                self._entries.pop(oldest.state, None)
            self._entries[entry.state] = entry
        return entry

    def take(self, state: str) -> PendingConsent | None:
        """Consume one consent by state. Single use: a replayed code finds nothing."""
        if not state:
            return None
        with self._lock:
            self._prune(time.monotonic())
            return self._entries.pop(state, None)

    def peek(self, state: str) -> PendingConsent | None:
        """Read without consuming. For a test, and for a caller that only needs to know."""
        with self._lock:
            self._prune(time.monotonic())
            return self._entries.get(state)

    def pending(self) -> tuple[PendingConsent, ...]:
        with self._lock:
            self._prune(time.monotonic())
            return tuple(self._entries.values())

    def _prune(self, now: float) -> None:
        for state in [
            state for state, entry in self._entries.items() if entry.expired(now, self.ttl_s)
        ]:
            self._entries.pop(state, None)


#: The store the routes share. One per process, because the two routes are two requests to the
#: same gateway and the record has to survive between them.
_STORE = PendingConsentStore()


def consent_store() -> PendingConsentStore:
    return _STORE


__all__ = [
    "CONSENT_TTL_S",
    "MAX_PENDING",
    "PendingConsent",
    "PendingConsentStore",
    "consent_store",
]
