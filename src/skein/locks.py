"""Cross-process file locking for Skein's control plane.

All state transitions (claim, heartbeat, release, complete, fail,
human interrupt, node mutation, snapshot rebuild) must be serialized
under this lock on a single machine. The lock is re-entrant within a
thread so helpers can safely nest (e.g. claim_node -> append_event).

This lock is advisory and local-only: it serializes processes on this
machine. It does NOT provide distributed mutual exclusion across
machines; multi-machine coordination via git sync is eventual, not
strongly consistent (see README "Consistency model").
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - windows
    fcntl = None

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - posix
    msvcrt = None


LOCK_NAME = "skein.lock"

_proc_locks: Dict[str, threading.RLock] = {}
_proc_locks_guard = threading.Lock()
_tls = threading.local()


def lock_path(repo_root) -> Path:
    """The lock lives under .git/, never under .skein/: git_commit_log
    stages the whole .skein directory, so a lock file there would be
    committed by accident. Falls back to .skein/ only when the repo has
    no .git directory yet (e.g. pre-init)."""
    root = Path(repo_root)
    if (root / ".git").is_dir():
        return root / ".git" / LOCK_NAME
    return root / ".skein" / LOCK_NAME


def _acquire_file(fh, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out acquiring skein repo lock")
                time.sleep(0.01)
    elif msvcrt is not None:  # pragma: no cover - windows only
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out acquiring skein repo lock")
                time.sleep(0.01)
    else:  # pragma: no cover - no locking primitive available
        raise RuntimeError("no file-locking primitive available on this platform")


def _release_file(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - windows only
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def repo_lock(repo_root, timeout: float = 30.0):
    """Hold the repo-wide control-plane lock (re-entrant per thread)."""
    key = str(lock_path(repo_root).absolute())
    with _proc_locks_guard:
        rlock = _proc_locks.get(key)
        if rlock is None:
            rlock = _proc_locks[key] = threading.RLock()
    rlock.acquire()
    try:
        depths = _tls.__dict__.setdefault("lock_depths", {})
        depth = depths.get(key, 0)
        fh = None
        if depth == 0:
            p = lock_path(repo_root)
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = open(p, "a+b")
            try:
                _acquire_file(fh, timeout)
            except Exception:
                fh.close()
                raise
        depths[key] = depth + 1
        try:
            yield
        finally:
            depths[key] = depth
            if depth == 0 and fh is not None:
                _release_file(fh)
                fh.close()
    finally:
        rlock.release()
