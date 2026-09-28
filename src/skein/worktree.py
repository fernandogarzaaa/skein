"""Worktree + branching strategy for Skein.

Core invariant: a node marked done produces a durable git result commit.
Downstream nodes branch from the dependency's *result commit*, never from
a branch name that may not contain the parent's actual changes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import graph as g
from . import ids


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
            return result_commit
        if d.get("worktree", {}).get("branch"):
            return d["worktree"]["branch"]
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


def _registered_worktrees(repo_root: str | Path) -> Dict[str, Dict[str, str]]:
    """Parse `git worktree list --porcelain` into {abspath: {branch, head}}."""
    r = _git(repo_root, "worktree", "list", "--porcelain")
    out: Dict[str, Dict[str, str]] = {}
    cur: Dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        if line.startswith("worktree "):
            if cur.get("path"):
                out[cur["path"]] = cur
            cur = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref.split("/")[-1] if "/" in ref else ref
            # keep full ref too for skein/ namespaced branches
            cur["branch_ref"] = ref
        elif line == "" and cur.get("path"):
            out[cur["path"]] = cur
            cur = {}
    if cur.get("path"):
        out[cur["path"]] = cur
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
    branch = node.get("worktree", {}).get("branch") or branch_for(node_id)
    base = node.get("worktree", {}).get("base_branch") or base_branch_for(repo_root, node_id, nodes)
    path = Path(node.get("worktree", {}).get("path") or str(worktree_path_for(repo_root, node_id)))

    # The base ref must exist; never substitute the current branch silently.
    _resolve_ref(repo_root, base)

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
        g.append_event(repo_root, actor, "node_edited", node_id,
                       {"worktree": {"branch": branch, "base_branch": base, "path": str(path)}})
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
                   {"worktree": {"branch": branch, "base_branch": base, "path": str(path)}})
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
    result_commit = _merge_in_temp(repo_root, integ_id, dep_refs,
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
    c.release_node(repo_root, integ_id, actor=actor, claim_token=token,
                   note="auto-merge conflicted; needs an agent to resolve")
    return False


def _merge_in_temp(repo_root: str | Path, integ_id: str,
                   dep_refs: List[str], integ_branch: str) -> Optional[str]:
    """Merge parent refs into the integration branch inside a throwaway
    worktree (never touches the user's checkout). Returns the merge
    commit SHA on success, None on conflict."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skein-integrate-"))
    try:
        r = subprocess.run(["git", "worktree", "add", str(tmp), integ_branch],
                           cwd=str(repo_root), capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for other in dep_refs[1:]:
            m = subprocess.run(["git", "merge", "--no-ff", "--no-edit", other],
                               cwd=str(tmp), capture_output=True, text=True)
            if m.returncode != 0:
                subprocess.run(["git", "merge", "--abort"], cwd=str(tmp),
                               capture_output=True)
                subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                               cwd=str(repo_root), capture_output=True)
                return None
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                       cwd=str(repo_root), capture_output=True)
        return _git(repo_root, "rev-parse", integ_branch).stdout.strip()
    finally:
        subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root),
                       capture_output=True)


def remove_worktree(repo_root: str | Path, node_id: str, delete_branch: bool = False) -> bool:
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    path = (node or {}).get("worktree", {}).get("path")
    branch = (node or {}).get("worktree", {}).get("branch")
    if path:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                       cwd=str(repo_root), capture_output=True)
    subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root), capture_output=True)
    if delete_branch and branch:
        subprocess.run(["git", "branch", "-D", branch], cwd=str(repo_root),
                       capture_output=True)
    return True
