"""Event log, node/edge model, and reduction for Skein v0.1."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SKEIN_DIR = ".skein"
LOG_NAME = "log.ndjson"
GRAPH_NAME = "graph.json"
CONFIG_NAME = "config.json"

VALID_STATUSES = {
    "unclaimed", "claimed", "in_progress", "blocked",
    "needs_human", "done", "failed",
}

VALID_EVENT_TYPES = {
    "node_added", "node_edited", "node_removed",
    "claimed", "heartbeat", "released",
    "completed", "failed", "human_interrupt",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def skein_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / SKEIN_DIR


def log_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / LOG_NAME


def graph_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / GRAPH_NAME


def config_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / CONFIG_NAME


def default_config() -> Dict[str, Any]:
    return {"default_ttl_seconds": 1800, "heartbeat_interval_seconds": 60}


def load_config(repo_root: str | Path) -> Dict[str, Any]:
    p = config_path(repo_root)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default_config()


def new_node(node_id: str, title: str = "", intent: Optional[Dict[str, str]] = None,
             depends_on: Optional[List[str]] = None,
             blast_radius: Optional[List[str]] = None,
             backend: str = "claude_code",
             backend_config: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "id": node_id,
        "title": title,
        "status": "unclaimed",
        "intent": intent or {"goal": "", "context": "", "constraints": "", "completion": ""},
        "depends_on": depends_on or [],
        "blast_radius": blast_radius or [],
        "backend": backend or "claude_code",
        "backend_config": backend_config or {},
        "claim": {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None},
        "worktree": {"branch": None, "base_branch": None, "path": None},
        "handoff_note": None,
        "evidence": [],
        "version": 0,
        "removed": False,
    }


def load_events(repo_root: str | Path) -> List[Dict[str, Any]]:
    p = log_path(repo_root)
    if not p.exists():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def reduce_events(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Reduce event log to current node state.

    Out-of-order-safe for concurrent appends: sort by timestamp
    (last-write-wins by event timestamp). Ties broken by file order.
    """
    ordered = sorted(
        enumerate(events),
        key=lambda pair: (pair[1].get("timestamp", ""), pair[0]),
    )
    nodes: Dict[str, Dict[str, Any]] = {}
    for _, ev in ordered:
        apply_event(nodes, ev)
    return nodes


