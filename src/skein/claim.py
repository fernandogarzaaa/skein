"""Claim protocol: fenced attempts, eligibility, TTL/heartbeat, reaper.

Execution model: every claim creates one immutable *attempt* identified
by (attempt_id, claim_token). The claim_token is a fencing token: any
lifecycle mutation (heartbeat, release, complete, fail) must present the
token of the currently owning attempt, or it is rejected. This makes the
stale-worker scenario impossible:

    A claims X (token t1) -> A stalls -> lease expires -> reaper releases
    -> B claims X (token t2) -> A's late completion presents t1 -> rejected

All transitions run under the repo control-plane lock, so the
read-validate-mutate-append sequence is atomic across processes on one
machine (a true local CAS). Git sync across machines stays eventual.
"""

from __future__ import annotations

import fnmatch
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import graph as g
from . import locks


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
    # Retry backoff gate: a failed attempt parks the node as unclaimed
    # with retry_at set; it becomes claimable again once the deadline
    # passes. Tests set retry_at in the past (or mock now_utc) instead
    # of sleeping.
    retry_at = parse_ts(node.get("retry_at"))
    if retry_at is not None and now_utc() < retry_at:
        return False, f"backoff until {retry_at.isoformat()}"
    for dep in node.get("depends_on", []):
        d = nodes.get(dep)
        if d is None or d.get("removed"):
            return False, f"dependency '{dep}' is missing/removed"
        if d["status"] != "done":
            return False, f"dependency '{dep}' is '{d['status']}', must be 'done'"
    # Multi-dependency nodes run only on a successfully integrated base:
    # the integration node is a first-class dependency result. Integration
    # nodes themselves are exempt (they ARE the integration step).
    if len(node.get("depends_on", [])) > 1 and not node.get("integration_for"):
        integ = nodes.get(f"{node_id}__integrate")
        if integ is None or integ.get("removed"):
            return False, (f"integration node '{node_id}__integrate' does not "
                           f"exist yet; run the node via the supervisor first")
        if integ["status"] != "done":
            return False, (f"integration node '{node_id}__integrate' is "
                           f"'{integ['status']}', must be 'done'")
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


def current_claim(repo_root: str | Path, node_id: str) -> Dict:
    """Return the node's current claim record (may be empty)."""
    node = g.load_graph(repo_root).get(node_id)
    if node is None:
        raise ClaimError(f"unknown node '{node_id}'")
    return dict(node.get("claim") or {})


def _check_fence(node: Dict, node_id: str, claim_token: Optional[str],
                 op: str) -> Dict:
    """Enforce the fencing token for lifecycle mutations.

    A worker presenting a stale (or missing) token for an attempt it no
    longer owns gets a ClaimError instead of mutating the node.
    """
    claim = node.get("claim") or {}
    current = claim.get("claim_token")
    if not current:
        raise ClaimError(f"node '{node_id}' has no live attempt to {op}")
    if not claim_token or claim_token != current:
        raise ClaimError(
            f"stale attempt on '{node_id}': cannot {op} (ownership moved on; "
            f"this worker no longer holds the node)"
        )
    return claim


def claim_node(repo_root: str | Path, node_id: str, holder: str,
               ttl_seconds: Optional[int] = None,
               expected_version: Optional[int] = None,
               actor: Optional[str] = None,
               worker_id: Optional[str] = None) -> Dict:
    """Atomic compare-and-swap claim under the control-plane lock.

    The whole read-check-append transition is serialized, so two
    processes can no longer both pass the eligibility check and append
    competing claims. Creates one fenced attempt (attempt_id +
    claim_token); every later mutation of that attempt must present the
    token.
    """
    from . import worktree as wt
    cfg = g.load_config(repo_root)
    ttl = ttl_seconds if ttl_seconds is not None else int(cfg.get("default_ttl_seconds", 1800))
    actor = actor or holder
    worker_id = worker_id or holder
    # The whole read-check-append runs inside one critical section, so
    # two contenders cannot both pass eligibility and append: the loser
    # re-reads under the lock and sees the winner's claim.
    with locks.repo_lock(repo_root):
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
        attempt_id = uuid.uuid4().hex
        claim_token = uuid.uuid4().hex
        # determine base branch for bookkeeping (worktree created in run path)
        try:
            base = wt.base_branch_for(repo_root, node_id, nodes)
        except wt.WorktreeError as e:
            raise ClaimError(str(e))
        g.append_event(
            repo_root, actor, "claimed", node_id,
            {"holder": holder, "worker_id": worker_id,
             "attempt_id": attempt_id, "claim_token": claim_token,
             "node_version": node["version"],
             "ttl_seconds": ttl,
             "worktree": {"base_branch": base}},
        )
        return g.load_graph(repo_root)[node_id]


