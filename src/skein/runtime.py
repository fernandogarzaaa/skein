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
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

OUTPUT_CAP_BYTES = 1_000_000  # per-command output cap; the tail is kept
_DRAIN_CHUNK = 65536

# Sandbox (Phase 6) resource caps applied via prlimit(1) on Linux.
# Best-effort defense in depth: when prlimit is missing or the platform
# is not Linux, the run continues unsandboxed with a note, never fails.
SANDBOX_CPU_SECONDS = 600
SANDBOX_MAX_AS_BYTES = 8 * 1024 ** 3  # 8 GiB address space


def minimal_environ() -> Dict[str, str]:
    """Allowlist environment for backend processes.

    PATH, HOME, LANG, every SKEIN_* variable, plus SystemRoot on Windows
    (required for process startup there). Everything else - ambient API
    keys, session tokens, agent credentials - is deliberately not
    inherited. Backends that need credentials should receive them via
    SKEIN_-prefixed variables mapped by their own wrapper.
    """
    keep = ("PATH", "HOME", "LANG")
    env = {k: v for k, v in os.environ.items()
           if k in keep or k.startswith("SKEIN_")}
    if os.name == "nt" and "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


def _sandbox_wrap(cmd, shell: bool) -> Tuple[object, bool, str]:
    """Wrap cmd in prlimit(1) resource caps on Linux.

    Returns (cmd, shell, note): note is "" when the sandbox applied,
    otherwise the human-readable reason the run continues unsandboxed.
    Never raises: the sandbox is defense in depth, the run is the product.
    """
    if sys.platform != "linux":
        return cmd, shell, (
            "sandbox rlimits need Linux (prlimit); continuing unsandboxed")
    prlimit = shutil.which("prlimit")
    if prlimit is None:
        return cmd, shell, "prlimit not found; continuing unsandboxed"
    if isinstance(cmd, (list, tuple)):
        if not cmd:
            return cmd, shell, ""
        if shutil.which(cmd[0]) is None:
            # Missing binary: leave the command alone so execute()'s
            # normal FileNotFoundError path reports exit 127 with the
            # real binary name instead of prlimit's error text.
            return cmd, shell, ""
        wrapped: object = [prlimit, f"--cpu={SANDBOX_CPU_SECONDS}",
                           f"--as={SANDBOX_MAX_AS_BYTES}", *list(cmd)]
    else:
        wrapped = [prlimit, f"--cpu={SANDBOX_CPU_SECONDS}",
                   f"--as={SANDBOX_MAX_AS_BYTES}", "sh", "-c", cmd]
    return wrapped, False, ""


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