def apply_event(nodes: Dict[str, Dict[str, Any]], ev: Dict[str, Any]) -> None:
    etype = ev.get("type")
    nid = ev.get("node_id")
    payload = ev.get("payload") or {}
    if etype == "node_added":
        if nid in nodes and not nodes[nid].get("removed"):
            # last-write-wins: re-add overwrites base fields but keep version bump
            pass
        node = new_node(
            nid,
            title=payload.get("title", ""),
            intent=payload.get("intent"),
            depends_on=payload.get("depends_on"),
            blast_radius=payload.get("blast_radius"),
            backend=payload.get("backend", "claude_code"),
            backend_config=payload.get("backend_config"),
        )
        node["version"] = (nodes[nid]["version"] + 1) if nid in nodes else 1
        nodes[nid] = node
    elif etype == "node_edited":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        for key in ("title", "depends_on", "blast_radius", "handoff_note", "status",
                      "backend", "backend_config"):
            if key in payload:
                node[key] = deepcopy(payload[key])
        if "intent" in payload and isinstance(payload["intent"], dict):
            for k, v in payload["intent"].items():
                node["intent"][k] = v
        if "worktree" in payload and isinstance(payload["worktree"], dict):
            for k, v in payload["worktree"].items():
                node["worktree"][k] = v
        node["version"] += 1
    elif etype == "node_removed":
        node = nodes.get(nid)
        if node is None:
            return
        node["removed"] = True
        node["version"] += 1
    elif etype == "claimed":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["status"] = "claimed"
        node["claim"] = {
            "holder": payload.get("holder"),
            "claimed_at": ev.get("timestamp"),
            "ttl_seconds": payload.get("ttl_seconds"),
            "last_heartbeat": ev.get("timestamp"),
        }
        if payload.get("worktree"):
            for k, v in payload["worktree"].items():
                node["worktree"][k] = v
        node["version"] += 1
    elif etype == "heartbeat":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["claim"]["last_heartbeat"] = ev.get("timestamp")
        if node["status"] == "claimed":
            node["status"] = "in_progress"
        node["version"] += 1
    elif etype == "released":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["status"] = "unclaimed"
        note = payload.get("note")
        if note:
            # stash previous-attempt context on the node for next claimant
            node["handoff_note"] = (node.get("handoff_note") or "") + (
                ("\n" if node.get("handoff_note") else "") + f"[release note] {note}"
            ) if not payload.get("keep_handoff") else node.get("handoff_note")
            if payload.get("keep_handoff"):
                pass
        node["claim"] = {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}
        node["version"] += 1
    elif etype == "completed":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["status"] = "done"
        if "handoff_note" in payload:
            node["handoff_note"] = payload["handoff_note"]
        if "evidence" in payload:
            node["evidence"] = deepcopy(payload["evidence"])
        if "worktree" in payload and isinstance(payload["worktree"], dict):
            for k, v in payload["worktree"].items():
                node["worktree"][k] = v
        node["claim"] = {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}
        node["version"] += 1
    elif etype == "failed":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["status"] = "failed"
        if "evidence" in payload:
            node["evidence"] = deepcopy(payload["evidence"])
        if "error" in payload and payload["error"]:
            node["handoff_note"] = payload["error"]
        node["claim"] = {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}
        node["version"] += 1
    elif etype == "human_interrupt":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        action = payload.get("action", "edit")
        if action == "delete":
            node["removed"] = True
        elif action in ("edit", "reassign"):
            # edits carried in payload.fields
            fields = payload.get("fields") or {}
            for key in ("title", "depends_on", "blast_radius", "status",
                          "backend", "backend_config"):
                if key in fields:
                    node[key] = deepcopy(fields[key])
            if "intent" in fields and isinstance(fields["intent"], dict):
                for k, v in fields["intent"].items():
                    node["intent"][k] = v
            # an interrupt targeting a claimed node parks it for human review
            if node["status"] in ("claimed", "in_progress"):
                node["status"] = "needs_human"
                node["claim"] = {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}
        else:
            if node["status"] in ("claimed", "in_progress"):
                node["status"] = "needs_human"
                node["claim"] = {"holder": None, "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}
        node["version"] += 1


def append_event(repo_root: str | Path, actor: str, type: str, node_id: str,
                 payload: Optional[Dict[str, Any]] = None,
                 timestamp: Optional[str] = None,
                 commit: bool = True) -> Dict[str, Any]:
    if type not in VALID_EVENT_TYPES:
        raise ValueError(f"unknown event type: {type}")
    ev = {
        "timestamp": timestamp or utcnow_iso(),
        "actor": actor,
        "type": type,
        "node_id": node_id,
        "payload": payload or {},
    }
    lp = log_path(repo_root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    rebuild_graph(repo_root)
    if commit:
        git_commit_log(repo_root, f"skein: {type} {node_id} by {actor}")
    return ev


def rebuild_graph(repo_root: str | Path) -> Dict[str, Dict[str, Any]]:
    events = load_events(repo_root)
    nodes = reduce_events(events)
    gp = graph_path(repo_root)
    gp.parent.mkdir(parents=True, exist_ok=True)
    # graph.json is derived; never hand-edited (written only here)
    visible = {nid: n for nid, n in nodes.items() if not n.get("removed")}
    gp.write_text(json.dumps(visible, indent=2, sort_keys=True), encoding="utf-8")
    return visible


def load_graph(repo_root: str | Path) -> Dict[str, Dict[str, Any]]:
    gp = graph_path(repo_root)
    if gp.exists():
        try:
            return json.loads(gp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return rebuild_graph(repo_root)


def get_node(repo_root: str | Path, node_id: str) -> Optional[Dict[str, Any]]:
    return load_graph(repo_root).get(node_id)


def git_commit_log(repo_root: str | Path, message: str) -> bool:
    """Commit .skein log + derived graph. Returns True if committed."""
    try:
        subprocess.run(["git", "add", SKEIN_DIR], cwd=str(repo_root),
                       capture_output=True, check=False)
        # Scope the commit to .skein: a bare `git commit` would sweep in any
        # unrelated user-staged files (observed in a live walkthrough).
        r = subprocess.run(["git", "commit", "-m", message, "--", SKEIN_DIR],
                           cwd=str(repo_root), capture_output=True, check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False
