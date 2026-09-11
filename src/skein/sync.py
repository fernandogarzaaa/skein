"""Multi-machine sync: share the event log through git.

The log is append-only newline-delimited JSON, so concurrent appends on
two machines merge by line union; the reduction is order-insensitive
(last-write-wins by event timestamp), which makes the union safe.
Derived graph.json is rebuilt after every merge, never merged itself.
Conflicts outside .skein/ are the user's to resolve - sync aborts the
merge and says so rather than touching user files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List


def _git(repo_root: Any, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=str(repo_root),
                          capture_output=True, text=True)


def _current_branch(repo_root: Any) -> str:
    r = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode != 0:
        raise ValueError("not a git repo (skein sync needs a git remote)")
    return r.stdout.strip()


def _unmerged_files(repo_root: Any) -> List[str]:
    r = _git(repo_root, "diff", "--name-only", "--diff-filter=U")
    if r.returncode != 0:
        return []
    return [l for l in r.stdout.splitlines() if l.strip()]


def _union_merge_log(repo_root: Any) -> bool:
    """Resolve a log.ndjson conflict by line union. Returns True if the
    conflict was confined to .skein/ and resolved."""
    from . import graph as g
    unmerged = _unmerged_files(repo_root)
    if not unmerged:
        return False
    if any(not f.startswith(".skein/") for f in unmerged):
        return False
    log = g.log_path(repo_root)
    lines: List[str] = []
    seen = set()
    # union across merge stages: ours (:2), theirs (:3), plus base (:1)
    for stage in ("1", "2", "3"):
        r = _git(repo_root, "show", f":{stage}:{g.SKEIN_DIR}/{g.LOG_NAME}")
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
    log.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    g.rebuild_graph(repo_root)
    _git(repo_root, "add", g.SKEIN_DIR)
    r = _git(repo_root, "commit", "-m", "skein: sync auto-merge (log union)")
    return r.returncode == 0


def sync_repo(repo_root: Any, push: bool = True,
              remote: str = "origin") -> Dict[str, Any]:
    """Fetch, merge, rebuild, push. Returns a status dict; never raises
    on merge conflicts (reported as status 'conflict')."""
    from . import graph as g
    branch = _current_branch(repo_root)
    if _git(repo_root, "remote", "get-url", remote).returncode != 0:
        raise ValueError(f"no git remote '{remote}' (nothing to sync with)")
    # checkpoint any uncommitted .skein state first
    _git(repo_root, "add", g.SKEIN_DIR)
    _git(repo_root, "commit", "-m", "skein: sync checkpoint")
    fetched = _git(repo_root, "fetch", remote)
    if fetched.returncode != 0:
        raise ValueError(f"fetch from '{remote}' failed: {fetched.stderr.strip()}")
    if _git(repo_root, "rev-parse", "--verify",
            f"{remote}/{branch}").returncode != 0:
        # remote branch does not exist yet: just push
        pushed = False
        if push:
            r = _git(repo_root, "push", "-u", remote, branch)
            pushed = r.returncode == 0
        return {"status": "ok", "pulled": False, "pushed": pushed,
                "detail": "remote branch absent; pushed local state"}
    merged = _git(repo_root, "merge", "--no-edit", f"{remote}/{branch}")
    if merged.returncode != 0:
        if _union_merge_log(repo_root):
            detail = "log conflict auto-merged by line union"
        else:
            _git(repo_root, "merge", "--abort")
            return {"status": "conflict", "pulled": False, "pushed": False,
                    "detail": "conflict outside .skein/; resolve manually, "
                              "then re-run skein sync (merge aborted)"}
    else:
        detail = "fast-forward or clean merge" if "Already up to date" not in (
            merged.stdout or "") else "already up to date"
    g.rebuild_graph(repo_root)
    _git(repo_root, "add", g.SKEIN_DIR)
    _git(repo_root, "commit", "-m", "skein: sync merge")
    pushed = False
    if push:
        r = _git(repo_root, "push", remote, branch)
        pushed = r.returncode == 0
        if not pushed:
            return {"status": "ok", "pulled": True, "pushed": False,
                    "detail": detail + "; push failed: " + r.stderr.strip()[:200]}
    return {"status": "ok", "pulled": True, "pushed": pushed, "detail": detail}
