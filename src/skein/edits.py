"""Shared node mutations: one ruleset for the CLI and the web API.

Both surfaces must treat claimed nodes identically (edit/delete ->
human_interrupt, never silent), so the logic lives here, not in two
places. Every mutation is validated centrally: node-ID syntax,
dependency existence, no self-dependency, no cycles, legal status
transitions, valid backend, and valid change policy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import graph as g
from . import ids
from . import worktree as wt


def _check_backend(backend: str, repo_root: Any) -> str:
    from .adapters.profiles import get_profile
    return get_profile((backend or "claude_code").strip(), repo_root).name


def _check_str_list(value: Any, field: str) -> List[str]:
    """A list of strings, or ValueError. A bare string is rejected rather
    than iterated character by character (the web API sends raw JSON)."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} must be a list of strings")
    return list(value)


def _check_backend_config(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("backend_config must be an object of key=value strings")
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("backend_config keys must be non-empty strings")
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            raise ValueError(f"backend_config['{k}'] must be a string")
        out[k] = str(v)
    return out


def _check_blast_radius(blast_radius: Optional[List[str]]) -> List[str]:
    out = []
    for pat in _check_str_list(blast_radius, "blast_radius"):
        if not isinstance(pat, str) or not pat.strip():
            raise ValueError("blast-radius patterns must be non-empty strings")
        if any(ord(ch) < 32 for ch in pat):
            raise ValueError(f"blast-radius pattern {pat!r} contains control characters")
        out.append(pat)
    return out


def _check_no_cycles(nodes: Dict[str, Dict[str, Any]], node_id: str,
                     depends_on: List[str]) -> None:
    """Reject self-dependencies and dependency cycles for the proposed edge set."""
    if node_id in depends_on:
        raise ValueError(f"node '{node_id}' cannot depend on itself")
    # DFS from node_id following proposed deps; a revisit means a cycle.
    deps_map = {nid: list(n.get("depends_on", [])) for nid, n in nodes.items()}
    deps_map[node_id] = list(depends_on)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in deps_map}

    def visit(nid: str, stack: List[str]) -> None:
        color[nid] = GRAY
        for dep in deps_map.get(nid, []):
            if dep not in deps_map:
                continue  # missing deps are rejected separately
            if color[dep] == GRAY:
                cycle = " -> ".join(stack + [dep])
                raise ValueError(f"dependency cycle detected: {cycle}")
            if color[dep] == WHITE:
                visit(dep, stack + [dep])
        color[nid] = BLACK

    visit(node_id, [node_id])


def _check_status_transition(status: str) -> None:
    if status not in g.VALID_STATUSES:
        raise ValueError(f"invalid status '{status}'")
    if status not in g.MANUAL_STATUSES:
        raise ValueError(
            f"status '{status}' cannot be set by direct edit: "
            f"'done'/'failed' come only from verified runs, "
            f"'claimed'/'in_progress' only from claim")


def _check_change_policy(policy: str) -> str:
    if policy not in g.CHANGE_POLICIES:
        raise ValueError(
            f"invalid change_policy '{policy}' "
            f"(choices: {', '.join(sorted(g.CHANGE_POLICIES))})")
    return policy


def _check_max_retries(value) -> int:
    try:
        v = int(value)
        integral = float(value) == v
    except (TypeError, ValueError):
        integral = False
        v = -1
    if not integral or v < 0:
        raise ValueError(
            f"invalid max_retries {value!r}: expected a non-negative integer "
            f"(0 = no retries)")
    return v


def _check_retry_backoff(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"invalid retry_backoff_seconds {value!r}: expected a "
            f"non-negative number of seconds")
    if v < 0:
        raise ValueError(
            f"invalid retry_backoff_seconds {value!r}: expected a "
            f"non-negative number of seconds")
    return v


