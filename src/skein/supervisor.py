"""Supervisor: wraps backend invocation with heartbeats + interrupt watch.

Sends heartbeats on an interval; watches the event log for
`human_interrupt` targeting the node; terminates the agent process
gracefully on interrupt; runs the verification gate (never trusts the
agent's self-report alone).
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import graph as g
from . import claim as c
from . import worktree as wt
from . import verify as v


def _interrupted(repo_root: str | Path, node_id: str, since_ts: str) -> bool:
    for ev in g.load_events(repo_root):
        if (ev.get("type") == "human_interrupt" and ev.get("node_id") == node_id
                and ev.get("timestamp", "") >= since_ts):
            return True
    return False


def _heartbeat_loop(repo_root: str | Path, node_id: str, holder: str,
                    interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            c.heartbeat(str(repo_root), node_id, holder)
        except Exception:
            pass  # heartbeats are best-effort; reaper handles expiry


def _collect_parent_handoffs(nodes: Dict[str, Dict], node: Dict) -> str:
    notes = []
    for dep in node.get("depends_on", []):
        d = nodes.get(dep)
        if d and d.get("handoff_note"):
            notes.append(f"[{dep}] {d['handoff_note']}")
    return "\n".join(notes)


def _write_adapter_evidence(repo_root: str, adapter, node_id: str,
                            adapter_exit: int, adapter_output: str) -> Dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    ev_file = v.evidence_subdir(repo_root) / f"{node_id}_{ts}_adapter.log"
    ev_file.parent.mkdir(parents=True, exist_ok=True)
    ev_file.write_text(
        f"adapter={getattr(adapter, 'name', '?')} exit={adapter_exit}\n{adapter_output[-20000:]}",
        encoding="utf-8")
    return {"command": f"<adapter:{getattr(adapter, 'name', '?')}>",
            "exit_code": adapter_exit, "output_ref": str(ev_file)}


def run_node(repo_root: str | Path, node_id: str, holder: str,
             adapter=None, verify_timeout: int = 600,
             heartbeat_interval: float = 30.0,
             poll_interval: float = 1.0,
             adapter_timeout: float = 1800.0,
             extra_args: Optional[list] = None,
             actor: Optional[str] = None) -> Dict:
    """Full supervised path: claim -> worktree -> adapter -> verify -> report."""
    from .adapters.engine import ProfileAdapter
    from .adapters.profiles import get_profile
    actor = actor or holder
    repo_root = str(repo_root)
    # Watch window starts at entry: an interrupt arriving during claim or
    # worktree setup must still abort the run, not be missed.
    started_ts = datetime.now(timezone.utc).isoformat()

    nodes = g.load_graph(repo_root)
    if node_id not in nodes:
        raise ValueError(f"unknown node '{node_id}'")
    node = nodes[node_id]
    if adapter is None:
        # Backend comes from the node; the default preserves the
        # long-standing claude_code behavior for nodes that predate it.
        backend_name = node.get("backend") or "claude_code"
        adapter = ProfileAdapter(get_profile(backend_name, repo_root),
                                 backend_config=node.get("backend_config"),
                                 extra_args=extra_args)

    # multi-dependency: ensure integration node exists first
    if len(node.get("depends_on", [])) > 1:
        wt.ensure_integration_node(repo_root, node_id, actor=actor)

    # claim if needed (idempotent when we already hold it)
    nodes = g.load_graph(repo_root)
    node = nodes[node_id]
    claim_holder = (node.get("claim") or {}).get("holder")
    if node["status"] == "unclaimed":
        node = c.claim_node(repo_root, node_id, holder, actor=actor)
    elif claim_holder != holder:
        raise c.ClaimError(f"node '{node_id}' held by '{claim_holder}', not '{holder}'")

    path, branch, base = wt.ensure_worktree(repo_root, node_id, actor=actor)

    # attach parent handoff notes so dependents read summaries, not transcripts
    nodes = g.load_graph(repo_root)
    node = nodes[node_id]
    handoffs = _collect_parent_handoffs(nodes, node)
    node_for_adapter = dict(node)
    node_for_adapter["intent"] = dict(node.get("intent", {}))
    if handoffs:
        node_for_adapter["intent"]["parent_handoffs"] = handoffs

    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop,
                          args=(repo_root, node_id, holder, heartbeat_interval, stop),
                          daemon=True)
    hb.start()

    # launch backend headlessly against the worktree (Popen so we can abort)
    if hasattr(adapter, "build_command"):
        cmd = adapter.build_command(node_for_adapter)
    else:
        prompt = adapter.build_prompt(node_for_adapter)
        binary = getattr(adapter, "binary", "claude")
        cmd = [binary, "--print", prompt]
    proc = subprocess.Popen(cmd, cwd=str(path), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    adapter_output = ""
    adapter_exit = -1
    aborted = False
    timed_out = False
    t0 = time.monotonic()
    while True:
        rc = proc.poll()
        if _interrupted(repo_root, node_id, started_ts):
            aborted = True
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:
                adapter_output = proc.stdout.read() if proc.stdout else ""
            except Exception:
                pass
            adapter_exit = proc.returncode
            break
        if rc is not None:
            try:
                adapter_output = proc.stdout.read() if proc.stdout else ""
            except Exception:
                pass
            adapter_exit = rc
            break
        if time.monotonic() - t0 > adapter_timeout:
            # Ceiling so a stalled agent process (e.g. waiting on a permission
            # prompt the headless flags didn't cover) becomes failed evidence,
            # not a hang.
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:
                adapter_output = (proc.stdout.read() if proc.stdout else "") or ""
            except Exception:
                pass
            adapter_output += f"\n[skein] adapter timed out after {adapter_timeout}s and was killed"
            adapter_exit = 124
            break
        time.sleep(poll_interval)

    if aborted:
        stop.set()
        g.append_event(repo_root, holder, "released", node_id,
                       {"note": f"aborted: human_interrupt received during run (holder={holder})"})
        return {"outcome": "interrupted", "adapter_exit": adapter_exit,
                "node": g.load_graph(repo_root).get(node_id)}

    if timed_out:
        stop.set()
        adapter_ev = _write_adapter_evidence(repo_root, adapter, node_id,
                                             adapter_exit, adapter_output)
        g.append_event(repo_root, holder, "failed", node_id, {
            "evidence": [adapter_ev],
            "error": f"backend timed out after {adapter_timeout}s (holder={holder}); see evidence",
        })
        return {"outcome": "failed", "adapter_exit": adapter_exit,
                "evidence": [adapter_ev],
                "node": g.load_graph(repo_root).get(node_id)}

    # Verification gate: run completion commands for real. Agent self-report
    # (adapter_exit) is never sufficient on its own.
    ok, evidence = v.run_completion(path, node.get("intent", {}).get("completion", ""),
                                    v.evidence_subdir(repo_root), node_id,
                                    timeout=verify_timeout)
    adapter_ev = _write_adapter_evidence(repo_root, adapter, node_id,
                                         adapter_exit, adapter_output)
    evidence = evidence + [adapter_ev]
    stop.set()
    if ok:
        summary = (adapter_output.strip().splitlines() or [""])[-1][:2000] if adapter_output.strip() else "completed; verification passed"
        handoff = f"Done. Verification passed. Last output: {summary}"
        g.append_event(repo_root, holder, "completed", node_id, {
            "handoff_note": handoff, "evidence": evidence,
            "worktree": {"branch": branch, "base_branch": base, "path": str(path)},
        })
        return {"outcome": "done", "adapter_exit": adapter_exit, "evidence": evidence,
                "node": g.load_graph(repo_root).get(node_id)}
    else:
        g.append_event(repo_root, holder, "failed", node_id, {
            "evidence": evidence,
            "error": f"completion command failed (adapter exit={adapter_exit}); see evidence",
        })
        return {"outcome": "failed", "adapter_exit": adapter_exit, "evidence": evidence,
                "node": g.load_graph(repo_root).get(node_id)}
