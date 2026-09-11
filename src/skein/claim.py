"""Claim protocol: CAS claiming, eligibility, TTL/heartbeat, reaper."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import graph as g


class ClaimError(Exception):
    pass


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _glob_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    # fnmatch in both directions catches file-vs-glob overlaps
    try:
        if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
            return True
    except Exception:
        pass
    try:
        if PurePosixPath(a).match(b) or PurePosixPath(b).match(a):
            return True
    except Exception:
        pass
    # static-prefix overlap: e.g. "src/auth/**" vs "src/auth/login.py"
    def static_prefix(glob: str) -> str:
        parts = glob.replace("\\", "/").split("/")
        out = []
        for p in parts:
            if any(c in p for c in ("*", "?", "[")):
                break
            out.append(p)
        return "/".join(out)
    pa, pb = static_prefix(a), static_prefix(b)
    if pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")):
        return True
    return False


def blast_overlap(r1: List[str], r2: List[str]) -> Optional[Tuple[str, str]]:
    for a in r1 or []:
        for b in r2 or []:
            if _glob_overlap(a, b):
                return (a, b)
    return None


def active_holders(nodes: Dict[str, Dict]) -> List[Dict]:
    return [n for n in nodes.values()
            if not n.get("removed") and n.get("status") in ("claimed", "in_progress")
            and n.get("claim", {}).get("holder")]


def eligibility(repo_root: str | Path, node_id: str) -> Tuple[bool, str]:
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        return False, f"unknown node '{node_id}'"
    if node.get("removed"):
        return False, f"node '{node_id}' has been removed"
    if node["status"] != "unclaimed":
        return False, f"node '{node_id}' status is '{node['status']}', expected 'unclaimed'"
    for dep in node.get("depends_on", []):
        d = nodes.get(dep)
        if d is None or d.get("removed"):
            return False, f"dependency '{dep}' is missing/removed"
        if d["status"] != "done":
            return False, f"dependency '{dep}' is '{d['status']}', must be 'done'"
    for other in active_holders(nodes):
        if other["id"] == node_id:
            continue
        hit = blast_overlap(node.get("blast_radius", []), other.get("blast_radius", []))
        if hit:
            return False, (
                f"blast-radius overlap with claimed node '{other['id']}' "
                f"(holder={other['claim']['holder']}): '{hit[0]}' vs '{hit[1]}'"
            )
    return True, "eligible"


def is_expired(node: Dict, at: Optional[datetime] = None) -> bool:
    claim = node.get("claim") or {}
    last = parse_ts(claim.get("last_heartbeat") or claim.get("claimed_at"))
    ttl = claim.get("ttl_seconds")
    if not last or not ttl:
        return False
    at = at or now_utc()
    return (at - last).total_seconds() > float(ttl)


def claim_node(repo_root: str | Path, node_id: str, holder: str,
               ttl_seconds: Optional[int] = None,
               expected_version: Optional[int] = None,
               actor: Optional[str] = None,
               max_retries: int = 3) -> Dict:
    """Compare-and-swap claim. Rejects write if version moved (expected_version).

    Without expected_version, performs a fresh read-check-append; retries on
    conflict up to max_retries (conflicts arise under concurrent appends).
    """
    from . import worktree as wt
    cfg = g.load_config(repo_root)
    ttl = ttl_seconds if ttl_seconds is not None else int(cfg.get("default_ttl_seconds", 1800))
    actor = actor or holder
    attempt = 0
    while True:
        nodes = g.load_graph(repo_root)
        node = nodes.get(node_id)
        if node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        if expected_version is not None and node["version"] != expected_version:
            raise ClaimError(
                f"version conflict on '{node_id}': expected {expected_version}, "
                f"found {node['version']}"
            )
        ok, reason = eligibility(repo_root, node_id)
        if not ok:
            raise ClaimError(reason)
        # re-read events to detect a concurrent append racing us
        fresh = g.rebuild_graph(repo_root)
        fresh_node = fresh.get(node_id)
        if fresh_node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        if expected_version is not None:
            if fresh_node["version"] != expected_version:
                raise ClaimError(
                    f"version conflict on '{node_id}': expected {expected_version}, "
                    f"found {fresh_node['version']}"
                )
        elif fresh_node["version"] != node["version"]:
            attempt += 1
            if attempt >= max_retries:
                raise ClaimError(f"concurrent modification on '{node_id}', retry failed")
            continue
        # determine base branch for bookkeeping (worktree created in run path)
        base = wt.base_branch_for(repo_root, node_id, fresh)
        ev = g.append_event(
            repo_root, actor, "claimed", node_id,
            {"holder": holder, "ttl_seconds": ttl,
             "worktree": {"base_branch": base}},
        )
        return g.load_graph(repo_root)[node_id]


def heartbeat(repo_root: str | Path, node_id: str, holder: str) -> Dict:
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ClaimError(f"unknown node '{node_id}'")
    claim = node.get("claim") or {}
    if claim.get("holder") != holder:
        raise ClaimError(f"node '{node_id}' held by '{claim.get('holder')}', not '{holder}'")
    if node["status"] not in ("claimed", "in_progress"):
        raise ClaimError(f"node '{node_id}' status '{node['status']}' cannot heartbeat")
    g.append_event(repo_root, holder, "heartbeat", node_id, {})
    return g.load_graph(repo_root)[node_id]


def release_node(repo_root: str | Path, node_id: str, actor: str,
                 force: bool = False, note: str = "") -> Dict:
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ClaimError(f"unknown node '{node_id}'")
    if node["status"] not in ("claimed", "in_progress", "needs_human", "failed", "blocked"):
        raise ClaimError(f"node '{node_id}' status '{node['status']}' is not releasable")
    if not force and node["status"] in ("claimed", "in_progress"):
        holder = (node.get("claim") or {}).get("holder")
        if holder and holder != actor:
            raise ClaimError(
                f"node '{node_id}' held by '{holder}'; use --force for explicit force-release"
            )
    payload = {"note": note or (f"force-released by {actor}" if force else f"released by {actor}"),
               "forced": force}
    g.append_event(repo_root, actor, "released", node_id, payload)
    return g.load_graph(repo_root)[node_id]


def reap_expired(repo_root: str | Path, actor: str = "reaper",
                 at: Optional[datetime] = None) -> List[str]:
    """Release expired leases. Returns list of released node ids."""
    at = at or now_utc()
    released = []
    nodes = g.load_graph(repo_root)
    for nid, node in list(nodes.items()):
        if node.get("status") in ("claimed", "in_progress") and is_expired(node, at):
            holder = (node.get("claim") or {}).get("holder")
            g.append_event(repo_root, actor, "released", nid, {
                "note": f"lease expired (holder={holder}); previous attempt did not complete",
                "expired": True,
            })
            released.append(nid)
    return released
