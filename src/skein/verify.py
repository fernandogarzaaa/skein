"""Verification gate: execute completion commands, capture evidence."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


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
        out_file = Path(evidence_dir) / f"{node_id}_{ts}_{i}.log"
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(worktree_path),
                               capture_output=True, text=True, timeout=timeout)
            out = f"$ {cmd}\nexit={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n"
            out_file.write_text(out, encoding="utf-8")
            evidence.append({"command": cmd, "exit_code": r.returncode,
                             "output_ref": str(out_file)})
            if r.returncode != 0:
                success = False
        except subprocess.TimeoutExpired as e:
            out_file.write_text(f"$ {cmd}\nTIMEOUT after {timeout}s\n{e}", encoding="utf-8")
            evidence.append({"command": cmd, "exit_code": 124,
                             "output_ref": str(out_file)})
            success = False
    return success, evidence


def evidence_subdir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".skein" / "evidence"
