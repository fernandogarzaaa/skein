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


def _check_backend(backend: str, repo_root: Any) -> str:
    from .adapters.profiles import get_profile
    return get_profile((backend or "claude_code").strip(), repo_root).name


def _check_blast_radius(blast_radius: Optional[List[str]]) -> List[str]:
    out = []
    for pat in blast_radius or []:
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


def add_node(repo_root: Any, actor: str, node_id: str, *,
             title: str = "",
             goal: str = "", context: str = "", constraints: str = "",
             completion: str = "",
             depends_on: Optional[List[str]] = None,
             blast_radius: Optional[List[str]] = None,
             backend: str = "claude_code",
             backend_config: Optional[Dict[str, str]] = None,
             change_policy: str = "warn") -> Dict[str, Any]:
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    if node_id in nodes:
        raise ValueError(f"node '{node_id}' already exists")
    depends_on = list(depends_on or [])
    for dep in depends_on:
        ids.validate_node_id(dep)
        if dep not in nodes:
            raise ValueError(f"dependency '{dep}' does not exist")
    _check_no_cycles(nodes, node_id, depends_on)
    backend_name = _check_backend(backend, repo_root)
    _check_change_policy(change_policy)
    g.append_event(repo_root, actor, "node_added", node_id, {
        "title": title or "",
        "intent": {"goal": goal or "", "context": context or "",
                   "constraints": constraints or "",
                   "completion": completion or ""},
        "depends_on": depends_on,
        "blast_radius": _check_blast_radius(blast_radius),
        "change_policy": change_policy,
        "backend": backend_name,
        "backend_config": dict(backend_config or {}),
    })
    return g.load_graph(repo_root)[node_id]


def edit_node(repo_root: Any, actor: str, node_id: str,
              fields: Dict[str, Any], delete: bool = False) -> str:
    """Apply an edit. Returns 'edited', 'interrupt', 'removed' or
    'interrupt_delete'. Raises ValueError on unknown node / bad status /
    unknown backend / empty edit / illegal transition."""
    ids.validate_node_id(node_id)
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    fields = dict(fields)
    if "status" in fields:
        _check_status_transition(fields["status"])
    if "depends_on" in fields:
        depends_on = list(fields["depends_on"] or [])
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
