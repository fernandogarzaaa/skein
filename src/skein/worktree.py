"""Worktree + branching strategy for Skein.

Core invariant: a node marked done produces a durable git result commit.
Downstream nodes branch from the dependency's *result commit*, never from
a branch name that may not contain the parent's actual changes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import graph as g
from . import ids


def _is_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", value or ""))


class WorktreeError(Exception):
    pass


class BaseCommitUnavailable(WorktreeError):
    """The intended base ref does not exist. Never silently substituted."""


def _git(repo_root: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=str(repo_root),
                          capture_output=True, text=True)


def current_branch(repo_root: str | Path) -> str:
    r = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode == 0:
        return r.stdout.strip()
    return "main"


def _resolve_ref(repo_root: str | Path, ref: str) -> str:
    """Resolve a branch/SHA to a commit SHA, or raise BaseCommitUnavailable."""
    r = _git(repo_root, "rev-parse", "--verify", ref + "^{commit}")
    if r.returncode != 0:
        raise BaseCommitUnavailable(
            f"base ref '{ref}' does not exist in this repo; refusing to "
            f"substitute another branch (would silently break dependency "
            f"semantics)")
    return r.stdout.strip()


def base_branch_for(repo_root: str | Path, node_id: str,
                    nodes: Optional[Dict[str, Dict]] = None) -> str:
    """Return the base *ref* a node's worktree should branch from.

    Single dependency: the dependency's recorded result commit (durable),
    falling back to its branch only when no result commit exists yet
    (legacy nodes). Multi-dependency: the integration branch.
    """
    nodes = nodes if nodes is not None else g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise BaseCommitUnavailable(
            f"cannot choose a base for unknown node '{node_id}'")
    deps = node.get("depends_on", [])
    if len(deps) == 0:
        return current_branch(repo_root)
    if len(deps) == 1:
        d = nodes.get(deps[0])
        if d is None:
            raise BaseCommitUnavailable(
                f"cannot choose a base for '{node_id}': dependency "
                f"'{deps[0]}' is missing; refusing to substitute the "
                f"current branch")
        result_commit = (d.get("result") or {}).get("commit")
        if result_commit:
            # The SHA is durable only while the object exists; a pruned
            # object store must raise here, never fall back silently.
            try:
                return _resolve_ref(repo_root, result_commit)
            except BaseCommitUnavailable:
                raise BaseCommitUnavailable(
                    f"cannot choose a base for '{node_id}': dependency "
                    f"'{deps[0]}' records result commit "
                    f"'{result_commit[:8]}' but the object is gone from "
                    f"this repo (pruned?); refusing to substitute")
        dep_branch = d.get("worktree", {}).get("branch")
        if dep_branch:
            try:
                _resolve_ref(repo_root, dep_branch)
            except BaseCommitUnavailable:
                raise BaseCommitUnavailable(
                    f"cannot choose a base for '{node_id}': dependency "
                    f"'{deps[0]}' has no result commit and its branch "
                    f"'{dep_branch}' no longer exists; refusing to "
                    f"substitute")
            return dep_branch
        raise BaseCommitUnavailable(
            f"cannot choose a base for '{node_id}': dependency "
            f"'{deps[0]}' has no result commit or branch yet")
    # multi-dependency: base is the integration branch
    return f"skein/{node_id}__integrate-base"


def branch_for(node_id: str) -> str:
    ids.validate_node_id(node_id)
    return f"skein/{node_id}"


def worktree_path_for(repo_root: str | Path, node_id: str) -> Path:
    return ids.worktree_path_for(repo_root, node_id)


def _norm_wt_path(p: str) -> str:
    """Normalize a git-reported worktree path to OS-native separators.

    `git worktree list --porcelain` emits forward slashes even on Windows,
    while Path operations use backslashes there; without normalization,
    dict lookups of registered worktrees always miss on Windows.
    """
    return str(Path(p))


def _registered_worktrees(repo_root: str | Path) -> Dict[str, Dict[str, str]]:
    """Parse `git worktree list --porcelain` into {abspath: {branch, head}}."""
    r = _git(repo_root, "worktree", "list", "--porcelain")
    out: Dict[str, Dict[str, str]] = {}
    cur: Dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        if line.startswith("worktree "):
            if cur.get("path"):
                out[_norm_wt_path(cur["path"])] = cur
            cur = {"path": _norm_wt_path(line[len("worktree "):].strip())}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref.split("/")[-1] if "/" in ref else ref
            # keep full ref too for skein/ namespaced branches
            cur["branch_ref"] = ref
        elif line == "" and cur.get("path"):
            out[_norm_wt_path(cur["path"])] = cur
            cur = {}
    if cur.get("path"):
        out[_norm_wt_path(cur["path"])] = cur
    return out


def ensure_worktree(repo_root: str | Path, node_id: str,
                    actor: str = "supervisor") -> Tuple[Path, str, str]:
    """Create (or reuse) the node's worktree branched from the right base.

    Returns (path, branch, base_ref). Records branch info on the node via
    node_edited event (no status change).

    An existing directory that is NOT a registered git worktree is stale
    and is removed before recreating; a missing base ref raises
    BaseCommitUnavailable instead of silently falling back.
    """
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise WorktreeError(f"unknown node '{node_id}'")
    wt_rec = node.get("worktree", {}) or {}
    branch = wt_rec.get("branch") or branch_for(node_id)
    base = wt_rec.get("base_branch") or base_branch_for(repo_root, node_id, nodes)
    path = Path(wt_rec.get("path") or str(worktree_path_for(repo_root, node_id)))

    # The base is pinned as a SHA in the worktree record at creation time,
    # and the pinned SHA is authoritative on reuse: a base branch name may
    # have moved on since (every skein event commits .skein), but the
    # worktree is still on its original base. When the base is itself a
    # pinned SHA (a dependency's result commit) and it no longer matches the
    # recorded one, the dependency re-ran: refuse, the worktree is stale.
    # Legacy records without a pinned SHA fall back to resolving the base
    # branch. The base must exist; never substitute silently.
    recorded_base = wt_rec.get("base_commit")
    if recorded_base:
        if _git(repo_root, "cat-file", "-e",
                recorded_base + "^{commit}").returncode != 0:
            raise BaseCommitUnavailable(
                f"recorded base commit {recorded_base[:8]} for node "
                f"'{node_id}' is gone from the object store; the worktree "
                f"cannot be reused")
        base_sha = recorded_base
        if _is_sha(base):
            # Pinned-SHA base (a dependency's result commit): the base is
            # immutable, so re-resolve what it should be right now. A
            # mismatch means the dependency re-ran since this worktree was
            # created: the worktree sits on a different base.
            current_base = base_branch_for(repo_root, node_id, nodes)
            if current_base != recorded_base:
                raise WorktreeError(
                    f"node '{node_id}' base changed from "
                    f"{recorded_base[:8]} to {current_base[:8]} (dependency "
                    f"re-ran); refusing to reuse a worktree sitting on a "
                    f"different base. Run `skein worktree gc` to remove the "
                    f"stale worktree, or reset the node.")
    else:
        base_sha = _resolve_ref(repo_root, base)

    path.parent.mkdir(parents=True, exist_ok=True)
    registered = _registered_worktrees(repo_root)
    reg = registered.get(str(path.resolve())) or registered.get(str(path))
    if reg is not None:
        # Registered worktree: verify it is really ours (right repo is
        # implied by the porcelain list; check branch matches).
        reg_branch = reg.get("branch_ref") or reg.get("branch")
        if reg_branch and reg_branch != branch and not reg_branch.endswith("/" + branch):
            raise WorktreeError(
                f"worktree at {path} is registered on branch '{reg_branch}', "
                f"expected '{branch}'; refusing to reuse")
        head = reg.get("head")
        if head and head != base_sha:
            # HEAD is allowed to be the base itself or a descendant (work
            # in progress on the right base). Anything else means the
            # worktree sits on a different base: refuse, do not reuse.
            r = _git(repo_root, "merge-base", "--is-ancestor", base_sha, head)
            if r.returncode != 0:
                raise WorktreeError(
                    f"worktree at {path} (HEAD {head[:8]}) has diverged from "
                    f"its expected base {base_sha[:8]}; refusing to reuse a "
                    f"worktree sitting on a different base. Run "
                    f"`skein worktree gc` to remove the stale worktree, or "
                    f"reset the node.")
        g.append_event(repo_root, actor, "node_edited", node_id,
                       {"worktree": {"branch": branch, "base_branch": base,
                                     "base_commit": base_sha,
                                     "path": str(path)}})
        return path, branch, base
    if path.exists() or (path / ".git").exists():
        # Stale directory masquerading as a worktree: it lives under the
        # skein-controlled worktrees root, so remove and recreate.
        shutil.rmtree(path, ignore_errors=True)
    # create branch if missing, from base
    if _git(repo_root, "rev-parse", "--verify", branch).returncode != 0:
        r = _git(repo_root, "branch", branch, base)
        if r.returncode != 0:
            raise WorktreeError(f"cannot create branch {branch} from {base}: {r.stderr}")
    r = _git(repo_root, "worktree", "add", str(path), branch)
    if r.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {r.stderr}")
    g.append_event(repo_root, actor, "node_edited", node_id,
                   {"worktree": {"branch": branch, "base_branch": base,
                                 "base_commit": base_sha, "path": str(path)}})
    return path, branch, base


def ensure_integration_node(repo_root: str | Path, node_id: str,
                            actor: str = "supervisor") -> Optional[str]:
    """For a multi-dependency node, auto-create an integration node.

    Returns the integration node id, or None if not needed.
    The integration node merges parent branches into one base branch;
    a deterministic merge is attempted first, else the node stays
    claimable via the normal claim mechanism. The child cannot be
    claimed until the integration node is done (see claim.eligibility).
    """
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise WorktreeError(f"unknown node '{node_id}'")
    deps = node.get("depends_on", [])
    if len(deps) < 2:
        return None
    integ_id = f"{node_id}__integrate"
    if integ_id in nodes and not nodes[integ_id].get("removed"):
        return integ_id
    g.append_event(repo_root, actor, "node_added", integ_id, {
        "title": f"Integrate parents of {node_id}",
        "intent": {
            "goal": f"Merge parent branches {deps} into one base branch for {node_id}.",
            "context": "Use deterministic git merge; resolve conflicts if automatic merge fails.",
            "constraints": "Do not change parent branches; only produce the integration branch.",
            "completion": "git branch --list",
        },
        "depends_on": deps,
        "blast_radius": [],
        "integration_for": node_id,
    })
    # point the child at the integration base
    g.append_event(repo_root, actor, "node_edited", node_id,
                   {"worktree": {"base_branch": f"skein/{integ_id}-base"}})
    auto_merge_parents(repo_root, integ_id, actor=actor)
    return integ_id


def _parent_refs(repo_root: str | Path, nodes: Dict[str, Dict],
                 deps: List[str]) -> List[str]:
    """Durable ref for each parent: result commit first, branch fallback."""
    refs = []
    for d in deps:
        dd = nodes.get(d)
        if dd is None:
            raise WorktreeError(f"integration parent '{d}' is missing")
        commit = (dd.get("result") or {}).get("commit")
        if commit:
            refs.append(commit)
        else:
            b = (dd.get("worktree") or {}).get("branch")
            if not b:
                raise WorktreeError(
                    f"integration parent '{d}' has no result commit or branch")
            refs.append(b)
    return refs


def auto_merge_parents(repo_root: str | Path, integ_id: str,
                       actor: str = "supervisor") -> bool:
    """Attempt deterministic merge of integration node's parent branches.

    Returns True if merged without agent help. The deterministic merge
    runs as a fenced system attempt (holder 'skein-auto-merge'): on
    success the integration node is marked done with evidence AND a
    result record (base/result commits); on conflict the attempt is
    released and the node stays claimable, and the child stays blocked
    until it resolves.
    """
    from . import claim as c
    nodes = g.load_graph(repo_root)
    node = nodes.get(integ_id)
    if node is None:
        return False
    deps = node.get("depends_on", [])
    if not deps:
        return False
    try:
        dep_refs = _parent_refs(repo_root, nodes, deps)
        for ref in dep_refs:
            _resolve_ref(repo_root, ref)
    except WorktreeError:
        return False
    integ_branch = f"skein/{integ_id}-base"
    # reset integration branch from first parent (delete if exists)
    _git(repo_root, "branch", "-D", integ_branch)
    first = dep_refs[0]
    r = _git(repo_root, "branch", integ_branch, first)
    if r.returncode != 0:
        return False
    try:
        inode = c.claim_node(repo_root, integ_id,
                             holder="skein-auto-merge", actor=actor)
    except c.ClaimError:
        return False  # owned by someone else; leave it alone
    token = (inode.get("claim") or {})["claim_token"]
    result_commit, conflicts = _merge_in_temp(repo_root, integ_id, dep_refs,
                                              integ_branch)
    if result_commit:
        c.complete_node(
            repo_root, integ_id, "skein-auto-merge", token,
            handoff_note=f"Auto-merged {dep_refs} into {integ_branch}.",
            evidence=[{"command": "git merge " + " ".join(dep_refs[1:]),
                       "exit_code": 0, "output_ref": None}],
            worktree={"branch": integ_branch, "base_branch": dep_refs[0],
                      "path": None},
            result={"base_commit": dep_refs[0]
                    if len(dep_refs[0]) == 40 else None,
                    "commit": result_commit,
                    "changed_files": [], "diff_stats": {}})
        return True
    # Conflict: park the node for a human with the conflict list in the
    # event payload and the handoff note, instead of silently releasing
    # it back to unclaimed. The human_interrupt clears the auto-merge
    # claim atomically; the node_edited then records what conflicts.
    files = ", ".join(conflicts) if conflicts else "(unknown files)"
    note = (f"auto-merge conflicted on: {files}; resolve the conflicts "
            f"in branch '{integ_branch}' and mark the node done")
    g.append_event(repo_root, actor, "human_interrupt", integ_id,
                   {"action": "edit",
                    "reason": note,
                    "conflicts": conflicts,
                    "integration_branch": integ_branch})
    g.append_event(repo_root, actor, "node_edited", integ_id,
                   {"handoff_note": note})
    return False


def _merge_in_temp(repo_root: str | Path, integ_id: str,
                   dep_refs: List[str], integ_branch: str
                   ) -> Tuple[Optional[str], List[str]]:
    """Merge parent refs into the integration branch inside a throwaway
    worktree (never touches the user's checkout).

    Returns (merge_commit, conflict_files): merge_commit is the new
    branch HEAD on success; on conflict it is None and conflict_files
    lists the paths git could not auto-merge."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skein-integrate-"))
    try:
        r = subprocess.run(["git", "worktree", "add", str(tmp), integ_branch],
                           cwd=str(repo_root), capture_output=True, text=True)
        if r.returncode != 0:
            return None, []
        for other in dep_refs[1:]:
            m = subprocess.run(["git", "merge", "--no-ff", "--no-edit", other],
                               cwd=str(tmp), capture_output=True, text=True)
            if m.returncode != 0:
                u = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=str(tmp), capture_output=True, text=True)
                conflicts = sorted(l for l in (u.stdout or "").splitlines()
                                   if l.strip())
                subprocess.run(["git", "merge", "--abort"], cwd=str(tmp),
                               capture_output=True)
                subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                               cwd=str(repo_root), capture_output=True)
                return None, conflicts
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                       cwd=str(repo_root), capture_output=True)
        return _git(repo_root, "rev-parse", integ_branch).stdout.strip(), []
    finally:
        subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root),
                       capture_output=True)


