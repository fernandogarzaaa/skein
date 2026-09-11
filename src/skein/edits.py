"""Shared node mutations: one ruleset for the CLI and the web API.

Both surfaces must treat claimed nodes identically (edit/delete ->
human_interrupt, never silent), so the logic lives here, not in two
places.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import graph as g


def _check_backend(backend: str, repo_root: Any) -> str:
    from .adapters.profiles import get_profile
    return get_profile((backend or "claude_code").strip(), repo_root).name


def add_node(repo_root: Any, actor: str, node_id: str, *,
             title: str = "",
             goal: str = "", context: str = "", constraints: str = "",
             completion: str = "",
             depends_on: Optional[List[str]] = None,
             blast_radius: Optional[List[str]] = None,
             backend: str = "claude_code",
             backend_config: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    nodes = g.load_graph(repo_root)
    if node_id in nodes:
        raise ValueError(f"node '{node_id}' already exists")
    depends_on = list(depends_on or [])
    for dep in depends_on:
        if dep not in nodes:
            raise ValueError(f"dependency '{dep}' does not exist")
    backend_name = _check_backend(backend, repo_root)
    g.append_event(repo_root, actor, "node_added", node_id, {
        "title": title or "",
        "intent": {"goal": goal or "", "context": context or "",
                   "constraints": constraints or "",
                   "completion": completion or ""},
        "depends_on": depends_on,
        "blast_radius": list(blast_radius or []),
        "backend": backend_name,
        "backend_config": dict(backend_config or {}),
    })
    return g.load_graph(repo_root)[node_id]


def edit_node(repo_root: Any, actor: str, node_id: str,
              fields: Dict[str, Any], delete: bool = False) -> str:
    """Apply an edit. Returns 'edited', 'interrupt', 'removed' or
    'interrupt_delete'. Raises ValueError on unknown node / bad status /
    unknown backend / empty edit."""
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    fields = dict(fields)
    if "status" in fields and fields["status"] not in g.VALID_STATUSES:
        raise ValueError(f"invalid status '{fields['status']}'")
    if "backend" in fields:
        fields["backend"] = _check_backend(fields["backend"], repo_root)
    if delete:
        # delete of a claimed node is a human_interrupt, never silent
        if node["status"] in ("claimed", "in_progress"):
            g.append_event(repo_root, actor, "human_interrupt", node_id,
                           {"action": "delete",
                            "reason": "human deleted claimed node"})
            return "interrupt_delete"
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
