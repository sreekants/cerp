"""Reload on file change, for platforms without SIGHUP (MPC001-115).

:class:`FileWatcher` polls the modification time and size of the configuration and exposure files and calls
``on_change`` when either changes. On Windows this is how an operator reloads: save the file. The poll
interval is ``server.reload_poll_seconds`` (0 turns it off; SIGHUP still works where it exists).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger("cerp.app")


def _stamp(paths: Sequence[Path]) -> tuple[tuple[int, int] | None, ...]:
    out: list[tuple[int, int] | None] = []
    for p in paths:
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class FileWatcher:
    def __init__(
        self, paths: Callable[[], Sequence[Path]], interval_s: float, on_change: Callable[[], None]
    ) -> None:
        self._paths = paths
        self._interval = interval_s
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cerp-config-watch", daemon=True)
        self._last = _stamp(self._paths())

    def start(self) -> FileWatcher:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def poll(self) -> bool:
        """Check once; True if a change was seen and handled."""
        now = _stamp(self._paths())
        if now == self._last:
            return False
        self._last = now
        try:
            self._on_change()
        except Exception:
            log.exception("reload after file change failed")
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.poll()