def _drain_stream(stream, buf: BoundedBuffer) -> None:
    """Read whatever a pipe currently holds, without blocking.

    Must be called in a loop alongside poll(): never let the pipe sit
    undrained while the parent waits on something else.

    POSIX: the fd is put in non-blocking mode and os.read raises
    BlockingIOError when the pipe is empty. Windows: non-blocking mode
    is unreliable for pipes, so each read is gated on PeekNamedPipe.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError):
        return
    if os.name == "nt":
        while _windows_pipe_has_data(stream):
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
    # POSIX only. On Windows the pipes are left in blocking mode and
    # _drain_stream gates each read on PeekNamedPipe instead, because
    # os.set_blocking() is unreliable for Windows pipes.
    if os.name != "posix":
        return
    for stream in (proc.stdout, proc.stderr):
        try:
            os.set_blocking(stream.fileno(), False)
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
    # Reap the zombie after SIGKILL so poll()/returncode settle.
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def spawn_monitored(cmd, cwd: str | Path,
                    stdin_text: Optional[str] = None,
                    shell: bool = False,
                    merge_streams: bool = False,
                    env: Optional[Dict[str, str]] = None) -> "subprocess.Popen":
    """Spawn with stdout/stderr on non-blocking pipes, stdin from DEVNULL
    (or fed once from stdin_text), and its own process group on POSIX so
    kill_tree() reaches grandchildren.

    With merge_streams=False (default) stderr gets its own pipe, so
    callers can hand the backend's exact (stdout, stderr, exit_code)
    triple to its output parser; with True, stderr merges into stdout.

    env=None inherits the ambient environment; pass minimal_environ()
    (or sandbox=True on execute()) to scrub it for backend processes.

    The caller owns the poll/drain loop: drain every stream on every
    iteration (see execute()) and kill_tree(proc) to stop.
    """
    stdin_cfg = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_streams else subprocess.PIPE,
        stdin=stdin_cfg,
        shell=shell,
        bufsize=0,
        env=env,
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


@dataclass
class ExecutionResult:
    """Outcome of execute(): the backend's exact (stdout, stderr,
    exit_code) triple plus how the run ended."""
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False
    aborted: bool = False
    # Sandbox note: "" when the sandbox applied (or was not requested);
    # otherwise the human-readable reason the run continued unsandboxed.
    sandbox_note: str = ""


def execute(cmd, *, cwd: str | Path, timeout: float,
            stdin_text: Optional[str] = None,
            shell: bool = False,
            should_abort: Optional[Callable[[], bool]] = None,
            poll_interval: float = 0.2,
            output_cap: int = OUTPUT_CAP_BYTES,
            env: Optional[Dict[str, str]] = None,
            sandbox: bool = False) -> ExecutionResult:
    """The one canonical execution path for backends and shell commands.

    - Continuous draining of both pipes: no 64KB deadlock.
    - Bounded output: the tail of each stream is kept, truncation flagged.
    - Interruptible: should_abort() is polled every iteration; on truthy
      the whole process tree is killed and aborted=True is reported.
    - Timeout: the whole process tree is killed, timed_out=True.
    - Missing binary: FileNotFoundError becomes exit 127 with the
      message on stderr, never an exception.
    - env: explicit environment for the child (None inherits). Backend
      runs pass minimal_environ() so ambient secrets are not inherited.
    - sandbox: additionally wrap the command in prlimit(1) CPU/memory
      caps on Linux (cwd stays the worktree, env is scrubbed). When the
      sandbox cannot apply, sandbox_note explains why and the run
      continues unsandboxed; it never fails for lack of sandbox tooling.

    Callers apply their own output parsers and human-readable notes on
    top of the raw triple; execute() adds none.
    """
    import time
    sandbox_note = ""
    if sandbox:
        if env is None:
            env = minimal_environ()
        cmd, shell, sandbox_note = _sandbox_wrap(cmd, shell)
    try:
        proc = spawn_monitored(cmd, cwd, stdin_text=stdin_text, shell=shell,
                               env=env)
    except FileNotFoundError as e:
        binary = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        return ExecutionResult(
            exit_code=127, stdout="",
            stderr=f"{binary} binary not found: {e}",
            sandbox_note=sandbox_note)
    out_buf = BoundedBuffer(cap=output_cap)
    err_buf = BoundedBuffer(cap=output_cap)
    aborted = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        if proc.stdout is not None:
            _drain_stream(proc.stdout, out_buf)
        if proc.stderr is not None:
            _drain_stream(proc.stderr, err_buf)
        rc = proc.poll()
        if should_abort is not None:
            try:
                abort = bool(should_abort())
            except Exception:
                abort = False
            if abort:
                aborted = True
                kill_tree(proc)
                break
        if rc is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            kill_tree(proc)
            break
        time.sleep(poll_interval)
    if proc.stdout is not None:
        _drain_stream(proc.stdout, out_buf)
    if proc.stderr is not None:
        _drain_stream(proc.stderr, err_buf)
    rc = proc.poll()
    if timed_out:
        exit_code = 124
    elif aborted:
        exit_code = -1
    elif rc is not None:
        exit_code = rc
    else:
        exit_code = 124  # unreachable: kill_tree reaps before returning
    return ExecutionResult(
        exit_code=exit_code,
        stdout=out_buf.text(),
        stderr=err_buf.text(),
        truncated=out_buf.dropped > 0 or err_buf.dropped > 0,
        timed_out=timed_out,
        aborted=aborted,
        sandbox_note=sandbox_note,
    )


def run_bounded(cmd, cwd: str | Path, timeout: float,
                stdin_text: Optional[str] = None,
                shell: bool = False) -> Tuple[int, str]:
    """Run to completion with continuous draining, bounded output, and
    whole-tree kill on timeout. Returns (exit_code, merged output text).

    Legacy merged shape (stdout plus a stderr trailer) for callers that
    predate the (stdout, stderr, exit_code) triple.
    """
    r = execute(cmd, cwd=cwd, timeout=timeout,
                stdin_text=stdin_text, shell=shell)
    out = r.stdout
    if r.stderr:
        out += "\n--- stderr ---\n" + r.stderr
    if r.timed_out:
        out += (f"\n[skein] timed out after {timeout}s; "
                f"process tree killed\n")
    return r.exit_code, out
