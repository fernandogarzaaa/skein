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
    """Resolve a log.ndjson conflict by event-id union. Returns True only
    if the conflict is confined to the event log and derived files.

    Anything else under .skein/ (config.json, evidence, ...) is NOT
    auto-merged: config.json is not line-delimited and a line union would
    corrupt it. The caller aborts the merge in those cases and tells the
    user to resolve manually."""
    from . import graph as g
    unmerged = _unmerged_files(repo_root)
    if not unmerged:
        return False
    auto_mergeable = {
        f"{g.SKEIN_DIR}/{g.LOG_NAME}",
        f"{g.SKEIN_DIR}/{g.GRAPH_NAME}",
        f"{g.SKEIN_DIR}/{g.META_NAME}",
    }
    if any(f not in auto_mergeable for f in unmerged):
        return False
    by_id = {}
    log_ref = f"{g.SKEIN_DIR}/{g.LOG_NAME}"
    # Working tree first: carries git's own clean merge for the hunks it
    # could resolve (conflict markers are skipped).
    lp = g.log_path(repo_root)
    if lp.exists():
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[:7] in ("<<<<<<<", "=======", ">>>>>>>"):
                continue
            ev = g.parse_event(line)
            if ev is not None:
                by_id.setdefault(ev["event_id"], line)
    for stage in ("1", "2", "3"):
        r = _git(repo_root, "show", f":{stage}:{log_ref}")
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            ev = g.parse_event(line)
            if ev is None:
                continue  # malformed lines are dropped on rebuild anyway
            by_id.setdefault(ev["event_id"], line)
    # Deterministic order: sort by (timestamp, seq, event_id) so every
    # machine converges on the same file content.
    ordered = sorted(
        (g.parse_event(l) for l in by_id.values()),
        key=lambda e: (e.get("timestamp", ""), e.get("seq", 0),
                       e.get("event_id", "")))
    lp.write_text(
        "".join(g.serialize_event(e) + "\n" for e in ordered),
        encoding="utf-8")
    g.rebuild_graph(repo_root)  # regenerates graph.json + meta from the log
    _git(repo_root, "add", g.SKEIN_DIR)
    r = _git(repo_root, "-c", "user.name=skein", "-c", "user.email=skein@localhost",
             "commit", "-m", "skein: sync auto-merge (log union)")
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
    _git(repo_root, "-c", "user.name=skein", "-c", "user.email=skein@localhost",
          "commit", "-m", "skein: sync checkpoint")
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
            detail = "log conflict auto-merged by event-id union"
        else:
            _git(repo_root, "merge", "--abort")
            return {"status": "conflict", "pulled": False, "pushed": False,
                    "detail": "conflict outside .skein/log.ndjson; resolve manually, "
                              "then re-run skein sync (merge aborted)"}
    else:
        detail = "fast-forward or clean merge" if "Already up to date" not in (
            merged.stdout or "") else "already up to date"
    g.rebuild_graph(repo_root)
    _git(repo_root, "add", g.SKEIN_DIR)
    _git(repo_root, "-c", "user.name=skein", "-c", "user.email=skein@localhost",
          "commit", "-m", "skein: sync merge")
    pushed = False
    if push:
        r = _git(repo_root, "push", remote, branch)
        pushed = r.returncode == 0
        if not pushed:
            return {"status": "ok", "pulled": True, "pushed": False,
                    "detail": detail + "; push failed: " + r.stderr.strip()[:200]}
    return {"status": "ok", "pulled": True, "pushed": pushed, "detail": detail}
