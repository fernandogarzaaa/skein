"""Event log, node/edge model, and reduction for Skein.

Consistency model (local machine): every state transition is serialized
under the repo-wide control-plane lock (locks.repo_lock), so concurrent
processes on one machine see atomic read-validate-mutate-append-persist
transitions. Multi-machine sync via git remains eventual/advisory:
git alone cannot provide distributed mutual exclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import locks
from . import redact

SKEIN_DIR = ".skein"
LOG_NAME = "log.ndjson"
GRAPH_NAME = "graph.json"
META_NAME = "graph.meta.json"
CONFIG_NAME = "config.json"

# Increment when the event schema changes in a way old readers must detect.
EVENT_SCHEMA_VERSION = 1

VALID_STATUSES = {
    "unclaimed", "claimed", "in_progress", "blocked",
    "needs_human", "done", "failed",
}

VALID_EVENT_TYPES = {
    "node_added", "node_edited", "node_removed",
    "claimed", "heartbeat", "released",
    "completed", "failed", "human_interrupt",
    # shipping lifecycle (Phase 5): informational, not fenced lifecycle
    # transitions, so they bypass lifecycle_transition_error
    "shipped", "release",
    # security audit (Phase 6): redaction hits, sandbox fallbacks,
    # serve auth failures. Informational like shipped/release: no node
    # state is derived, so apply_event ignores them.
    "security",
    # lifecycle rejections (Phase 7): a fenced mutation that raised
    # (stale token, double-claim, invalid transition) is recorded here
    # with the reason, never with secrets. Informational: apply_event
    # only bumps the node's rejected_count, so operators can see
    # contention in `skein log`, `skein status`, and the timeline UI.
    "rejected",
}

# Statuses a human may set directly via node edit / web UI. Terminal and
# worker-owned states are never set by direct edit: done/failed come only
# from the fenced supervisor path, claimed/in_progress only from claim.
MANUAL_STATUSES = {"unclaimed", "blocked", "needs_human"}

CHANGE_POLICIES = {"off", "warn", "strict"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def skein_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / SKEIN_DIR


def log_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / LOG_NAME


def graph_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / GRAPH_NAME


def meta_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / META_NAME


def config_path(repo_root: str | Path) -> Path:
    return skein_dir(repo_root) / CONFIG_NAME


def default_config() -> Dict[str, Any]:
    return {"default_ttl_seconds": 1800, "heartbeat_interval_seconds": 60,
            "default_max_retries": 3, "default_retry_backoff_seconds": 60,
            "default_sandbox": False}


def load_config(repo_root: str | Path) -> Dict[str, Any]:
    p = config_path(repo_root)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default_config()


# Retry policy fallbacks for nodes that predate the retry fields (their
# node_added payloads carry no policy, so reduce cannot consult config).
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 60.0

# Attempt outcomes that consume one retry from the node's policy.
RETRYABLE_OUTCOMES = frozenset({"failed", "timeout"})

# Cap on stored attempt history per node: append-only, but bounded so a
# hot node cannot grow the log-derived graph without limit.
MAX_ATTEMPT_HISTORY = 20


def _coerce_max_retries(value: Any, fallback: int = DEFAULT_MAX_RETRIES) -> int:
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    try:
        if float(value) != v:  # reject 2.5-style fractional input
            return fallback
    except (TypeError, ValueError):
        return fallback
    return v if v >= 0 else fallback


def _coerce_backoff(value: Any,
                    fallback: float = DEFAULT_RETRY_BACKOFF_SECONDS) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return v if v >= 0 else fallback


def new_node(node_id: str, title: str = "", intent: Optional[Dict[str, str]] = None,
             depends_on: Optional[List[str]] = None,
             blast_radius: Optional[List[str]] = None,
             backend: str = "claude_code",
             backend_config: Optional[Dict[str, str]] = None,
             change_policy: str = "warn",
             integration_for: Optional[str] = None,
             max_retries: Optional[int] = None,
             retry_backoff_seconds: Optional[float] = None) -> Dict[str, Any]:
    return {
        "id": node_id,
        "title": title,
        "status": "unclaimed",
        "intent": intent or {"goal": "", "context": "", "constraints": "", "completion": ""},
        "depends_on": depends_on or [],
        "blast_radius": blast_radius or [],
        "change_policy": change_policy if change_policy in CHANGE_POLICIES else "warn",
        "backend": backend or "claude_code",
        "backend_config": backend_config or {},
        # set only on system-created integration nodes: they merge the
        # parents of `integration_for` and are themselves exempt from the
        # integration gate in claim eligibility.
        "integration_for": integration_for,
        # retry policy: max_retries is the number of retries AFTER the
        # initial attempt (0 = no retries). Backoff between attempts is
        # exponential: base * 2**failures_used, with jitter, computed at
        # fail time and recorded on the failed event.
        "max_retries": _coerce_max_retries(max_retries),
        "retry_backoff_seconds": _coerce_backoff(retry_backoff_seconds),
        # set while a failed attempt is waiting out its backoff; cleared
        # on claim and on release
        "retry_at": None,
        # failures consumed so far (informational; the authoritative count
        # is derived from the attempt history below)
        "attempts_used": 0,
        # append-only history of claim -> outcome cycles, bounded to the
        # last MAX_ATTEMPT_HISTORY entries
        "attempts": [],
        "claim": {"holder": None, "attempt_id": None, "claim_token": None,
                  "worker_id": None, "node_version": None, "base_branch": None,
                  "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None},
        "worktree": {"branch": None, "base_branch": None, "path": None},
        "result": None,
        # shipping state, derived from "shipped" events: {target_branch: {
        # result_commit, merge_commit, base_commit, diverged, forced,
        # shipped_at, target_branch}}
        "shipped": {},
        "handoff_note": None,
        "evidence": [],
        # lifecycle rejections recorded against this node (Phase 7);
        # informational only, derived from "rejected" events
        "rejected_count": 0,
        "version": 0,
        "removed": False,
    }


def _valid_event_shape(ev: Any) -> bool:
    """Reject malformed events before reduction so one bad line (e.g. a
    torn write from a crashed append) cannot poison the whole graph."""
    if not isinstance(ev, dict):
        return False
    if ev.get("type") not in VALID_EVENT_TYPES:
        return False
    nid = ev.get("node_id")
    if not isinstance(nid, str) or not nid:
        return False
    payload = ev.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return False
    return True


def parse_event(line: str) -> Optional[Dict[str, Any]]:
    """Parse one NDJSON line; return None for malformed lines instead of
    raising, so callers can skip torn writes from crashed appends."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not _valid_event_shape(ev):
        return None
    return ev


