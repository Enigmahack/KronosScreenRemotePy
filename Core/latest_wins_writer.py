"""Process-wide ordered background file writer ("newest write wins").

Lives in its own module (no Qt, no app imports) so any layer - Models.storage, the SysEx caches, the settings
writer - can share it without import cycles."""
from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

_version_lock = threading.Lock()
_version_counter = itertools.count(1)


def next_version() -> int:
    """Process-wide, strictly increasing write version (take it together with the snapshot)."""
    with _version_lock:
        return next(_version_counter)


class LatestWinsWriter:
    """One background thread that runs file jobs in submission order.

    `submit(key, version, fn)` is a keyed write: per key only the newest version is ever
    written, and a version at or below the last one written for that key is dropped, so an
    older snapshot can never land on top of a newer one. `call(fn)` queues an ordinary job
    (a cache load) in the same FIFO, so a load queued after a write to the same file reads
    what that write put there. `flush()` waits for the queue to drain (exit, tests)."""

    def __init__(self, name: str):
        self._name = name
        self._cv = threading.Condition()
        self._queue: collections.deque = collections.deque()
        self._pending: Dict[object, Tuple[int, Callable[[], None]]] = {}
        self._written: Dict[object, int] = {}
        self._busy = 0
        self._thread: Optional[threading.Thread] = None

    def _kick(self) -> None:                       # under self._cv
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
            self._thread.start()
        self._cv.notify_all()

    def submit(self, key, version: int, fn: Callable[[], None]) -> None:
        with self._cv:
            if version <= self._written.get(key, 0):
                return
            cur = self._pending.get(key)
            if cur is None:
                self._pending[key] = (version, fn)
                self._queue.append((key, None))
            elif version > cur[0]:
                self._pending[key] = (version, fn)   # keeps its earlier queue position
            self._kick()

    def call(self, fn: Callable[[], None]) -> None:
        with self._cv:
            self._queue.append((None, fn))
            self._kick()

    def run_inline(self, key, version: int, fn: Callable[[], None]) -> None:
        """A keyed write performed by a job already running on this writer's thread."""
        with self._cv:
            if version <= self._written.get(key, 0):
                return
        fn()
        with self._cv:
            if version > self._written.get(key, 0):
                self._written[key] = version

    def flush(self, timeout_s: Optional[float] = None) -> bool:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cv:
            while self._queue or self._busy:
                if deadline is None:
                    self._cv.wait()
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cv.wait(left)
        return True

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                key, fn = self._queue.popleft()
                version = 0
                if key is not None:
                    version, fn = self._pending.pop(key)
                    if version <= self._written.get(key, 0):
                        self._cv.notify_all()
                        continue
                self._busy += 1
            try:
                fn()
            except Exception:                      # noqa: BLE001 - a failed write must not kill the writer
                log.exception("%s job failed", self._name)
            finally:
                with self._cv:
                    if key is not None and version > self._written.get(key, 0):
                        self._written[key] = version
                    self._busy -= 1
                    self._cv.notify_all()
