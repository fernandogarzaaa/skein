"""Worktree + branching strategy for Skein v0.1."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import graph as g


def _git(repo_root: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=str(repo_root),
                          capture_output=True, text=True)


def current_branch(repo_root: str | Path) -> str:
    r = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode == 0:
        return r.stdout.strip()
    return "main"


def base_branch_for(repo_root: str | Path, node_id: str,
                    nodes: Optional[Dict[str, Dict]] = None) -> str:
    nodes = nodes if nodes is not None else g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        return current_branch(repo_root)
    deps = node.get("depends_on", [])
    if len(deps) == 0:
        return current_branch(repo_root)
    if len(deps) == 1:
        d = nodes.get(deps[0])
        if d is not None and d.get("worktree", {}).get("branch"):
            return d["worktree"]["branch"]
        return current_branch(repo_root)
    # multi-dependency: base is the integration branch
    return f"skein/{node_id}__integrate-base"


def branch_for(node_id: str) -> str:
    return f"skein/{node_id}"


def worktree_path_for(repo_root: str | Path, node_id: str) -> Path:
    return Path(repo_root) / ".skein" / "worktrees" / node_id


def ensure_worktree(repo_root: str | Path, node_id: str,
                    actor: str = "supervisor") -> Tuple[Path, str, str]:
    """Create (or reuse) the node's worktree branched from the right base.

    Returns (path, branch, base_branch). Records branch info on the node via
    node_edited event (no status change).
    """
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    branch = node.get("worktree", {}).get("branch") or branch_for(node_id)
    base = node.get("worktree", {}).get("base_branch") or base_branch_for(repo_root, node_id, nodes)
    path = Path(node.get("worktree", {}).get("path") or str(worktree_path_for(repo_root, node_id)))

    # make sure base ref exists locally
    _git(repo_root, "fetch", "--all", "--quiet")
    if _git(repo_root, "rev-parse", "--verify", base).returncode != 0:
        base = current_branch(repo_root)

    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists() and not path.exists():
        pass
    existing = _git(repo_root, "worktree", "list", "--porcelain").stdout
    if str(path) in existing or path.exists():
        # already present; ensure branch checked out
        g.append_event(repo_root, actor, "node_edited", node_id,
                       {"worktree": {"branch": branch, "base_branch": base, "path": str(path)}})
        return path, branch, base
    # create branch if missing, from base
    if _git(repo_root, "rev-parse", "--verify", branch).returncode != 0:
        r = _git(repo_root, "branch", branch, base)
        if r.returncode != 0:
            raise RuntimeError(f"cannot create branch {branch} from {base}: {r.stderr}")
    r = _git(repo_root, "worktree", "add", str(path), branch)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr}")
    g.append_event(repo_root, actor, "node_edited", node_id,
                   {"worktree": {"branch": branch, "base_branch": base, "path": str(path)}})
    return path, branch, base


def ensure_integration_node(repo_root: str | Path, node_id: str,
                            actor: str = "supervisor") -> Optional[str]:
    """For a multi-dependency node, auto-create an integration node.

    Returns the integration node id, or None if not needed.
    The integration node merges parent branches into one base branch;
    a deterministic merge is attempted first, else the node stays
    claimable via the normal claim mechanism.
    """
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    deps = node.get("depends_on", [])
    if len(deps) < 2:
        return None
    integ_id = f"{node_id}__integrate"
    if integ_id in nodes and not nodes[integ_id].get("removed"):
        return integ_id
    parents = [nodes[d]["worktree"]["branch"] for d in deps
               if d in nodes and nodes[d].get("worktree", {}).get("branch")]
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
    })
    # point the child at the integration base
    g.append_event(repo_root, actor, "node_edited", node_id,
                   {"worktree": {"base_branch": f"skein/{integ_id}-base"}})
    auto_merge_parents(repo_root, integ_id, actor=actor)
    return integ_id


def auto_merge_parents(repo_root: str | Path, integ_id: str,
                       actor: str = "supervisor") -> bool:
    """Attempt deterministic merge of integration node's parent branches.

    Returns True if merged without agent help. On success the integration
    node is marked done with evidence; on conflict it stays unclaimed for
    normal claiming.
    """
    from . import verify as v
    nodes = g.load_graph(repo_root)
    node = nodes.get(integ_id)
    if node is None:
        return False
    deps = node.get("depends_on", [])
    if not deps:
        return False
    dep_branches = []
    for d in deps:
        dd = nodes.get(d)
        if dd is None:
            return False
        b = (dd.get("worktree") or {}).get("branch") or current_branch(repo_root)
        dep_branches.append(b)
    integ_branch = f"skein/{integ_id}-base"
    # reset integration branch from first parent (delete if exists)
    _git(repo_root, "branch", "-D", integ_branch)
    first = dep_branches[0]
    if _git(repo_root, "rev-parse", "--verify", first).returncode != 0:
        first = current_branch(repo_root)
        dep_branches = [first] + dep_branches[1:]
    r = _git(repo_root, "branch", integ_branch, first)
    if r.returncode != 0:
        return False
    # Correct, checkout-safe approach: perform merges in a temp worktree.
    return _auto_merge_in_temp(repo_root, integ_id, dep_branches, integ_branch, actor)


def _auto_merge_in_temp(repo_root: str | Path, integ_id: str,
                        dep_branches: List[str], integ_branch: str,
                        actor: str) -> bool:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skein-integrate-"))
    try:
        r = subprocess.run(["git", "worktree", "add", str(tmp), integ_branch],
                           cwd=str(repo_root), capture_output=True, text=True)
        if r.returncode != 0:
            return False
        for other in dep_branches[1:]:
            m = subprocess.run(["git", "merge", "--no-ff", "--no-edit", other],
                               cwd=str(tmp), capture_output=True, text=True)
            if m.returncode != 0:
                subprocess.run(["git", "merge", "--abort"], cwd=str(tmp),
                               capture_output=True)
                # leave node claimable for agent/human resolution
                g.append_event(repo_root, actor, "node_edited", integ_id, {
                    "worktree": {"branch": integ_branch,
                                 "base_branch": dep_branches[0],
                                 "path": str(tmp)},
                })
                subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                               cwd=str(repo_root), capture_output=True)
                # re-add as detached record: keep path out; resolution will recreate
                g.append_event(repo_root, actor, "node_edited", integ_id, {
                    "worktree": {"branch": integ_branch,
                                 "base_branch": dep_branches[0],
                                 "path": None},
                })
                return False
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                       cwd=str(repo_root), capture_output=True)
        g.append_event(repo_root, actor, "node_edited", integ_id, {
            "worktree": {"branch": integ_branch, "base_branch": dep_branches[0], "path": None},
        })
        g.append_event(repo_root, actor, "completed", integ_id, {
            "handoff_note": f"Auto-merged {dep_branches} into {integ_branch}.",
            "evidence": [{"command": "git merge " + " ".join(dep_branches[1:]),
                          "exit_code": 0, "output_ref": None}],
            "worktree": {"branch": integ_branch, "base_branch": dep_branches[0]},
        })
        return True
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
