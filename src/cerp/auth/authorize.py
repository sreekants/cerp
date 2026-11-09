"""The one authorization function (MPC001-061), ported from webos ``AuthenticationAspect``.

webos (``SecurityAspect.cpp``, ``CheckPrivilege``) checks, for a model's privilege name ``P``, the caller's
rights in this order: ``P.DenyAll`` denies, ``P.AllowAll`` allows, the method's right (``P.View`` for reads)
allows; anything else is denied unless the caller is an administrator (``OnNoAccess``). cerp keeps that
order and is **stricter** where webos has defects:

* **No ``AccessPrivilege`` (WOS-BUG-033).** webos skips the check, so any logged-in user may read. cerp
  requires the model's own rights instead (``<Model>.View``, from the manifest's ``<Model>``); the webos
  rights catalogue already defines them for every model. A model with neither is denied.
* **Unclassified methods (WOS-BUG-034).** Only the four verbs View, Add, Edit and Delete exist; any other
  verb is denied.
* **Substring matches (WOS-BUG-035).** webos looks the right up with ``FindPtr`` in a concatenated list,
  so ``Account.View`` matches ``Account.ViewAll``. cerp compares exact names.
* **Security model "none".** webos skips every check when ``security.xml`` is missing; cerp never does.
* **Disabled users** are denied here as well as at principal mapping (MPC001-060).

Administrators (members of group 1) pass where webos lets them pass, and every such bypass is written to
the ``cerp.audit`` log (requirement 3). Any failure to read the security data denies (requirement 4).
The parity test against webos (MPC002-007) is still to be written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final, Literal

from cerp.data.registry import Registry

audit = logging.getLogger("cerp.audit")

Verb = Literal["View", "Add", "Edit", "Delete"]
VERBS: Final[frozenset[str]] = frozenset({"View", "Add", "Edit", "Delete"})
_PRIVILEGE_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class AccessDenied(Exception):
    """The caller may not perform the operation. Tools report it as ``not_permitted`` (or ``not_found``
    for a single record, MPC001-087)."""


@dataclass(frozen=True)
class Caller:
    """An authenticated, mapped and enabled webos user (MPC001-060)."""

    user_id: int
    user_name: str
    display_name: str
    principal: str  # e.g. "static:alice", "oauth:<iss>|<sub>", "local:alice"
    rights: frozenset[str]
    administrator: bool
    enabled: bool = True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    admin_bypass: bool = False


def privilege_model(registry: Registry, model_key: str) -> str:
    """The privilege name guarding a registry model: its ``AccessPrivilege`` model when enforced, otherwise
    the model's own class name (stricter than webos, WOS-BUG-033). Raises :class:`AccessDenied` if the model
    is unknown or has neither."""
    model = registry.models.get(model_key)
    if model is None:
        raise AccessDenied(f"model {model_key} is not in the registry")
    name = model.privilege_model if model.access_enforced and model.privilege_model else model.model
    if not name or not _PRIVILEGE_NAME.match(name):
        raise AccessDenied(f"model {model_key} has no privilege name")
    return name


def authorize(caller: Caller, privilege: str, verb: str) -> Decision:
    """Decide whether ``caller`` may perform ``verb`` on records guarded by ``privilege``."""
    if not caller.enabled:
        return Decision(False, "user disabled")
    if not _PRIVILEGE_NAME.match(privilege):
        return Decision(False, "invalid privilege name")
    if verb not in VERBS:
        return Decision(False, f"unclassified operation {verb!r}")
    rights = caller.rights
    if f"{privilege}.DenyAll" in rights:
        return _no_access(caller, privilege, verb, "DenyAll")
    if f"{privilege}.AllowAll" in rights or f"{privilege}.{verb}" in rights:
        return Decision(True, "right held")
    return _no_access(caller, privilege, verb, "no right")


def _no_access(caller: Caller, privilege: str, verb: str, why: str) -> Decision:
    if caller.administrator:
        audit.warning(
            "administrator bypass: %s %s.%s (%s)",
            caller.principal,
            privilege,
            verb,
            why,
            extra={"event": "admin_bypass", "user": caller.user_name, "privilege": f"{privilege}.{verb}"},
        )
        return Decision(True, f"administrator ({why})", admin_bypass=True)
    return Decision(False, why)


def require(caller: Caller, privilege: str, verb: str) -> None:
    """Raise :class:`AccessDenied` unless :func:`authorize` allows the operation."""
    decision = authorize(caller, privilege, verb)
    if not decision.allowed:
        raise AccessDenied(f"{privilege}.{verb}: {decision.reason}")
