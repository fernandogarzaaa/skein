"""Shipping lifecycle: land done nodes' result commits, tag releases.

A node is not useful until its work reaches the branch people build
from. `ship` merges a done node's recorded result commit into a target
branch; `release` tags a branch HEAD as a named release.

Everything rides on the event log: `shipped` events (one per node per
target branch) are derived into node["shipped"] by reduce_events, and
`release` events are repo-level entries anchored at the reserved
"skein-release" id, listed by scanning the log. Only merge commits and
annotated tags are created; history is never rewritten (no resets, no
force-pushes, no rebases).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import graph as g
from . import ids
from . import worktree as wt

# Tag names become git refs; keep the conservative node-id-style schema
# (no spaces, no shell-hostile characters).
TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Reserved node id that anchors repo-level release events. It is a valid
# node id syntactically but reduce_events derives no node state from
# "release" events, so a real node with this id would be unaffected
# (still: do not create one).
RELEASE_ANCHOR = "skein-release"


class ShipError(Exception):
    pass


class ReleaseError(Exception):
    pass


def _git(repo_root: str | Path, *args: str,
         identity: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if identity:
        # Skein-created commits/tags carry explicit identity flags and
        # never touch the user's configured git identity.
        cmd += ["-c", "user.name=skein", "-c", "user.email=skein@localhost"]
    cmd += list(args)
    return subprocess.run(cmd, cwd=str(repo_root),
                          capture_output=True, text=True)


def _resolve_commit(repo_root: str | Path, ref: str) -> str:
    r = _git(repo_root, "rev-parse", "--verify", ref + "^{commit}")
    if r.returncode != 0:
        raise ShipError(f"ref '{ref}' does not exist in this repo")
    return r.stdout.strip()


def _is_ancestor(repo_root: str | Path, maybe_anc: str, desc: str) -> bool:
    return _git(repo_root, "merge-base", "--is-ancestor",
                maybe_anc, desc).returncode == 0


def _conflicted_files(work_dir: str | Path) -> List[str]:
    r = _git(work_dir, "diff", "--name-only", "--diff-filter=U")
    return sorted(l for l in (r.stdout or "").splitlines() if l.strip())


def _conflict_message(node_id: str, target: str,
                      conflicts: List[str]) -> str:
    files = ", ".join(conflicts) if conflicts else "(unknown files)"
    return (f"merge of node '{node_id}' result into '{target}' conflicted "
            f"on: {files}; resolve manually and ship again")


def _merge_into(repo_root: str | Path, node_id: str, result_commit: str,
                target: str, ff_only: bool) -> str:
    """Merge result_commit into target. Returns the new target HEAD SHA.

    When the target is the currently checked-out branch the merge runs
    in the live working tree (git refuses anything that would clobber
    local changes); otherwise it runs in a throwaway worktree so the
    user's checkout is never touched. A failed merge is aborted before
    raising, so no half-merged state is left behind.
    """
    message = f"skein: ship {node_id} ({result_commit[:8]})"
    if target == wt.current_branch(repo_root):
        if ff_only:
            r = _git(repo_root, "merge", "--ff-only", result_commit,
                     identity=True)
        else:
            r = _git(repo_root, "merge", "--no-ff", "--no-edit",
                     "-m", message, result_commit, identity=True)
        if r.returncode != 0:
            conflicts = _conflicted_files(repo_root)
            _git(repo_root, "merge", "--abort")
            raise ShipError(_conflict_message(node_id, target, conflicts))
        return _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="skein-ship-"))
    try:
        r = _git(repo_root, "worktree", "add", str(tmp), target)
        if r.returncode != 0:
            raise ShipError(f"cannot check out target '{target}': "
                            f"{r.stderr.strip()[:300]}")
        if ff_only:
            m = _git(tmp, "merge", "--ff-only", result_commit, identity=True)
        else:
            m = _git(tmp, "merge", "--no-ff", "--no-edit",
                     "-m", message, result_commit, identity=True)
        if m.returncode != 0:
            conflicts = _conflicted_files(tmp)
            _git(tmp, "merge", "--abort")
            raise ShipError(_conflict_message(node_id, target, conflicts))
        return _git(tmp, "rev-parse", "HEAD").stdout.strip()
    finally:
        _git(repo_root, "worktree", "remove", "--force", str(tmp))
        _git(repo_root, "worktree", "prune")


def _target_diverged(repo_root: str | Path, base_commit: Optional[str],
                     target_sha: str) -> bool:
    """True when the target moved in real (non-control-plane) files since
    the result's recorded base. .skein commits (event log, derived graph)
    are control-plane churn: every claim/complete appends one, so a raw
    HEAD != base comparison would warn on every ship."""
    if not base_commit or base_commit == target_sha:
        return False
    r = _git(repo_root, "diff", "--name-only", base_commit, target_sha,
             "--", ".", ":!.skein")
    return bool(r.stdout.strip())


def _done_node_with_result(repo_root: str | Path,
                           node_id: str) -> Dict:
    """Load the node and enforce the ship preconditions that do not
    touch git: done status plus a recorded result commit whose object
    still exists (a dead ref is never shipped)."""
    ids.validate_node_id(node_id)
    node = g.load_graph(repo_root).get(node_id)
    if node is None:
        raise ShipError(f"unknown node '{node_id}'")
    if node.get("removed"):
        raise ShipError(f"node '{node_id}' has been removed")
    if node.get("status") != "done":
        raise ShipError(f"node '{node_id}' is '{node.get('status')}', "
                        f"not done; only done nodes ship")
    result = node.get("result") or {}
    result_commit = result.get("commit")
    if not result_commit:
        raise ShipError(f"node '{node_id}' has no result record; "
                        f"nothing to ship")
    if _git(repo_root, "cat-file", "-e",
            result_commit + "^{commit}").returncode != 0:
        raise wt.BaseCommitUnavailable(
            f"result commit {result_commit[:8]} for node '{node_id}' is "
            f"gone from the object store; refusing to ship a dead ref")
    return node


def ship_node(repo_root: str | Path, node_id: str,
              target: Optional[str] = None,
              ff_only: bool = False,
              force: bool = False,
              actor: Optional[str] = None) -> Dict:
    """Merge a done node's result commit into the target branch.

    Returns a dict with status "shipped" or "already-shipped" (the
    merge was a no-op because the result is already an ancestor of the
    target; no event is appended in that case).
    """
    actor = actor or "ship"
    node = _done_node_with_result(repo_root, node_id)
    result = node["result"]
    result_commit = result["commit"]
    base_commit = result.get("base_commit")
    target = target or wt.current_branch(repo_root)
    target_sha = _resolve_commit(repo_root, target)
    if _is_ancestor(repo_root, result_commit, target_sha):
        return {"node_id": node_id, "target": target,
                "status": "already-shipped",
                "result_commit": result_commit, "target_sha": target_sha}
    diverged = _target_diverged(repo_root, base_commit, target_sha)
    if diverged and not force:
        raise ShipError(
            f"target '{target}' moved since node '{node_id}' ran "
            f"(real changes on '{target}' past recorded base "
            f"{base_commit[:8]}); ship with --force to merge anyway, or "
            f"re-run the node on the new base")
    if ff_only and not _is_ancestor(repo_root, target_sha, result_commit):
        raise ShipError(f"cannot fast-forward '{target}' to "
                        f"{result_commit[:8]}: target has commits the "
                        f"result does not contain")
    merge_commit = _merge_into(repo_root, node_id, result_commit, target,
                               ff_only)
    g.append_event(repo_root, actor, "shipped", node_id, {
        "result_commit": result_commit,
        "base_commit": base_commit,
        "target_branch": target,
        "target_head_before": target_sha,
        "merge_commit": merge_commit,
        "ff_only": bool(ff_only),
        "diverged": diverged,
        "forced": bool(force and diverged),
    })
    return {"node_id": node_id, "target": target, "status": "shipped",
            "result_commit": result_commit, "merge_commit": merge_commit,
            "diverged": diverged}


def _ship_order(nodes: Dict[str, Dict]) -> List[str]:
    """Topological ship order over done nodes with result records:
    dependencies before dependents, integration nodes last."""
    wanted = {nid for nid, n in nodes.items()
              if not n.get("removed") and n.get("status") == "done"
              and (n.get("result") or {}).get("commit")}
    # Kahn's algorithm, deterministic via sorted picks.
    deps = {nid: [d for d in nodes[nid].get("depends_on", []) if d in wanted]
            for nid in wanted}
    order: List[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(nid for nid, ds in remaining.items()
                       if all(d not in remaining for d in ds))
        if not ready:  # cycle: drain alphabetically, do not hang
            ready = sorted(remaining)
        for nid in ready:
            order.append(nid)
            del remaining[nid]
    integ = [nid for nid in order if nodes[nid].get("integration_for")]
    rest = [nid for nid in order if not nodes[nid].get("integration_for")]
    return rest + integ


def ship_all(repo_root: str | Path, target: Optional[str] = None,
             actor: Optional[str] = None) -> List[Dict]:
    """Ship every done node with a result record, in dependency order.

    Returns per-node result dicts: status is "shipped", "already-shipped",
    or "skipped" with a "reason". A node whose dependency was not
    shipped (failed, skipped, or not done) is skipped with reason
    "dependency <id> not shipped". One node's merge conflict does not
    stop the rest; it is reported as a skip with the conflict list.

    Within one --all run the divergence guard is relaxed (force): each
    ship legitimately moves the target for the next one, and that
    movement is exactly the content just merged, so warning on it would
    make --all refuse every DAG. Merge conflicts still abort the merge
    and are reported per node. Use single-node `ship` for the guarded
    interactive path.
    """
    actor = actor or "ship"
    target = target or wt.current_branch(repo_root)
    # Resolve the target once up front so a typo fails fast instead of
    # after half the graph shipped.
    _resolve_commit(repo_root, target)
    nodes = g.load_graph(repo_root)
    landed: set = set()  # node ids whose result is now in the target
    results: List[Dict] = []
    for nid in _ship_order(nodes):
        node = nodes[nid]
        missing = [d for d in node.get("depends_on", []) if d not in landed]
        if missing:
            results.append({"node_id": nid, "target": target,
                            "status": "skipped",
                            "reason": f"dependency {missing[0]} not shipped"})
            continue
        try:
            r = ship_node(repo_root, nid, target=target, force=True,
                          actor=actor)
        except (ShipError, wt.BaseCommitUnavailable) as e:
            results.append({"node_id": nid, "target": target,
                            "status": "skipped", "reason": str(e)[:300]})
            continue
        results.append(r)
        landed.add(nid)
    return results


def _worktree_dirty(repo_root: str | Path) -> bool:
    """True when the working tree has uncommitted changes outside the
    skein control plane (.skein is committed by the CLI itself)."""
    r = _git(repo_root, "status", "--porcelain", "--untracked-files=normal")
    for line in (r.stdout or "").splitlines():
        path = line[3:].strip().strip('"') if len(line) > 3 else ""
        if " -> " in path:  # rename: take the new name
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if not path:
            continue
        if path == ".skein" or path.startswith(".skein/"):
            continue
        return True
    return False


def list_releases(repo_root: str | Path) -> List[Dict]:
    """Release events in log order (the event log is the source of truth;
    no derived node state is kept for releases)."""
    return [e for e in g.load_events(repo_root)
            if e.get("type") == "release"]


def shipped_nodes_since_last_release(repo_root: str | Path) -> List[str]:
    """Node ids with a shipped event after the most recent release event
    (per-repo seq is the true local order key)."""
    events = g.load_events(repo_root)
    last_seq = -1
    for e in events:
        if e.get("type") == "release":
            seq = e.get("seq")
            if isinstance(seq, int) and seq > last_seq:
                last_seq = seq
    out: List[str] = []
    for e in events:
        if e.get("type") == "shipped":
            seq = e.get("seq")
            if isinstance(seq, int) and seq > last_seq:
                nid = e.get("node_id")
                if nid and nid not in out:
                    out.append(nid)
    return out


def release_tag(repo_root: str | Path, tag: str,
                message: Optional[str] = None,
                target: Optional[str] = None,
                allow_unshipped: bool = False,
                actor: Optional[str] = None) -> Dict:
    """Create an annotated tag on the target branch HEAD and record a
    release event with the nodes shipped since the previous release."""
    actor = actor or "release"
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise ReleaseError(
            f"invalid tag {tag!r}: must match ^[A-Za-z0-9._-]+$ "
            f"(no spaces)")
    target = target or wt.current_branch(repo_root)
    r = _git(repo_root, "rev-parse", "--verify", target + "^{commit}")
    if r.returncode != 0:
        raise ReleaseError(f"target branch '{target}' does not exist")
    head = r.stdout.strip()
    if _git(repo_root, "rev-parse", "--verify",
            f"refs/tags/{tag}").returncode == 0:
        raise ReleaseError(f"tag '{tag}' already exists")
    if _worktree_dirty(repo_root):
        raise ReleaseError("working tree is dirty; commit or stash changes "
                           "before cutting a release")
    nodes = g.load_graph(repo_root)
    # Done nodes whose result never shipped block the release. A node
    # counts as shipped when it has a shipped event for this target, or
    # when its result commit is already an ancestor of the target (e.g.
    # it landed via a manual conflict resolution after `ship` reported
    # the conflict and aborted cleanly).
    unshipped = []
    for nid in sorted(nodes):
        n = nodes[nid]
        if (n.get("removed") or n.get("status") != "done"
                or not (n.get("result") or {}).get("commit")
                or target in (n.get("shipped") or {})):
            continue
        if _is_ancestor(repo_root, n["result"]["commit"], target):
            continue
        unshipped.append(nid)
    if unshipped and not allow_unshipped:
        raise ReleaseError(
            f"unshipped done nodes: {', '.join(unshipped)}; ship them or "
            f"re-run with --allow-unshipped")
    msg = message or f"skein release {tag}"
    t = _git(repo_root, "tag", "-a", tag, head, "-m", msg, identity=True)
    if t.returncode != 0:
        raise ReleaseError(f"git tag failed: {t.stderr.strip()[:300]}")
    shipped_list = shipped_nodes_since_last_release(repo_root)
    g.append_event(repo_root, actor, "release", RELEASE_ANCHOR, {
        "tag": tag,
        "message": msg,
        "target_branch": target,
        "head": head,
        "shipped_nodes": shipped_list,
        "unshipped_overridden": sorted(unshipped)
        if (unshipped and allow_unshipped) else [],
    })
    return {"tag": tag, "target": target, "head": head,
            "shipped_nodes": shipped_list}
