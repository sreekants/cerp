"""webos domains: name -> GUID -> database folder (MPC001-286, decision 0005).

Ports the Cortex lookups an OSESSION depends on:

* ``CDeployment::CreateRecord`` / ``CDomain::Load``: a domain is a row of ``<domain_root>/nodedb.s3db`` table
  ``domains`` (``name``, ``guid``, ``enabled``), found by its lower-case name;
* ``CDeployment::ResolveDomainDirectory`` / ``CDomainManager::GetDirectoryForDomain``: its databases are in
  ``<domain_root>/<G[0]>/<G[1]>/<GUID upper-case>/db``, one ``<datasource>.s3db`` per module.

Only enabled domains are returned (``enabled > 0``); a disabled or unknown domain is ``None``. The whole
``domains`` table is loaded into one name -> GUID map (as ``CDomainCache`` keeps domains in memory), so a
lookup is a dictionary access, unknown names never reach the database and the cache cannot grow with them.
The map is reloaded after ``ttl`` seconds; a miss also reloads it early when ``nodedb.s3db`` has changed on
disk (a new domain is found at once; a disabled one stops working within ``ttl``).
``nodedb.s3db`` is opened read-only (MPC001-162). Any failure to read it raises :class:`DomainUnavailable`,
so callers fail closed.

This module imports nothing else from cerp (MPC001-005).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

NODEDB: Final = "nodedb.s3db"
CACHE_SECONDS: Final = 300.0
DOMAIN_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_GUID: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOMAINS: Final = "SELECT name, guid, enabled FROM domains"


class DomainUnavailable(Exception):
    """The domain list could not be read, or a domain's databases are not usable. Callers must deny."""


def normalise(name: str) -> str | None:
    """The lower-case domain name if it is well formed (webos lower-cases names, ``CDeployment::Load``)."""
    low = name.strip().lower()
    return low if DOMAIN_NAME.match(low) else None


class DomainDirectory:
    def __init__(
        self, domain_root: Path, *, ttl: float = CACHE_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.root = Path(domain_root)
        self._ttl = ttl
        self._clock = clock
        self._map: dict[str, str] = {}
        self._expires = float("-inf")
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _file(self) -> tuple[Path, tuple[int, int]]:
        path = self.root / NODEDB
        try:
            st = path.stat()
        except OSError:
            raise DomainUnavailable("the domain list (nodedb.s3db) was not found") from None
        return path, (st.st_mtime_ns, st.st_size)

    def _load(self, path: Path) -> dict[str, str]:
        try:
            con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(_DOMAINS).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise DomainUnavailable(f"the domain list cannot be read ({type(exc).__name__})") from None
        found: dict[str, str] = {}
        for name, guid, enabled in rows:
            try:
                active = int(enabled or 0) > 0
            except (TypeError, ValueError):
                active = False
            low = normalise(name) if isinstance(name, str) else None
            if active and low is not None and isinstance(guid, str) and _GUID.match(guid.strip()):
                found[low] = guid.strip()
        return found

    def _refresh(self, now: float, *, if_changed: bool) -> None:
        """Reload the map (lock held); with ``if_changed``, only if the file changed since the last load."""
        path, stamp = self._file()
        if if_changed and stamp == self._stamp:
            return
        self._map = self._load(path)
        self._stamp = stamp
        self._expires = now + self._ttl

    def guid(self, name: str) -> str | None:
        """The GUID of an enabled domain, as stored (webos hashes it as stored), or None."""
        low = normalise(name)
        if low is None:
            return None
        now = self._clock()
        with self._lock:
            if now >= self._expires:
                self._refresh(now, if_changed=False)
            found = self._map.get(low)
            if found is None:
                self._refresh(now, if_changed=True)
                found = self._map.get(low)
            return found

    def database_dir(self, guid: str) -> Path:
        """``<domain_root>/<G[0]>/<G[1]>/<GUID>/db``, the GUID upper-case (``ResolveDomainDirectory``)."""
        g = guid.strip().upper()
        if not _GUID.match(g):
            raise DomainUnavailable("malformed domain GUID")
        return self.root / g[0] / g[1] / g / "db"