def remove_worktree(repo_root: str | Path, node_id: str, delete_branch: bool = False) -> bool:
    """Remove a node's registered worktree (branch kept unless asked).

    Never touches the main repo working tree: the resolved worktree path
    is checked against the repo root first, and `git worktree remove`
    itself refuses the main worktree.

    The node lookup goes through the full event reduction (not load_graph)
    because load_graph hides removed nodes, and this runs as part of the
    node-deletion path after the node_removed event is appended.
    """
    nodes = g.reduce_events(g.load_events(repo_root))
    node = nodes.get(node_id)
    path = (node or {}).get("worktree", {}).get("path")
    branch = (node or {}).get("worktree", {}).get("branch")
    if path:
        try:
            if Path(path).resolve() == Path(repo_root).resolve():
                raise WorktreeError(
                    f"refusing to remove worktree at {path}: it is the main "
                    f"repo working tree")
        except OSError:
            pass
        subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                       cwd=str(repo_root), capture_output=True)
    subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root), capture_output=True)
    if delete_branch and branch:
        subprocess.run(["git", "branch", "-D", branch], cwd=str(repo_root),
                       capture_output=True)
    return True


def _worktrees_root(repo_root: str | Path) -> Path:
    # ids.worktree_path_for guarantees paths under .skein/worktrees;
    # the parent of any node path is that root.
    return worktree_path_for(repo_root, "x").parent


