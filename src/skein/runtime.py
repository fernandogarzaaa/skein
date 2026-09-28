"""Bounded, killable subprocess execution for the control plane.

Three hazards this module fixes wherever agents or completion commands run:

1. Pipe deadlock: attaching stdout=PIPE and reading only after the child
   exits deadlocks once the pipe buffer (~64KB) fills while the parent is
   busy doing something else (polling for interrupts, waiting on a
   timeout). Every spawn here drains the pipe continuously.

2. Unbounded memory: a chatty or compromised backend could fill RAM with
   captured output. Output is capped; the tail is kept and the evidence
   notes the truncation.

3. Orphaned grandchildren: terminate() kills only the direct child; tools
   the agent spawned keep running. On POSIX the child gets its own
   process group so killpg() takes the whole tree.

This is the execution primitive the supervisor and the verification gate
share. A full canonical runtime (unifying ProfileAdapter.run's stdin
modes and output parsers with the supervisor's interruptible loop) is
still Phase 2 roadmap work; this module is its foundation.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Tuple

OUTPUT_CAP_BYTES = 1_000_000  # per-command output cap; the tail is kept
_DRAIN_CHUNK = 65536


class BoundedBuffer:
    """Byte buffer that keeps only the last OUTPUT_CAP_BYTES."""

    def __init__(self, cap: int = OUTPUT_CAP_BYTES):
        self.cap = cap
        self.chunks: List[bytes] = []
        self.total = 0
        self.dropped = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.chunks.append(data)
        self.total += len(data)
        while self.total > self.cap and len(self.chunks) > 1:
            self.total -= len(self.chunks.pop(0))
            self.dropped += 1

    def text(self) -> str:
        data = b"".join(self.chunks)
        # errors="replace": a multibyte char split across the drain
        # boundary degrades to U+FFFD instead of raising.
        out = data.decode("utf-8", errors="replace")
        if self.dropped:
            out = (f"[skein] output truncated: kept last {self.total} bytes "
                   f"of a longer stream\n") + out
        return out


def _windows_pipe_has_data(pipe) -> bool:
    """True when a Windows pipe has unread bytes waiting.

    os.set_blocking(fd, False) does not reliably make anonymous pipes
    non-blocking on Windows, and the failure is silent: os.read() on an
    empty pipe then blocks until the child writes or exits, which wedged
    the timeout loop for silent children (the 120s sleeps ran to
    completion instead of being killed at 2s). PeekNamedPipe lets us ask
    first and only read when data is actually waiting.
    """
    import msvcrt
    from ctypes import byref, windll, wintypes

    try:
        handle = msvcrt.get_osfhandle(pipe.fileno())
    except (OSError, ValueError):
        return False
    kernel32 = windll.kernel32
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    avail = wintypes.DWORD(0)
    if not kernel32.PeekNamedPipe(handle, None, 0, None,
                                  byref(avail), None):
        return False  # broken/closed pipe: nothing more to drain
    return avail.value > 0


def drain_available(proc: "subprocess.Popen", buf: BoundedBuffer) -> None:
    """Read whatever the child's stdout currently holds, without blocking.

    Must be called in a loop alongside poll(): never let the pipe sit
    undrained while the parent waits on something else.

    POSIX: the fd is put in non-blocking mode and os.read raises
    BlockingIOError when the pipe is empty. Windows: non-blocking mode
    is unreliable for pipes, so each read is gated on PeekNamedPipe.
    """
    try:
        pipe = proc.stdout
        fd = pipe.fileno()
    except (AttributeError, ValueError):
        return
    if os.name == "nt":
        while _windows_pipe_has_data(pipe):
            try:
                chunk = os.read(fd, _DRAIN_CHUNK)
            except OSError:
                break
            if not chunk:
                break  # EOF
            buf.append(chunk)
        return
    while True:
        try:
            chunk = os.read(fd, _DRAIN_CHUNK)
        except BlockingIOError:
            break
        except OSError:
            break
        if not chunk:
            break  # EOF
        buf.append(chunk)


def _make_nonblocking(proc: "subprocess.Popen") -> None:
    # POSIX only. On Windows the pipe is left in blocking mode and
    # drain_available gates each read on PeekNamedPipe instead, because
    # os.set_blocking() is unreliable for Windows pipes.
    if os.name != "posix":
        return
    try:
        os.set_blocking(proc.stdout.fileno(), False)
    except (OSError, ValueError, AttributeError):
        pass


def kill_tree(proc: "subprocess.Popen") -> None:
    """Terminate the child and any grandchildren it spawned."""
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    else:
        # Windows: no process groups. taskkill /T /F takes the whole
        # tree rooted at the child; fall back to direct terminate/kill
        # if taskkill is unavailable.
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass


def spawn_monitored(cmd, cwd: str | Path,
                    stdin_text: Optional[str] = None,
                    shell: bool = False) -> "subprocess.Popen":
    """Spawn with merged stdout/stderr on a non-blocking pipe, stdin from
    DEVNULL (or fed once from stdin_text), and its own process group on
    POSIX so kill_tree() reaches grandchildren.

    The caller owns the poll/drain loop: call drain_available(proc, buf)
    on every iteration and kill_tree(proc) to stop.
    """
    stdin_cfg = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=stdin_cfg,
        shell=shell,
        bufsize=0,
        # Ignored on Windows; on POSIX the child becomes a process-group
        # leader so killpg() can take the whole tree it spawns.
        start_new_session=(os.name == "posix"),
    )
    _make_nonblocking(proc)
    if stdin_text is not None and proc.stdin is not None:
        # Feed the prompt from a thread: a large prompt could block a
        # synchronous write while the child fills stdout.
        def _feed() -> None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=_feed, daemon=True).start()
    return proc


def run_bounded(cmd, cwd: str | Path, timeout: float,
                stdin_text: Optional[str] = None,
                shell: bool = False) -> Tuple[int, str]:
    """Run to completion with continuous draining, bounded output, and
    whole-tree kill on timeout. Returns (exit_code, output_text)."""
    import time
    proc = spawn_monitored(cmd, cwd, stdin_text=stdin_text, shell=shell)
    buf = BoundedBuffer()
    deadline = time.monotonic() + timeout
    while True:
        drain_available(proc, buf)
        rc = proc.poll()
        if rc is not None:
            drain_available(proc, buf)
            return rc, buf.text()
        if time.monotonic() >= deadline:
            drain_available(proc, buf)
            kill_tree(proc)
            drain_available(proc, buf)
            buf.append(f"\n[skein] timed out after {timeout}s; process tree killed\n"
                       .encode("utf-8"))
            return 124, buf.text()
        time.sleep(0.05)