def serialize_event(ev: Dict[str, Any]) -> str:
    return json.dumps(ev, sort_keys=True)


def load_events(repo_root: str | Path) -> List[Dict[str, Any]]:
    """Load events, skipping malformed lines (e.g. a truncated final line
    from a crashed append) instead of failing the whole repository.
    Use load_events_diagnostics() when you need the skip count."""
    events, _ = load_events_diagnostics(repo_root)
    return events


def load_events_diagnostics(repo_root: str | Path) -> Tuple[List[Dict[str, Any]], int]:
    p = log_path(repo_root)
    if not p.exists():
        return [], 0
    events = []
    skipped = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        ev = parse_event(line)
        if ev is None:
            if line.strip():
                skipped += 1
            continue
        events.append(ev)
    return events, skipped


def reduce_events(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Reduce event log to current node state.

    Deterministic ordering for a given event set: sort by timestamp,
    then per-repo seq (true local append order, assigned under the
    control-plane lock), then stable event_id (not file position), so
    two machines that union the same lines converge to the same state.
    Wall-clock timestamps are still the primary order key across
    machines (see README consistency model); seq only breaks
    same-timestamp ties in real append order locally.
    """
    nodes, _ = reduce_events_diagnostics(events)
    return nodes


def reduce_events_diagnostics(
        events: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Reduce, returning (nodes, rejected_lifecycle_count).

    Lifecycle events that fail the fencing/state-machine check are
    rejected here too, not just at append time: a stale worker's
    completion that reaches the log via git sync must not apply.
    """
    ordered = sorted(
        enumerate(events),
        key=lambda pair: (pair[1].get("timestamp", ""),
                          pair[1].get("seq", 0),
                          pair[1].get("event_id", ""),
                          pair[0]),
    )
    nodes: Dict[str, Dict[str, Any]] = {}
    rejected = 0
    for _, ev in ordered:
        if not _valid_event_shape(ev):
            continue
        if ev.get("type") in LIFECYCLE_TYPES and lifecycle_transition_error(nodes, ev):
            rejected += 1
            continue
        apply_event(nodes, ev)
    return nodes, rejected


LIFECYCLE_TYPES = frozenset(
    {"claimed", "heartbeat", "released", "completed", "failed"})


def _lease_expired_at(claim: Dict[str, Any], at_ts: Optional[str]) -> bool:
    """True if the claim's lease had expired at the given event timestamp."""
    last = claim.get("last_heartbeat") or claim.get("claimed_at")
    ttl = claim.get("ttl_seconds")
    if not last or not ttl or not at_ts:
        return False
    try:
        at = datetime.fromisoformat(at_ts)
        last_dt = datetime.fromisoformat(last)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (at - last_dt).total_seconds() > float(ttl)


def lifecycle_transition_error(nodes: Dict[str, Dict[str, Any]],
                               ev: Dict[str, Any]) -> Optional[str]:
    """Return an error string if ev is not a legal fenced lifecycle
    transition against the reduced state, else None.

    This is the state machine's last line of defense: it runs both in
    append_event (local writes raise) and in reduce_events (stale or
    forged events arriving via sync are rejected instead of applied).
    """
    etype = ev.get("type")
    if etype not in LIFECYCLE_TYPES:
        return None
    nid = ev.get("node_id")
    payload = ev.get("payload") or {}
    node = nodes.get(nid)
    if node is None or node.get("removed"):
        return f"lifecycle event '{etype}' for unknown/removed node '{nid}'"
    claim = node.get("claim") or {}
    live_token = claim.get("claim_token")
    live_holder = claim.get("holder")

    if etype == "claimed":
        if node["status"] != "unclaimed":
            return (f"cannot claim '{nid}': status is "
                    f"'{node['status']}', expected 'unclaimed'")
        return None

    # System-level revocations are exempt from fencing: the reaper (lease
    # actually expired) and an explicit operator force-release are the
    # mechanisms that *invalidate* tokens, so they cannot present one.
    if etype == "released" and (payload.get("expired") or payload.get("forced")):
        if payload.get("expired"):
            # Scope the release to the exact attempt the reaper examined:
            # a newer live attempt (e.g. claimed via sync after the reap
            # decision) makes this event stale, never a wipe.
            reaped = payload.get("reaped_attempt_id")
            live_attempt = claim.get("attempt_id")
            if reaped and live_attempt and reaped != live_attempt:
                return (f"reaper release of '{nid}' rejected: targets a "
                        f"superseded attempt; ownership moved on")
            # The reaper's decision time (reaped_as_of) may differ from
            # the event's timestamp; expiry is evaluated at decision time.
            as_of = payload.get("reaped_as_of") or ev.get("timestamp")
            if not _lease_expired_at(claim, as_of):
                return (f"reaper release of '{nid}' rejected: lease was not "
                        f"expired at the reaper's decision time")
        if node["status"] not in ("claimed", "in_progress", "needs_human",
                                  "failed", "blocked"):
            return (f"cannot release '{nid}': status is "
                    f"'{node['status']}'")
        return None

    # Fencing: a token'd attempt is mutated only by its own token.
    if not live_holder:
        return f"'{etype}' on '{nid}': no live attempt"
    if live_token:
        if payload.get("claim_token") != live_token:
            return (f"'{etype}' on '{nid}': stale attempt (fencing token "
                    f"mismatch); ownership moved on")
    elif payload.get("holder", live_holder) != live_holder:
        # Legacy tokenless claim: fall back to holder match.
        return f"'{etype}' on '{nid}': holder mismatch"

    if etype == "heartbeat":
        if node["status"] not in ("claimed", "in_progress"):
            return (f"cannot heartbeat '{nid}': status is "
                    f"'{node['status']}'")
    elif etype == "released":
        if node["status"] not in ("claimed", "in_progress", "needs_human"):
            return (f"cannot release '{nid}': status is "
                    f"'{node['status']}'")
    elif etype in ("completed", "failed"):
        if node["status"] not in ("claimed", "in_progress"):
            return (f"cannot mark '{etype}' '{nid}': status is "
                    f"'{node['status']}'")
    return None


def _empty_claim() -> Dict[str, Any]:
    return {"holder": None, "attempt_id": None, "claim_token": None,
            "worker_id": None, "node_version": None, "base_branch": None,
            "claimed_at": None, "ttl_seconds": None, "last_heartbeat": None}


def _record_attempt(nodes: Dict[str, Dict[str, Any]], nid: str,
                    ev: Dict[str, Any], outcome: str, error: str) -> None:
    """Append one claim -> outcome entry to the node's attempt history.

    The entry is built from the node's live claim record plus the event
    that ended the attempt, so the history is fully derived from the
    log. Append-only and bounded to the last MAX_ATTEMPT_HISTORY
    entries. No-ops when there is no live attempt to record (e.g. a
    release of an already-idle node).
    """
    node = nodes.get(nid)
    if node is None:
        return
    claim = node.get("claim") or {}
    payload = ev.get("payload") or {}
    attempt_id = claim.get("attempt_id") or payload.get("attempt_id")
    if not attempt_id:
        return
    entry = {
        "attempt_id": attempt_id,
        "holder": claim.get("holder") or payload.get("holder"),
        "started_at": claim.get("claimed_at"),
        "ended_at": ev.get("timestamp"),
        "outcome": outcome,
        "error": (error or "")[:500],
    }
    attempts = node.get("attempts") or []
    node["attempts"] = (attempts[-(MAX_ATTEMPT_HISTORY - 1):] + [entry]
                        if attempts else [entry])


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
            change_policy=payload.get("change_policy", "warn"),
            integration_for=payload.get("integration_for"),
            max_retries=payload.get("max_retries"),
            retry_backoff_seconds=payload.get("retry_backoff_seconds"),
        )
        node["version"] = (nodes[nid]["version"] + 1) if nid in nodes else 1
        nodes[nid] = node
    elif etype == "node_edited":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        for key in ("title", "depends_on", "blast_radius", "handoff_note", "status",
                    "backend", "backend_config", "change_policy"):
            if key in payload:
                node[key] = deepcopy(payload[key])
        if node.get("change_policy") not in CHANGE_POLICIES:
            node["change_policy"] = "warn"
        # retry policy edits are applied defensively: a hand-crafted or
        # synced event with a bad value falls back instead of poisoning
        # the node record
        if "max_retries" in payload:
            node["max_retries"] = _coerce_max_retries(
                payload.get("max_retries"), node.get("max_retries",
                                                     DEFAULT_MAX_RETRIES))
        if "retry_backoff_seconds" in payload:
            node["retry_backoff_seconds"] = _coerce_backoff(
                payload.get("retry_backoff_seconds"),
                node.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS))
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
        # a fresh claim consumes the pending backoff, if any
        node["retry_at"] = None
        node["claim"] = {
            "holder": payload.get("holder"),
            "attempt_id": payload.get("attempt_id"),
            "claim_token": payload.get("claim_token"),
            "worker_id": payload.get("worker_id") or payload.get("holder"),
            "node_version": payload.get("node_version"),
            "base_branch": payload.get("worktree", {}).get("base_branch")
            if isinstance(payload.get("worktree"), dict) else None,
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
        # A reaper release is scoped to the attempt the reaper examined:
        # it must never wipe a newer attempt that interleaved (e.g. a
        # fresh claim via sync that sorts before this event). The
        # validator rejects such stale events; this guard is defense in
        # depth for direct apply_event callers.
        reaped = payload.get("reaped_attempt_id")
        live_attempt = (node.get("claim") or {}).get("attempt_id")
        if reaped and live_attempt and reaped != live_attempt:
            return
        _record_attempt(nodes, nid, ev,
                        "reaped" if payload.get("expired") else "released",
                        payload.get("note") or "")
        node["status"] = "unclaimed"
        node["retry_at"] = None
        note = payload.get("note")
        if note:
            # stash previous-attempt context on the node for next claimant
            node["handoff_note"] = (node.get("handoff_note") or "") + (
                ("\n" if node.get("handoff_note") else "") + f"[release note] {note}"
            ) if not payload.get("keep_handoff") else node.get("handoff_note")
            if payload.get("keep_handoff"):
                pass
        node["claim"] = _empty_claim()
        node["version"] += 1
    elif etype == "completed":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        _record_attempt(nodes, nid, ev, "done", "")
        node["status"] = "done"
        node["retry_at"] = None
        if "handoff_note" in payload:
            node["handoff_note"] = payload["handoff_note"]
        if "evidence" in payload:
            node["evidence"] = deepcopy(payload["evidence"])
        if "worktree" in payload and isinstance(payload["worktree"], dict):
            for k, v in payload["worktree"].items():
                node["worktree"][k] = v
        if "result" in payload and isinstance(payload["result"], dict):
            node["result"] = deepcopy(payload["result"])
        node["claim"] = _empty_claim()
        node["version"] += 1
    elif etype == "failed":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        outcome = payload.get("outcome") or "failed"
        _record_attempt(nodes, nid, ev, outcome, payload.get("error") or "")
        try:
            node["attempts_used"] = int(payload.get("attempts_used") or 0)
        except (TypeError, ValueError):
            node["attempts_used"] = 0
        if "evidence" in payload:
            node["evidence"] = deepcopy(payload["evidence"])
        if "error" in payload and payload["error"]:
            node["handoff_note"] = payload["error"]
        # Retryable failure with retries left: park as unclaimed with a
        # backoff deadline instead of terminally failed. Retries
        # exhausted (or a legacy event with no retry decision): failed.
        if payload.get("retry") and payload.get("retry_at"):
            node["status"] = "unclaimed"
            node["retry_at"] = payload["retry_at"]
        else:
            node["status"] = "failed"
            node["retry_at"] = None
        node["claim"] = _empty_claim()
        node["version"] += 1
    elif etype == "shipped":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        # One record per target branch: re-shipping after a re-run
        # updates the entry for that branch with the newer result.
        target_branch = payload.get("target_branch") or "unknown"
        shipped = node.get("shipped") or {}
        shipped[target_branch] = {
            "result_commit": payload.get("result_commit"),
            "merge_commit": payload.get("merge_commit"),
            "base_commit": payload.get("base_commit"),
            "diverged": bool(payload.get("diverged")),
            "forced": bool(payload.get("forced")),
            "shipped_at": ev.get("timestamp"),
            "target_branch": target_branch,
        }
        node["shipped"] = shipped
        node["version"] += 1
    elif etype == "release":
        # Repo-level event anchored at the reserved "skein-release" id.
        # No node state is derived: releases are listed by scanning the
        # log (shipping.list_releases), which keeps graph.json free of
        # non-node records.
        return
    elif etype == "rejected":
        # Informational audit of a fenced mutation that raised (stale
        # token, double-claim, invalid transition). Only the count is
        # derived; the full reasons stay in the log/timeline. Nodes from
        # old snapshots predate the field, hence the defensive .get().
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        node["rejected_count"] = int(node.get("rejected_count") or 0) + 1
        node["version"] += 1
    elif etype == "human_interrupt":
        node = nodes.get(nid)
        if node is None or node.get("removed"):
            return
        action = payload.get("action", "edit")
        if action == "delete":
            # a claimed node deleted by a human still had a live attempt;
            # record its interruption before the node is removed
            if node["status"] in ("claimed", "in_progress"):
                _record_attempt(nodes, nid, ev, "interrupted",
                                payload.get("reason") or "")
                node["retry_at"] = None
            node["removed"] = True
        elif action in ("edit", "reassign"):
            # edits carried in payload.fields. NOTE: a human_interrupt never
            # applies a status field - a claimed node is parked as
            # needs_human below, and terminal states are never set by
            # direct edit (see edits.py). This closes the path where a
            # human edit could flip a claimed node straight to done,
            # bypassing verification.
            fields = payload.get("fields") or {}
            for key in ("title", "depends_on", "blast_radius",
                        "backend", "backend_config"):
                if key in fields:
                    node[key] = deepcopy(fields[key])
            if "intent" in fields and isinstance(fields["intent"], dict):
                for k, v in fields["intent"].items():
                    node["intent"][k] = v
            # an interrupt targeting a claimed node parks it for human review
            if node["status"] in ("claimed", "in_progress"):
                _record_attempt(nodes, nid, ev, "interrupted",
                                payload.get("reason") or "")
                node["status"] = "needs_human"
                node["retry_at"] = None
                node["claim"] = _empty_claim()
        else:
            if node["status"] in ("claimed", "in_progress"):
                _record_attempt(nodes, nid, ev, "interrupted",
                                payload.get("reason") or "")
                node["status"] = "needs_human"
                node["retry_at"] = None
                node["claim"] = _empty_claim()
        node["version"] += 1


def _next_seq(repo_root: str | Path) -> int:
    """Monotonic per-repo sequence assigned under the control-plane lock."""
    lp = log_path(repo_root)
    max_seq = 0
    if lp.exists():
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line).get("seq")
            except (ValueError, AttributeError):
                continue
            if isinstance(s, int) and s > max_seq:
                max_seq = s
    return max_seq + 1