def heartbeat(repo_root: str | Path, node_id: str, holder: str,
              claim_token: Optional[str] = None,
              commit: bool = False) -> Dict:
    """Renew a lease. With claim_token, the heartbeat is fenced: a stale
    worker whose attempt was reaped/released gets ClaimError instead of
    silently reviving the lease. Heartbeats are ephemeral liveness, not
    durable state, so they skip the git commit by default."""
    with locks.repo_lock(repo_root):
        nodes = g.load_graph(repo_root)
        node = nodes.get(node_id)
        if node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        claim = node.get("claim") or {}
        if claim.get("holder") != holder:
            raise ClaimError(f"node '{node_id}' held by '{claim.get('holder')}', not '{holder}'")
        if claim_token is not None:
            _check_fence(node, node_id, claim_token, "heartbeat")
        if node["status"] not in ("claimed", "in_progress"):
            raise ClaimError(f"node '{node_id}' status '{node['status']}' cannot heartbeat")
        payload = {"holder": holder}
        if claim_token is not None:
            payload["claim_token"] = claim_token
        g.append_event(repo_root, holder, "heartbeat", node_id, payload,
                       commit=commit)
        return g.load_graph(repo_root)[node_id]


def release_node(repo_root: str | Path, node_id: str, actor: str,
                 force: bool = False, note: str = "",
                 claim_token: Optional[str] = None) -> Dict:
    with locks.repo_lock(repo_root):
        nodes = g.load_graph(repo_root)
        node = nodes.get(node_id)
        if node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        if node["status"] not in ("claimed", "in_progress", "needs_human", "failed", "blocked"):
            raise ClaimError(f"node '{node_id}' status '{node['status']}' is not releasable")
        if not force and node["status"] in ("claimed", "in_progress"):
            if claim_token is not None:
                _check_fence(node, node_id, claim_token, "release")
            else:
                holder = (node.get("claim") or {}).get("holder")
                if holder and holder != actor:
                    raise ClaimError(
                        f"node '{node_id}' held by '{holder}'; use --force for explicit force-release"
                    )
        payload = {"note": note or (f"force-released by {actor}" if force else f"released by {actor}"),
                   "forced": force,
                   "holder": (node.get("claim") or {}).get("holder")}
        if claim_token is not None:
            payload["claim_token"] = claim_token
        g.append_event(repo_root, actor, "released", node_id, payload)
        return g.load_graph(repo_root)[node_id]


def complete_node(repo_root: str | Path, node_id: str, actor: str,
                  claim_token: str,
                  handoff_note: str = "",
                  evidence: Optional[List[Dict]] = None,
                  worktree: Optional[Dict] = None,
                  result: Optional[Dict] = None) -> Dict:
    """Fenced completion: only the current attempt's token can mark done."""
    with locks.repo_lock(repo_root):
        nodes = g.load_graph(repo_root)
        node = nodes.get(node_id)
        if node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        if node["status"] not in ("claimed", "in_progress"):
            raise ClaimError(
                f"node '{node_id}' status '{node['status']}' cannot complete")
        claim = _check_fence(node, node_id, claim_token, "complete")
        payload = {
            "handoff_note": handoff_note,
            "evidence": evidence or [],
            "attempt_id": claim.get("attempt_id"),
            "claim_token": claim_token,
        }
        if worktree:
            payload["worktree"] = worktree
        if result:
            payload["result"] = result
        g.append_event(repo_root, actor, "completed", node_id, payload)
        return g.load_graph(repo_root)[node_id]


