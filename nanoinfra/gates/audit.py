"""The append-only record of every gate decision -- nanoinfraorg/nanoinfra#16.

One line per decision, and the line covers a denial, an expiry, and a latched refusal too.
A store that records only the allows answers no question an incident asks.

**Append-only in the real sense.** A record goes to the file through ``O_APPEND`` and one
``os.write`` call. ``nanoinfra/servers/job_store.py`` and ``nanoinfra/pairing/store.py``
read the whole file, change it in memory, and write the whole file back. That shape suits
those two stores, because each one owns mutable state. It is wrong here. A whole-file
rewrite can lose every earlier record when the process dies mid-write. Lost earlier records
are the one failure an audit log may not have. ``O_APPEND`` also removes the need for a
lock, because the kernel puts each write at the true end of the file. A lock guards one
process only, and #18 moves the gate into a separate executor process.

The writer probes nothing before it appends. A tail probe cannot be race free while other
writers append. A reader can observe a file size that a write in flight has not filled yet.
The probe then reports a torn tail that does not exist. A false alarm in an audit log costs
more than the case it guards. ``fsync`` runs per record, so only a power loss or a kernel
death can tear a line, and the tear reaches the last line only. The next record after such
a tear joins the stump, and both of those lines go. :meth:`AuditStore.read_all` names the
file and the line number, and it returns every intact record around the damage.

**Digest by default.** The record carries ``command_digest``. It carries ``command_text``
only when ``gates.audit.record_command_text`` is true. Resolved commands routinely embed
secrets, so a log that captures them becomes a second secret store. One closed gap must not
open another gap.

**The outcome is a second record (#46).** ``exit_code`` and ``duration_ms`` stayed null on
every executor record, so the log said what the gate decided and never what happened next.
:meth:`AuditStore.record_completion` appends one record when the action ends, and it fills the
two fields. It is an append, and never an edit of the decision record. The decision lands
before the action runs, and that order is the property this module exists for. An edit
afterwards would make an append-only log mutable.

Every record carries a ``record_id``, and a completion carries the id of the decision it
follows in ``follows``. A reader pairs the two records by that id. A pair found by two
timestamps is a guess, because two actions can decide inside one millisecond.

A completion holds no command output. This module keeps a digest of the command rather than
the text, and output carries the same risk. It also holds no grant, no approval, no actor,
and no secret ref. The decision record it names holds those answers, so one authorization
cannot read two ways.

**Two identities, and one of them is a claim (#79).** ``actor`` names the person who answered,
and a path this deployment trusts authenticated them. ``origin_actor`` names the person the
request came from, and **it is an assertion of the agent that nothing here verified.**
``nanoinfra/gates/executor/protocol.py`` says the same about ``origin_path``: a compromised agent
can claim any origin, and it can claim any person with it. So a reviewer reads ``origin_actor``
as a claim of the request rather than as an authentication, and #68 states what a deployment
gives up when it lets that claim widen who may answer. Both record kinds carry the field, so one
filter on a person shows a decision and the outcome that followed it. Neither field ever holds
blank text: an empty name reads as a person, so "nobody" is ``null``.

**Separate from the transcripts.** These records belong under ``gates/audit/`` in the data
dir. They never enter the session history, and they have their own retention. This module
also stays independent of ``nanoinfra/bus/runtime_events.py``. That bus is in-memory pub/sub
for live WebUI state. It has no durability, and it is not the audit trail.

**Retention by date-stamped segments.** Records go to ``gate-YYYY-MM-DD.jsonl``, one segment
per UTC day. :meth:`AuditStore.prune` then deletes whole segments outside
``gates.audit.retention_days``. The alternative is one file plus a filter-and-rewrite pass.
That pass must read every record it keeps and write it again. It therefore reintroduces the
exact rewrite this module refuses. It also changes bytes that an auditor may hold a hash of.
Deletion of a whole segment touches zero bytes of the records it keeps. The cost is
granularity. The oldest kept segment holds records up to one day past the limit. Retention
over-keeps by less than a day, and an over-keep is the safe direction of error here.

**The root is a directory, not a path.** #36 found that the agent account could rename the
audit directory. Write rights on a parent allow a rename of any entry inside it, whatever the
entry's own owner and mode are. The log then read as empty, and #32 rebuilt no latch from it.
``entrypoint.sh`` closes the rename. A store opened with ``pin_root`` also holds the device
and inode of the directory it opened, and it refuses to read or write when that pair changes.
The refusal is an :class:`AuditRootChangedError`, which is an ``OSError``, so the executor refuses
the action and the latch restore degrades. A rename then costs availability, not a latch.

Nothing here reads config or the data dir on its own. The caller passes the root and the
policy, so #8 wires one store and a test writes to a temporary directory.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from loguru import logger

# The digest format lives with the class vocabulary, so the log-only recorder in
# nanoinfra/agent/tools/capabilities.py and this store always agree on one string.
from nanoinfra.agent.tools.capabilities import command_digest as _command_digest
from nanoinfra.config.gates import AuditConfig

_SEGMENT_PREFIX = "gate-"
_SEGMENT_SUFFIX = ".jsonl"
_SEGMENT_DATE_FORMAT = "%Y-%m-%d"

# What the `decision` field holds on a completion record (#46). The value names a record kind
# rather than an answer a gate gave, because nothing decides at the end of an action. It sits
# beside the decision values on purpose: one field, one filter, and one row per fact.
#
# `nanoinfra/webui/audit_api.py` imports this name for its filter list, so the viewer offers the
# value with no second copy of the string.
DECISION_COMPLETION = "completion"

# An audit record names hosts, actors, and sessions. Only the owner needs to read it.
# The executor writes this log and the agent process reads it: #32 restores latches there, and
# #29's viewer serves it. So the group reads and never writes, and other users get nothing. A
# reader that cannot open a segment leaves the deployment permanently degraded, which is worse
# than the bypass that would close. The log holds command digests by default, and full text
# needs gates.audit.recordCommandText.
_FILE_MODE = 0o640
_DIR_MODE = 0o2750


def segment_name(day: datetime) -> str:
    """Return the segment file name that holds a record from *day* (UTC)."""
    return f"{_SEGMENT_PREFIX}{day.astimezone(UTC).strftime(_SEGMENT_DATE_FORMAT)}{_SEGMENT_SUFFIX}"


class AuditRootChangedError(OSError):
    """The audit root is not the directory the store opened -- #36.

    An ``OSError`` subclass on purpose. Every caller of this store already fails closed on an
    ``OSError``: the executor refuses the action it did not record, and ``restore_latches``
    degrades and keeps every session latched. So no caller needs a new branch, and a moved
    audit root costs availability instead of every latch the log holds.
    """


def root_identity(root: Path) -> tuple[int, int] | None:
    """Return the device and inode pair of *root*, or ``None`` when no directory is there.

    The pair is the identity of a directory, and the path is not. A rename keeps the pair and
    changes the path. A rename aside plus a fresh ``mkdir`` keeps the path and changes the
    pair. The second case is the #36 bypass, so a long-lived process must hold the pair.

    A path that is not a directory answers ``None``. A file or a symlink to another directory
    at the audit root is not the audit root, and it must not read as one.
    """
    try:
        info = os.stat(root)
    except OSError:
        # An absent root and an unreadable one answer the same here. The caller compares this
        # answer with the pinned pair, and neither one can equal a pinned pair.
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


class AuditStore:
    """Append gate decisions to date-stamped JSONL segments under one root."""

    def __init__(
        self, root: Path | str, *, config: AuditConfig | None = None, pin_root: bool = False
    ) -> None:
        self.root = Path(root)
        self.config = config or AuditConfig()
        # Opt-in, because the pin guards one thing: a process that outlives a rename of its
        # audit root. The executor is that process, and it takes the pin. A short-lived caller
        # that opens the store, reads it, and exits gains nothing from a pin, and a pin there
        # would turn an operator who moves the directory into a hard failure.
        self.pin_root = pin_root
        self.pinned_identity = root_identity(self.root) if pin_root else None

    def verify_root(self) -> None:
        """Raise :class:`AuditRootChangedError` when the root is not the pinned directory -- #36.

        One ``stat`` call, so every record and every read can afford this check.

        A pinned identity of ``None`` means the directory did not exist yet. There is nothing
        to protect in that state, so the first directory this store sees becomes the pinned
        one. A fresh install opens the store before the first record creates the root.
        """
        if not self.pin_root:
            return
        current = root_identity(self.root)
        if self.pinned_identity is None:
            self.pinned_identity = current
            return
        if current == self.pinned_identity:
            return
        logger.error(
            "gates: the audit root {} changed identity (pinned {}, found {})",
            self.root,
            self.pinned_identity,
            current,
        )
        raise AuditRootChangedError(
            f"The audit root {self.root} is not the directory this process opened "
            f"(pinned device and inode {self.pinned_identity}, found {current}). A rename or a "
            "replacement of the audit root hides the records that the denial latches come from, "
            "so this store refuses to read it or to write to it."
        )

    def record(
        self,
        *,
        decision: str,
        capability_class: str,
        execution_context: str,
        tool: str | None = None,
        session_id: str | None = None,
        origin_path: str | None = None,
        approval_path: str | None = None,
        origin_actor: str | None = None,
        actor: str | None = None,
        scope: str | None = None,
        hosts: Sequence[str] | None = None,
        secret_ref: str | None = None,
        command: str | None = None,
        command_digest: str | None = None,
        reason: str | None = None,
        grant_id: str | None = None,
        approval_id: str | None = None,
        token_nonce: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        follows: str | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Write one decision and return the record as it went to disk.

        ``command`` takes the resolved command string. The store digests it here, so no
        caller has to remember the rule. The text reaches the file only under the opt-in.
        ``command_digest`` covers the caller that holds a digest and no text.

        ``secret_ref`` and ``approval_id`` belong to a ``credential.access`` decision (#39).
        The first names the credential a decryption covered, and it is an id rather than a
        value. The second names the suspended action a human answered, so a reviewer can ask
        which approval authorized one decryption. Neither one is a bearer value. The nonce
        stays out of this log for that exact reason, and ``token_nonce`` predates this rule.

        ``actor`` names **the person who answered**, in the vocabulary of the path that
        authenticated them: ``webui:<claim>`` for a verified assertion, the channel's own
        authenticated sender id for a chat answer, and the bare path name for a deployment that
        authenticated a shared token and nobody (#64). It is ``None`` where nobody answered at
        all, which is the case for a decision a grant covered and for the record that suspends
        an action before an answer exists. A blank string must never stand for that: empty text
        reads as a name.

        ``origin_actor`` names **the person the request came from** (#79). It is the second half
        of the question a reviewer asks with #68 on: who asked, and who approved. **The value is
        an assertion of the agent, and nothing here verified it.** The executor treats
        ``origin_path`` the same way, and ``origin_actor`` inherits that exactly: a compromised
        agent can claim any person. A reviewer who reads a name in this field must read it as a
        claim rather than as an authentication. ``actor`` is the other case: a path that this
        deployment trusts authenticated that person before the answer counted.

        The value is ``None`` where the origin path authenticated nobody. This method turns blank
        text into ``None``, so no writer can put an empty name in the log. #67 keeps the two apart
        on the wire for that reason, and the record holds the same line.

        ``same_path`` and ``host_count`` are not parameters on purpose. Both derive from
        other fields, so a derived value cannot contradict them. #13 keys an out-of-band
        approval on the two paths. The record must not claim a separate path that the two
        paths themselves deny.

        ``record_id`` is not a parameter either. This store names every record it writes, so
        no caller can hand two records one name. ``follows`` takes the id of an earlier
        record, and :meth:`record_completion` is the one caller that fills it (#46).

        Raises ``OSError`` when the write fails. The caller then knows the decision reached
        no durable record, so #8 can refuse the action instead of a run that nothing records.
        """
        moment = (ts or datetime.now(UTC)).astimezone(UTC)
        host_list = [str(host) for host in (hosts or ())]
        digest = _command_digest(command) if command is not None else command_digest
        payload: dict[str, Any] = {
            "ts": moment.isoformat(),
            # The name of this record, and the name a completion record points at (#46).
            "record_id": uuid.uuid4().hex,
            "follows": follows,
            "session_id": session_id,
            "execution_context": execution_context,
            "origin_path": origin_path,
            "approval_path": approval_path,
            "same_path": _same_path(origin_path, approval_path),
            # Who asked, beside who answered (#79). The gate strips this value before it compares
            # it, so the record holds the text that decided.
            "origin_actor": _named_or_none(origin_actor),
            "actor": actor,
            "capability_class": capability_class,
            "scope": scope,
            "hosts": host_list,
            "host_count": len(host_list),
            "secret_ref": secret_ref,
            "command_digest": digest,
            "decision": decision,
            "reason": reason,
            "grant_id": grant_id,
            "approval_id": approval_id,
            "token_nonce": token_nonce,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "tool": tool,
        }
        if command is not None and self.config.record_command_text:
            payload["command_text"] = command
        self._append(self.root / segment_name(moment), payload, moment)
        return payload

    def record_completion(
        self,
        *,
        follows: Mapping[str, Any],
        exit_code: int | None,
        duration_ms: int | None,
        reason: str | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Append the outcome of one action as its own record and return it (#46).

        ``follows`` is the decision record that authorized the action, as :meth:`record`
        returned it, and it is the only source, so a copy cannot disagree with it.

        **One rule decides which fields travel: a fact about the action is copied, and a step of
        the authorization is not** (#83). So the session, the class, the context, the scope, the
        hosts, the command digest, the tool, the origin path and the origin identity all travel.
        ``actor``, ``approval_path``, ``grant_id``, ``approval_id`` and ``token_nonce`` stay on
        the decision.

        The rule exists for two reasons that point the same way. #46 keeps the authorization on
        one record, so one authorization cannot read two ways. And a reviewer filters the log on a
        fact — a person, a channel, a host, a session — and must see the outcome of every action
        that fact names. A field that stayed behind would make one filter answer with the decision
        and hide the row that says what happened, which is the gap #46 exists to close.

        ``same_path`` derives from the two paths rather than travelling, so it reports null here.
        A completion knows where the request came from and not who answered it, and null is the
        honest answer to a comparison this record cannot make.

        ``exit_code`` takes ``None`` when the action ended and the outcome is unknown. A
        timeout, a lost transport, and a killed executor all end that way. The record still
        exists, because unknown and never ran are opposite facts for a reviewer. A refused
        action gets no completion record at all.

        ``reason`` states how the action ended in words. It must hold no command output. This
        method accepts no output parameter, so no caller can put output in the log.

        Raises ``OSError`` when the write fails, for the same reason :meth:`record` does. The
        caller then knows the outcome reached no durable record. The decision record already
        landed before the action ran, so the failure costs the outcome and never the decision.
        """
        return self.record(
            decision=DECISION_COMPLETION,
            # A decision record that names neither field leaves both empty here. An empty class
            # is honest, and a raise would drop the outcome over the shape of another record.
            capability_class=_text(follows.get("capability_class")) or "",
            execution_context=_text(follows.get("execution_context")) or "",
            tool=_text(follows.get("tool")),
            session_id=_text(follows.get("session_id")),
            # A fact about the action travels, and a step of the authorization does not (#83).
            # The origin path names where the request came from, like the session and the hosts.
            # The answering path and `actor` name who authorized it, so they stay on the
            # decision, and `same_path` then derives to null here rather than claim a comparison
            # this record cannot make.
            origin_path=_text(follows.get("origin_path")),
            origin_actor=_text(follows.get("origin_actor")),
            scope=_text(follows.get("scope")),
            hosts=_host_list(follows.get("hosts")),
            # The digest, and never the text. The decision record holds the text under the
            # opt-in, so a second copy would double the exposure and add no fact.
            command_digest=_text(follows.get("command_digest")),
            reason=reason,
            exit_code=exit_code,
            duration_ms=duration_ms,
            follows=_text(follows.get("record_id")),
            ts=ts,
        )

    def prune(self, *, now: datetime | None = None) -> list[Path]:
        """Delete whole segments outside ``retention_days`` and return what went away.

        A retention of zero or less keeps every segment. An audit store must not empty
        itself because one config value arrived as a zero or as a placeholder.
        """
        retention = self.config.retention_days
        if retention <= 0:
            return []
        cutoff = (now or datetime.now(UTC)).astimezone(UTC).date() - timedelta(days=retention)
        removed: list[Path] = []
        for segment in self.segments():
            day = _segment_date(segment)
            if day is None:
                # The name carries no date this module wrote. Keep the file, because a
                # deletion here would destroy records that nothing can rebuild.
                logger.warning("Keeping audit file with an unparsable date: {}", segment)
                continue
            if day >= cutoff:
                continue
            try:
                segment.unlink()
            except OSError as exc:
                logger.warning("Could not delete expired audit segment {}: {}", segment, exc)
                continue
            removed.append(segment)
        if removed:
            logger.info("Pruned {} audit segment(s) older than {}", len(removed), cutoff)
        return removed

    def segments(self) -> list[Path]:
        """Return the segment files, oldest first. The name sorts by date already.

        The identity check comes before the ``is_dir`` test, because an absent root and a moved
        root both fail that test. Only one of the two is a bypass, and it must not answer with
        an empty list.
        """
        self.verify_root()
        if not self.root.is_dir():
            return []
        # os.scandir rather than Path.glob. glob swallows PermissionError and answers with an
        # empty list, and an unreadable root then reads exactly like a fresh install. #32
        # rebuilds latches from this log, so "empty" cleared every latch on every boot in the
        # split container, through permissions alone and with no rename needed. An unreadable
        # root must raise, so restore_latches degrades and every session stays latched.
        with os.scandir(self.root) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.name.startswith(_SEGMENT_PREFIX)
                and entry.name.endswith(_SEGMENT_SUFFIX)
            ]
        return [self.root / name for name in sorted(names)]

    def read_all(self) -> list[dict[str, Any]]:
        """Return every readable record, oldest segment first."""
        records: list[dict[str, Any]] = []
        for segment in self.segments():
            records.extend(self._read_segment(segment))
        return records

    def _read_segment(self, segment: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            text = segment.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Skipping unreadable audit segment {}: {}", segment, exc)
            return records
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # A torn tail from a crash, or an edited file. One damaged line must
                # never hide the intact records around it.
                logger.warning("Skipping malformed audit line {}:{}", segment, number)
                continue
            if isinstance(parsed, dict):
                records.append(dict(parsed))  # pyright: ignore[reportUnknownArgumentType]
            else:
                logger.warning("Skipping non-object audit line {}:{}", segment, number)
        return records

    def _append(self, segment: Path, payload: dict[str, Any], moment: datetime) -> None:
        # The check runs before the mkdir. A store that recreates a moved root starts a fresh
        # log, and a fresh log holds no latch. That is the #36 bypass.
        self.verify_root()
        if not self.root.is_dir():
            self.root.mkdir(parents=True, exist_ok=True)
            # Set the mode on creation only. An operator who opens the directory to an
            # audit group must keep that change.
            with suppress(OSError):
                os.chmod(self.root, _DIR_MODE)
            # Pin the directory this store just created. A pin that waits for the next record
            # leaves a window: another account could move this directory aside and put its own
            # at the same path, and the next record would then adopt the replacement.
            self.verify_root()
        # json.dumps escapes every control character, so a newline inside a field cannot
        # split one record into two lines.
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
        fresh = not segment.exists()
        fd = os.open(segment, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        try:
            if fresh:
                # os.open masks the mode with the umask, so set the mode exactly.
                with suppress(OSError):
                    os.fchmod(fd, _FILE_MODE)
                self._share_segment_with_the_directory_group(fd)
            self._write_all(fd, line, segment)
            # An audit record that a crash can drop is not evidence. Records arrive once
            # per gate decision, so one fsync per record costs little.
            os.fsync(fd)
        finally:
            os.close(fd)
        if fresh:
            # Rotation is the trim point. It arrives once a UTC day, so retention needs no
            # scheduler. The record already landed, so a failed trim must not raise here
            # and lose the decision the caller just made.
            try:
                self.prune(now=moment)
            except OSError:
                logger.exception("Audit retention pass failed in {}", self.root)

    def _share_segment_with_the_directory_group(self, fd: int) -> None:
        """Give a newly created segment the group its directory carries, so the reader can read it.

        This log is written by one account and read by another: the executor appends, and the
        agent process restores latches from it (#32) while the WebUI serves it (#29). A fresh file
        inherits the *creator's* primary group, `nanoinfra-exec`, and the mode is 640, so the agent
        is refused -- and a refused read fails closed, which latches every session in the
        deployment. The rotation at midnight UTC is what creates that file, so a container that
        boots healthy breaks hours later with nothing having changed.

        `_DIR_MODE` asks for setgid, which would make the kernel do this. It is not enough, and the
        same reason the job store gives applies here: a directory that arrives on a container volume
        or an image layer loses `S_ISGID` on a chmod copy-up. Measured on the demo host, `chmod 2750`
        reads back as 750 even as root, so nothing in the running deployment carries that bit.

        So the group is set on the file, which the owner may do for any group it belongs to, and the
        executor belongs to this directory's group. The directory's own group is the source of truth
        rather than a name from the environment: an operator who opened this log to an audit group
        gets that group, and a single-uid host has its own primary group there and changes nothing.
        Failures are ignored at debug level -- a group this process cannot set is not a reason to
        lose a gate decision, and the record has already been written.
        """
        try:
            directory = os.stat(self.root)
            current = os.fstat(fd)
            if current.st_gid != directory.st_gid:
                os.fchown(fd, -1, directory.st_gid)
        except OSError as exc:
            logger.debug("Could not share audit segment in {} with its directory group: {}", self.root, exc)

    @staticmethod
    def _write_all(fd: int, line: bytes, segment: Path) -> None:
        """Write the whole record. One call keeps two concurrent writers apart."""
        written = os.write(fd, line)
        while written < len(line):
            # A short write on a regular file is rare. The rest may now land after another
            # writer's record. Say so, because the line then reads as malformed.
            logger.warning(
                "Short audit write to {} ({} of {} bytes)", segment, written, len(line)
            )
            chunk = os.write(fd, line[written:])
            if chunk == 0:
                # Zero progress means an endless loop. A hang in the gate path stops every
                # turn, which is worse than one damaged line.
                raise OSError(f"Audit write to {segment} made no progress")
            written += chunk


def _segment_date(segment: Path) -> date | None:
    """Return the UTC day a segment holds, or ``None`` when the name does not say."""
    stem = segment.name[len(_SEGMENT_PREFIX) : -len(_SEGMENT_SUFFIX)]
    try:
        return datetime.strptime(stem, _SEGMENT_DATE_FORMAT).date()
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    """Return *value* when a record holds it as a string, and ``None`` otherwise.

    A record read back from disk is an untrusted dynamic boundary. A completion record copies
    fields from another record, so it normalizes each one here rather than trust the shape.
    """
    return value if isinstance(value, str) else None


def _host_list(value: Any) -> list[str]:
    """Return the resolved hosts a record holds, and an empty list when it holds none.

    The ``isinstance`` check on the same path supports the cast. A record from disk can hold
    any JSON value under this key, and a host list is the only shape this store writes.
    """
    if isinstance(value, list):
        return [str(host) for host in cast("list[Any]", value)]
    return []


def _named_or_none(value: str | None) -> str | None:
    """Return the name a caller passed, or ``None`` when it named nobody -- #79.

    Blank text is never a name. A record that held ``""`` would read as a person whose name is
    empty, and a reviewer cannot tell that apart from a person the log failed to write. #67 keeps
    ``None`` and ``""`` apart on the wire for the same reason. The rule sits here rather than at
    each call site, so one writer cannot break it for the whole log.
    """
    if value is None:
        return None
    named = value.strip()
    return named or None


def _same_path(origin_path: str | None, approval_path: str | None) -> bool | None:
    """Compare the request path with the approval path.

    The answer is ``None`` when no approval arrived, because ``False`` would read as a
    failed out-of-band check on a call that nobody ever approved.
    """
    if origin_path is None or approval_path is None:
        return None
    return origin_path == approval_path


__all__ = [
    "DECISION_COMPLETION",
    "AuditRootChangedError",
    "AuditStore",
    "root_identity",
    "segment_name",
]