def append_event(repo_root: str | Path, actor: str, type: str, node_id: str,
                 payload: Optional[Dict[str, Any]] = None,
                 timestamp: Optional[str] = None,
                 commit: bool = True) -> Dict[str, Any]:
    """Append one event atomically: the whole read-validate-mutate-append-
    persist sequence runs under the repo control-plane lock, so concurrent
    processes on this machine cannot interleave a claim race."""
    if type not in VALID_EVENT_TYPES:
        raise ValueError(f"unknown event type: {type}")
    # Fail fast on hostile node ids: they become branch names, worktree
    # paths, and evidence filenames.
    from . import ids as _ids
    _ids.validate_node_id(node_id)
    # Write-boundary redaction: secrets in prompts, handoffs, error
    # strings, or evidence commands never reach events.ndjson verbatim.
    # Already-stored events are never mutated; redaction is deterministic
    # so synced peers reduce identical payloads.
    payload, redacted_hits = redact.redact_payload(payload or {})
    with locks.repo_lock(repo_root):
        ev = {
            "event_id": uuid.uuid4().hex,
            "schema_version": EVENT_SCHEMA_VERSION,
            "seq": _next_seq(repo_root),
            "timestamp": timestamp or utcnow_iso(),
            "actor": actor,
            "type": type,
            "node_id": node_id,
            "payload": payload or {},
        }
        # The state machine is enforced at write time too: a lifecycle
        # event that is not a legal fenced transition is rejected before
        # it ever reaches the log.
        err = lifecycle_transition_error(load_graph(repo_root), ev)
        if err:
            raise ValueError(err)
        lp = log_path(repo_root)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
            f.flush()
            os.fsync(f.fileno())
        rebuild_graph(repo_root)
        if commit:
            git_commit_log(repo_root, f"skein: {type} {node_id} by {actor}")
    if redacted_hits:
        # Audit the redaction itself: count only, never the secret. The
        # security payload carries no secret shapes, so this nested
        # append cannot recurse.
        append_event(repo_root, actor, "security", node_id,
                     {"kind": "redaction", "event_type": type,
                      "redacted_count": redacted_hits},
                     commit=commit)
    return ev


