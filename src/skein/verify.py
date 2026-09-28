"""Verification gate: execute completion commands, capture evidence."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


from . import ids
from . import redact
from . import runtime as rt


def split_commands(completion: str) -> List[str]:
    cmds = []
    for line in (completion or "").splitlines():
        line = line.strip()
        if line:
            cmds.append(line)
    return cmds


def run_completion(worktree_path: str | Path, completion: str,
                   evidence_dir: str | Path,
                   node_id: str, timeout: int = 600,
                   should_abort: Optional[Callable[[], bool]] = None
                   ) -> Tuple[bool, List[Dict], int]:
    """Execute each completion command for real in the worktree.

    Returns (success, evidence, redacted_hits). Evidence entries:
    {command, exit_code, output_ref}. Full output is stored in a file under
    evidence_dir; output_ref points at it. Evidence file contents are
    redacted at write time so secrets never land in .skein/evidence/
    verbatim; redacted_hits counts the masked secrets for audit.

    should_abort, when given, is polled during each command: an abort
    kills the command's process tree and stops the remaining commands.
    """
    evidence: List[Dict] = []
    redacted = 0
    cmds = split_commands(completion)
    Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    if not cmds:
        return False, [{"command": "", "exit_code": 1,
                        "output_ref": None, "error": "empty completion command"}], 0
    success = True
    for i, cmd in enumerate(cmds):
        if should_abort is not None:
            try:
                if should_abort():
                    evidence.append({"command": cmd, "exit_code": -1,
                                     "duration_ms": 0, "output_ref": None,
                                     "aborted": True})
                    return False, evidence, redacted
            except Exception:
                pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        out_file = Path(evidence_dir) / ids.evidence_name(node_id, f"{ts}_{i}")
        t_start = time.monotonic()
        # Canonical bounded execution: the pipe is drained continuously
        # (no 64KB deadlock), output is capped, the whole process tree is
        # killed on timeout/abort. Completion lines are shell by design:
        # they are operator-authored node intent, run in the worktree.
        r = rt.execute(cmd, cwd=worktree_path, timeout=timeout, shell=True,
                       should_abort=should_abort)
        duration_ms = int((time.monotonic() - t_start) * 1000)
        output = r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")
        if r.aborted:
            out = f"$ {cmd}\nABORTED by interrupt; process tree killed\n{output}\n"
            redacted += redact.write_redacted(out_file, out)
            evidence.append({"command": cmd, "exit_code": -1,
                             "duration_ms": duration_ms,
                             "output_ref": str(out_file), "aborted": True})
            return False, evidence, redacted
        if r.timed_out:
            out = f"$ {cmd}\nTIMEOUT after {timeout}s\n{output}\n"
        else:
            out = (f"$ {cmd}\nexit={r.exit_code}\n--- output (capped) ---\n"
                   f"{output}\n")
        redacted += redact.write_redacted(out_file, out)
        evidence.append({"command": cmd, "exit_code": r.exit_code,
                         "duration_ms": duration_ms,
                         "output_ref": str(out_file)})
        if r.exit_code != 0:
            success = False
    return success, evidence, redacted


def evidence_subdir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".skein" / "evidence"
