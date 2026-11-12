"""Doorbells for the Portal's planners over the webos node's in-memory queue (Portal PTL001-310, PTL001-311).

The Portal's "Plan your day" canvas listens to one inbox per user, ``/memq/<domain GUID>/user/<user id>``, on
the webos node's NodeMQ service (``/.Cluster/MessageQueue``). A write that changes a user's plate rings that
user's bell; the open planner then reloads the plate through its own authenticated endpoint.

* **A bell is a code, not a message:** ``m=WORK``, at most 16 ASCII bytes. No ids, titles or senders ever go
  into a queue: inbox paths are guessable and NodeMQ has no authentication.
* **One bell is enough:** ``GetMessageCount`` first, and ``PushI`` only when the inbox is empty, so an offline
  user's inbox holds one bell however many writes there are.
* **Never in the way of a write:** bells are rung on one background thread after the write has committed;
  every failure (node down, a ``throw`` reply, a timeout) is logged and dropped. The database stays the truth:
  a lost bell only delays a planner until its next load (PTL001-310 item 7).
* The calls are raw DWR POSTs (spec ``portal/bug/SPEC-inter-session-messaging.md`` §5); the reply is
  JavaScript, and errors come back as HTTP 200 with a ``throw`` line.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from urllib.parse import urlsplit

log = logging.getLogger("cerp.data")

MAX_BYTES: Final = 16
MAX_PENDING: Final = 200
_CODE = re.compile(r"^[A-Z][A-Z0-9.]{0,3}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,62}[a-z0-9])?$")
_VALUE = re.compile(r"var s0 = '((?:[^'\\]|\\.)*)'")
_THROW = re.compile(r"(^|\n)\s*throw ")


class DoorbellError(Exception):
    """A NodeMQ call failed (HTTP error, timeout, or a ``throw`` reply)."""


def inbox(guid: str, user_id: int) -> str:
    """The user's inbox: ``/memq/<lower-case GUID>/user/<id>`` (PTL001-310 item 2)."""
    if not _GUID.match(guid):
        raise ValueError("malformed domain GUID")
    return f"/memq/{guid.lower()}/user/{int(user_id)}"


def bell(code: str) -> str:
    """``m=<code>``: refused (never cut short) when it is not a short code or exceeds 16 bytes."""
    if not _CODE.match(code):
        raise ValueError("a bell code is 1-4 upper-case letters or digits")
    text = f"m={code}"
    if len(text.encode("ascii")) > MAX_BYTES:
        raise ValueError("a bell is at most 16 bytes")
    return text


class Doorbell:
    """Rings users' inboxes on the webos node at ``url`` (for example ``http://127.0.0.1:4080``)."""

    def __init__(self, url: str, *, timeout_s: float = 2.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("the webos node URL must be http(s)://host[:port]")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._timeout = timeout_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cerp-doorbell")
        self._pending = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: Iterable[str]) -> str:
        """One NodeMQ call; returns its string result ('' for an empty queue)."""
        lines = ["callCount=1", "c0-scriptName=", f"c0-methodName={method}", "c0-id=0"]
        lines += [f"c0-param{i}=string:{p}" for i, p in enumerate(params)]
        body = ("\n".join(lines) + "\n").encode("ascii")
        req = urllib.request.Request(  # noqa: S310 - scheme checked in __init__ (http/https only)
            f"{self._base}/.Cluster/MessageQueue?o=AJAX_DWR&m=/exec/.{method}.dwr",
            data=body,
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:  # noqa: S310 - see above
                text = res.read(4096).decode("ascii", "replace")
        except OSError as exc:
            raise DoorbellError(f"{method}: {type(exc).__name__}") from None
        if _THROW.search(text):
            raise DoorbellError(f"{method}: the node refused the call")
        found = _VALUE.search(text)
        return found.group(1) if found else ""

    def ring_now(self, domain: str, guid: str, users: Iterable[int], code: str = "WORK") -> int:
        """Ring each user once, on this thread; returns the number of bells pushed."""
        msg = bell(code)
        pushed = 0
        for uid in sorted({int(u) for u in users if int(u) > 0}):
            path = inbox(guid, uid)
            if self.call("GetMessageCount", (domain, path, "")).strip() not in ("", "0"):
                continue  # one waiting bell is enough
            self.call("PushI", (domain, path, msg, ""))
            pushed += 1
        return pushed

    def ring(self, domain: str, guid: str, users: Iterable[int], code: str = "WORK") -> bool:
        """Ring in the background; never raises. False when nothing was queued."""
        try:
            ids = sorted({int(u) for u in users if int(u) > 0})
            bell(code)
            inbox(guid, 1)
        except (TypeError, ValueError) as exc:
            log.warning("doorbell not rung: %s", exc)
            return False
        low = domain.strip().lower()
        if not ids or not _DOMAIN.match(low):
            return False
        with self._lock:
            if self._pending >= MAX_PENDING:
                log.warning("doorbell queue full; %d bell(s) dropped", len(ids))
                return False
            self._pending += 1
        self._pool.submit(self._run, low, guid, ids, code)
        return True

    def _run(self, domain: str, guid: str, ids: list[int], code: str) -> None:
        try:
            self.ring_now(domain, guid, ids, code)
        except DoorbellError as exc:
            log.warning("doorbell failed: %s", exc)
        except Exception:  # never let a bell take the worker down
            log.exception("doorbell failed")
        finally:
            with self._lock:
                self._pending -= 1

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