def _log_fingerprint(repo_root: str | Path) -> Tuple[int, Optional[str], str]:
    """Cheap log identity: (non-empty line count, last event_id, sha256).

    One pass over raw bytes - no per-line JSON parsing - so load_graph()
    can validate the snapshot without a full reduction.
    """
    lp = log_path(repo_root)
    if not lp.exists():
        return 0, None, hashlib.sha256(b"").hexdigest()
    raw = lp.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    last_id: Optional[str] = None
    count = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        count += 1
        last_line = line
    if count:
        try:
            last_id = json.loads(last_line).get("event_id")
        except (ValueError, AttributeError):
            last_id = None
    return count, last_id, digest


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows: a concurrent reader (or an AV scan) can hold the destination
    # open without delete sharing, so the rename can fail with
    # PermissionError even though nothing is logically wrong. The window
    # is tiny; retry briefly instead of surfacing a spurious failure.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def rebuild_graph(repo_root: str | Path) -> Dict[str, Dict[str, Any]]:
    with locks.repo_lock(repo_root):
        events = load_events(repo_root)
        nodes = reduce_events(events)
        gp = graph_path(repo_root)
        gp.parent.mkdir(parents=True, exist_ok=True)
        # graph.json is derived; never hand-edited (written only here)
        visible = {nid: n for nid, n in nodes.items() if not n.get("removed")}
        text = json.dumps(visible, indent=2, sort_keys=True)
        _write_atomic(gp, text)
        count, last_id, digest = _log_fingerprint(repo_root)
        meta = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_count": count,
            "last_event_id": last_id,
            "log_digest": digest,
            # bind the snapshot to its own bytes too: a hand-edited or
            # corrupted graph.json must not be trusted just because the
            # log is unchanged
            "graph_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "generated_at": utcnow_iso(),
        }
        _write_atomic(meta_path(repo_root), json.dumps(meta, indent=2, sort_keys=True))
        return visible


