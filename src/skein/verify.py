"""Verification gate: execute completion commands, capture evidence."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


from . import ids
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
                   node_id: str, timeout: int = 600) -> Tuple[bool, List[Dict]]:
    """Execute each completion command for real in the worktree.

    Returns (success, evidence). Evidence entries:
    {command, exit_code, output_ref}. Full output is stored in a file under
    evidence_dir; output_ref points at it.
    """
    evidence: List[Dict] = []
    cmds = split_commands(completion)
    Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    if not cmds:
        return False, [{"command": "", "exit_code": 1,
                         "output_ref": None, "error": "empty completion command"}]
    success = True
    for i, cmd in enumerate(cmds):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        out_file = Path(evidence_dir) / ids.evidence_name(node_id, f"{ts}_{i}")
        t_start = time.monotonic()
        # Bounded execution: the pipe is drained continuously (no 64KB
        # deadlock), output is capped, and the whole process tree is
        # killed on timeout. Completion lines are shell by design: they
        # are operator-authored node intent, run in the worktree.
        exit_code, output = rt.run_bounded(cmd, worktree_path,
                                           timeout=timeout, shell=True)
        duration_ms = int((time.monotonic() - t_start) * 1000)
        if exit_code == 124 and "timed out" in output:
            out = f"$ {cmd}\nTIMEOUT after {timeout}s\n{output}\n"
        else:
            out = (f"$ {cmd}\nexit={exit_code}\n--- output (capped) ---\n"
                   f"{output}\n")
        out_file.write_text(out, encoding="utf-8")
        evidence.append({"command": cmd, "exit_code": exit_code,
                         "duration_ms": duration_ms,
                         "output_ref": str(out_file)})
        if exit_code != 0:
            success = False
    return success, evidence


def evidence_subdir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".skein" / "evidence"
