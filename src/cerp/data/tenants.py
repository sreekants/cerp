"""One set of databases per webos domain (MPC001-286, decision 0005).

A caller authenticated by an OSESSION works in its own domain's databases, which override the configured
default for that request. The set for a domain has the same datasources and file names as the default set,
under the domain's ``db`` folder (:mod:`cerp.data.domains`). It is built on first use, checked once by
``validate`` (the tool packs' declared tables and columns), and then cached. A domain whose folder or
``security.s3db`` is missing, or that fails validation, raises :class:`DomainUnavailable` (fail closed).

This module imports nothing else from cerp except the data layer (MPC001-005).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from cerp.data.domains import DomainDirectory, DomainUnavailable
from cerp.data.security import SECURITY
from cerp.data.sqlite import Databases, DbOptions

log = logging.getLogger("cerp.data")

Validator = Callable[[Databases], list[str]]


class TenantDatabases:
    def __init__(
        self,
        domains: DomainDirectory,
        default: Databases,
        options: DbOptions | None = None,
        *,
        validate: Validator | None = None,
    ) -> None:
        self.domains = domains
        self._files = {name: default.path(name).name for name in default.names}
        self._options = options
        self._validate = validate
        self._sets: dict[str, Databases] = {}
        self._lock = threading.Lock()

    def for_domain(self, domain: str) -> Databases:
        guid = self.domains.guid(domain)
        if guid is None:
            raise DomainUnavailable("unknown or disabled domain")
        with self._lock:
            found = self._sets.get(guid)
        if found is not None:
            return found
        folder = self.domains.database_dir(guid)
        if not (folder / self._files.get(SECURITY, "security.s3db")).is_file():
            raise DomainUnavailable("the domain's databases were not found")
        dbs = Databases({name: folder / file for name, file in self._files.items()}, self._options)
        problems = self._validate(dbs) if self._validate else []
        if problems:
            log.error("domain %s databases do not match the tool packs: %s", domain, "; ".join(problems))
            raise DomainUnavailable("the domain's databases do not match this server")
        with self._lock:
            return self._sets.setdefault(guid, dbs)