def _snapshot_is_fresh(repo_root: str | Path) -> bool:
    gp = graph_path(repo_root)
    mp = meta_path(repo_root)
    if not (gp.exists() and mp.exists()):
        return False
    try:
        meta = json.loads(mp.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if meta.get("schema_version") != EVENT_SCHEMA_VERSION:
        return False
    count, last_id, digest = _log_fingerprint(repo_root)
    if not (meta.get("event_count") == count
            and meta.get("last_event_id") == last_id
            and meta.get("log_digest") == digest):
        return False
    # the snapshot must also be byte-identical to what the last rebuild
    # wrote; otherwise rebuild from the log (source of truth)
    try:
        graph_digest = hashlib.sha256(gp.read_bytes()).hexdigest()
    except OSError:
        return False
    return meta.get("graph_digest") == graph_digest


def load_graph(repo_root: str | Path) -> Dict[str, Dict[str, Any]]:
    if _snapshot_is_fresh(repo_root):
        try:
            return json.loads(graph_path(repo_root).read_text(encoding="utf-8"))
        except (ValueError, OSError):
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
        r = subprocess.run(
            ["git", "-c", "user.name=skein", "-c", "user.email=skein@localhost",
             "commit", "-m", message, "--", SKEIN_DIR],
            cwd=str(repo_root), capture_output=True, check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False