def _backoff_with_jitter(base_seconds: float, failures_used: int) -> float:
    """Exponential backoff with +/-25% jitter: base * 2**failures_used.

    failures_used counts failures already consumed (0 for the first
    retry), so the first backoff is ~base, the next ~2*base, and so on.
    The jittered value is computed once at fail time and recorded on
    the failed event, so reduce stays deterministic.
    """
    base = max(0.0, float(base_seconds))
    return base * (2 ** max(0, failures_used)) * random.uniform(0.75, 1.25)


def fail_node(repo_root: str | Path, node_id: str, actor: str,
              claim_token: str,
              evidence: Optional[List[Dict]] = None,
              error: str = "",
              outcome: str = "failed") -> Dict:
    """Fenced failure: only the current attempt's token can mark failed.

    Retryable failures (backend failure, timeout, verification failure)
    do NOT park the node at failed while retries remain: the node goes
    back to unclaimed with retry_at set to now + backoff, and the
    attempt is appended to the node's attempt history. Only when
    failures_used reaches max_retries does the node park at failed.
    Human interrupts and claim-token violations never reach this path:
    interrupts park at needs_human via human_interrupt, and stale
    tokens are rejected by the fence above.
    """
    if outcome not in ("failed", "timeout"):
        raise ClaimError(f"unknown failure outcome '{outcome}'")
    with locks.repo_lock(repo_root):
        nodes = g.load_graph(repo_root)
        node = nodes.get(node_id)
        if node is None:
            raise ClaimError(f"unknown node '{node_id}'")
        if node["status"] not in ("claimed", "in_progress"):
            raise ClaimError(
                f"node '{node_id}' status '{node['status']}' cannot fail")
        claim = _check_fence(node, node_id, claim_token, "fail")
        attempts = node.get("attempts") or []
        failures_used = sum(1 for a in attempts
                            if a.get("outcome") in g.RETRYABLE_OUTCOMES)
        max_retries = node.get("max_retries", g.DEFAULT_MAX_RETRIES)
        try:
            max_retries = int(max_retries)
        except (TypeError, ValueError):
            max_retries = g.DEFAULT_MAX_RETRIES
        retry = failures_used < max(0, max_retries)
        backoff = _backoff_with_jitter(
            node.get("retry_backoff_seconds",
                     g.DEFAULT_RETRY_BACKOFF_SECONDS),
            failures_used) if retry else 0.0
        retry_at = (now_utc() + timedelta(seconds=backoff)).isoformat() \
            if retry else None
        g.append_event(repo_root, actor, "failed", node_id, {
            "evidence": evidence or [],
            "error": error,
            "outcome": outcome,
            "attempt_id": claim.get("attempt_id"),
            "claim_token": claim_token,
            "retry": retry,
            "retry_at": retry_at,
            "attempts_used": failures_used,
            "backoff_seconds": backoff,
        })
        return g.load_graph(repo_root)[node_id]


def reap_expired(repo_root: str | Path, actor: str = "reaper",
                 at: Optional[datetime] = None) -> List[str]:
    """Release expired leases. Returns list of released node ids."""
    at = at or now_utc()
    released = []
    with locks.repo_lock(repo_root):
        nodes = g.load_graph(repo_root)
        for nid, node in list(nodes.items()):
            if node.get("status") in ("claimed", "in_progress") and is_expired(node, at):
                claim = node.get("claim") or {}
                holder = claim.get("holder")
                # Event time is now (when the reaper ran). `at` is the
                # decision input ("expired as of at") and is recorded as
                # reaped_as_of so the validator evaluates expiry at the
                # same instant the reaper did. The release is scoped to
                # the exact attempt examined: a newer attempt that
                # interleaves later must not be wiped by this event.
                g.append_event(repo_root, actor, "released", nid, {
                    "note": f"lease expired (holder={holder}); previous attempt did not complete",
                    "expired": True,
                    "reaped_as_of": at.isoformat(),
                    "reaped_attempt_id": claim.get("attempt_id"),
                    "reaped_holder": holder,
                })
                released.append(nid)
    return released