def find_orphaned_worktrees(repo_root: str | Path) -> List[Tuple[str, str]]:
    """Scan for orphaned/stale skein worktrees without removing anything.

    Returns (path, reason) pairs: registered worktrees no live node
    claims (or whose branch ref is gone), plus directories under
    .skein/worktrees that are not registered git worktrees at all.
    The main repo working tree is never reported. Shared read-only
    core of gc_worktrees() and `skein doctor`.
    """
    found: List[Tuple[str, str]] = []
    root = Path(repo_root).resolve()
    wt_root = _worktrees_root(repo_root).resolve()
    nodes = g.load_graph(repo_root)
    live_paths: Dict[str, Dict] = {}
    for nid, n in nodes.items():
        if n.get("removed"):
            continue
        p = (n.get("worktree") or {}).get("path")
        if p:
            try:
                live_paths[str(Path(p).resolve())] = n
            except OSError:
                continue
    registered = _registered_worktrees(repo_root)
    for wpath, info in registered.items():
        try:
            rp = str(Path(wpath).resolve())
        except OSError:
            continue
        if rp == str(root):
            continue  # never touch the main working tree
        under_skein = rp == str(wt_root) or rp.startswith(str(wt_root) + os.sep)
        node = live_paths.get(rp)
        if not under_skein and node is None:
            continue  # not skein-managed; leave alone
        reason = ""
        if node is None:
            reason = "no live node claims it"
        else:
            if node.get("status") in ("claimed", "in_progress"):
                continue  # an agent may be working there right now
            expected_branch = (node.get("worktree") or {}).get("branch")
            if expected_branch and _git(
                    repo_root, "rev-parse", "--verify",
                    expected_branch).returncode != 0:
                reason = f"branch '{expected_branch}' is gone"
        if reason:
            found.append((wpath, reason))
    # Stale directories: under .skein/worktrees but not registered worktrees.
    if wt_root.is_dir():
        reg_paths = set()
        for p in registered:
            try:
                reg_paths.add(str(Path(p).resolve()))
            except OSError:
                continue
        for child in sorted(wt_root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                if str(child.resolve()) in reg_paths:
                    continue
            except OSError:
                continue
            found.append((str(child), "stale directory (not a registered worktree)"))
    return found


def gc_worktrees(repo_root: str | Path) -> List[str]:
    """Remove orphaned and stale skein worktrees. Returns human-readable
    descriptions of what was removed.

    Removes a registered worktree when no live (non-removed) node claims
    its path, or when its branch ref is gone. Removes directories under
    .skein/worktrees that are not registered git worktrees at all.
    The main repo working tree is never touched.
    """
    removed: List[str] = []
    for wpath, reason in find_orphaned_worktrees(repo_root):
        if reason.startswith("stale directory"):
            shutil.rmtree(wpath, ignore_errors=True)
            removed.append(f"removed stale directory {wpath}")
            continue
        r = _git(repo_root, "worktree", "remove", "--force", wpath)
        if r.returncode == 0:
            removed.append(f"removed worktree {wpath} ({reason})")
        else:
            removed.append(f"could not remove worktree {wpath}: "
                           f"{r.stderr.strip()[:200]}")
    _git(repo_root, "worktree", "prune")
    return removed


def _numstat(repo_root: str | Path, base_commit: str,
             result_commit: str) -> Dict[str, Dict[str, int]]:
    """Recompute per-file added/deleted line counts between two commits.

    Same shape as the diff_stats recorded by the supervisor's result
    checkpoint: {path: {"added": n, "deleted": m}}.
    """
    if not base_commit or not result_commit or base_commit == result_commit:
        return {}
    r = _git(repo_root, "diff", "--numstat", base_commit, result_commit)
    stats: Dict[str, Dict[str, int]] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                stats[parts[2]] = {"added": int(parts[0]),
                                   "deleted": int(parts[1])}
            except ValueError:
                pass
    return stats


def verify_result_record(repo_root: str | Path, node_id: str) -> List[str]:
    """Check a node's result record against the object store.

    Returns a list of problems; an empty list means the record verifies.
    Raises WorktreeError for an unknown node id. A node with no result
    record is a problem, not an empty pass.
    """
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise WorktreeError(f"unknown node '{node_id}'")
    result = node.get("result") or {}
    base = result.get("base_commit")
    commit = result.get("commit")
    problems: List[str] = []
    if not result or not commit:
        return [f"node '{node_id}' has no result record"]
    if _git(repo_root, "cat-file", "-e", commit).returncode != 0:
        problems.append(f"result commit {commit[:8]} is missing from the "
                        f"object store")
    base_ok = bool(base) and _git(repo_root, "cat-file", "-e",
                                  base + "^{commit}").returncode == 0
    if base and not base_ok:
        problems.append(f"base commit {base[:8]} is missing from the "
                        f"object store")
    if base_ok and _git(repo_root, "cat-file", "-e",
                        commit).returncode == 0:
        if _git(repo_root, "merge-base", "--is-ancestor", base,
                commit).returncode != 0:
            problems.append(f"result commit {commit[:8]} is not a descendant "
                            f"of base commit {base[:8]}")
        recomputed = _numstat(repo_root, base, commit)
        if recomputed != (result.get("diff_stats") or {}):
            problems.append("recorded diff stats do not match the "
                            "recomputed diff between base and result")
    return problems