def add_node(repo_root: Any, actor: str, node_id: str, *,
             title: str = "",
             goal: str = "", context: str = "", constraints: str = "",
             completion: str = "",
             depends_on: Optional[List[str]] = None,
             blast_radius: Optional[List[str]] = None,
             backend: str = "claude_code",
             backend_config: Optional[Dict[str, str]] = None,
             change_policy: str = "warn",
             max_retries: Optional[int] = None,
             retry_backoff_seconds: Optional[float] = None) -> Dict[str, Any]:
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    if node_id in nodes:
        raise ValueError(f"node '{node_id}' already exists")
    depends_on = _check_str_list(depends_on, "depends_on")
    backend_config = _check_backend_config(backend_config)
    for dep in depends_on:
        ids.validate_node_id(dep)
        if dep not in nodes:
            raise ValueError(f"dependency '{dep}' does not exist")
    _check_no_cycles(nodes, node_id, depends_on)
    backend_name = _check_backend(backend, repo_root)
    _check_change_policy(change_policy)
    # retry policy defaults come from repo config; explicit values win
    cfg = g.load_config(repo_root)
    if max_retries is None:
        max_retries = cfg.get("default_max_retries", g.DEFAULT_MAX_RETRIES)
    if retry_backoff_seconds is None:
        retry_backoff_seconds = cfg.get("default_retry_backoff_seconds",
                                        g.DEFAULT_RETRY_BACKOFF_SECONDS)
    max_retries = _check_max_retries(max_retries)
    retry_backoff_seconds = _check_retry_backoff(retry_backoff_seconds)
    g.append_event(repo_root, actor, "node_added", node_id, {
        "title": title or "",
        "intent": {"goal": goal or "", "context": context or "",
                   "constraints": constraints or "",
                   "completion": completion or ""},
        "depends_on": depends_on,
        "blast_radius": _check_blast_radius(blast_radius),
        "change_policy": change_policy,
        "backend": backend_name,
        "backend_config": backend_config,
        "max_retries": max_retries,
        "retry_backoff_seconds": retry_backoff_seconds,
    })
    return g.load_graph(repo_root)[node_id]


def edit_node(repo_root: Any, actor: str, node_id: str,
              fields: Dict[str, Any], delete: bool = False,
              delete_branch: bool = False) -> str:
    """Apply an edit. Returns 'edited', 'interrupt', 'removed' or
    'interrupt_delete'. Raises ValueError on unknown node / bad status /
    unknown backend / empty edit / illegal transition.

    Deleting a node also removes its worktree (branch kept unless
    delete_branch is set), so the CLI and the web canvas share one rule.
    """
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    fields = dict(fields)
    if "status" in fields and fields["status"] == node.get("status"):
        # Re-submitting the current status (the canvas form sends every
        # field) is not a transition; dropping it keeps "save title" on
        # a done/claimed node from failing the manual-status check.
        del fields["status"]
    if "status" in fields:
        _check_status_transition(fields["status"])
    if "title" in fields and not isinstance(fields["title"], str):
        raise ValueError("title must be a string")
    if "intent" in fields:
        intent = fields["intent"]
        if not isinstance(intent, dict) or any(
                not isinstance(v, str) for v in intent.values()):
            raise ValueError("intent must be an object of string fields")
    if "backend_config" in fields:
        fields["backend_config"] = _check_backend_config(fields["backend_config"])
    if "depends_on" in fields:
        depends_on = _check_str_list(fields["depends_on"], "depends_on")
        for dep in depends_on:
            ids.validate_node_id(dep)
            if dep not in nodes:
                raise ValueError(f"dependency '{dep}' does not exist")
        _check_no_cycles(nodes, node_id, depends_on)
        fields["depends_on"] = depends_on
    if "blast_radius" in fields:
        fields["blast_radius"] = _check_blast_radius(fields.get("blast_radius"))
    if "change_policy" in fields:
        fields["change_policy"] = _check_change_policy(fields["change_policy"])
    if "max_retries" in fields:
        fields["max_retries"] = _check_max_retries(fields["max_retries"])
    if "retry_backoff_seconds" in fields:
        fields["retry_backoff_seconds"] = _check_retry_backoff(
            fields["retry_backoff_seconds"])
    if "backend" in fields:
        fields["backend"] = _check_backend(fields["backend"], repo_root)
    if delete:
        # delete of a claimed node is a human_interrupt, never silent
        if node["status"] in ("claimed", "in_progress"):
            g.append_event(repo_root, actor, "human_interrupt", node_id,
                           {"action": "delete",
                            "reason": "human deleted claimed node"})
            return "interrupt_delete"
        # refuse to delete a node that still has dependents, unless the
        # dependents are gone too - dangling dependents would block forever
        dependents = [nid for nid, n in nodes.items()
                      if not n.get("removed") and node_id in n.get("depends_on", [])]
        if dependents:
            raise ValueError(
                f"cannot delete '{node_id}': still depended on by "
                f"{', '.join(sorted(dependents))}")
        g.append_event(repo_root, actor, "node_removed", node_id, {})
        # Shared rule: a deleted node must not leave a worktree behind.
        # The node_removed event is already appended, so the node still
        # carries its worktree record for remove_worktree to find.
        wt.remove_worktree(repo_root, node_id, delete_branch=delete_branch)
        return "removed"
    if not fields:
        raise ValueError("nothing to edit")
    # human edit targeting a claimed node -> human_interrupt (supervisor aborts)
    if node["status"] in ("claimed", "in_progress"):
        g.append_event(repo_root, actor, "human_interrupt", node_id,
                       {"action": "edit", "fields": fields,
                        "reason": "human edited claimed node"})
        return "interrupt"
    g.append_event(repo_root, actor, "node_edited", node_id, fields)
    return "edited"
