"""Supervisor: wraps backend invocation with heartbeats + interrupt watch.

Sends heartbeats on an interval; watches the event log for
`human_interrupt` targeting the node; terminates the agent process
gracefully on interrupt; runs the verification gate (never trusts the
agent's self-report alone).

Every lifecycle mutation is fenced by the attempt's claim_token: a
worker that lost ownership (lease expired, reaped, re-claimed by
someone else) can no longer heartbeat, complete, or fail the node.
After verification passes, the worktree is checkpointed into a durable
result commit - downstream nodes branch from that commit, never from an
uncommitted working tree.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import graph as g
from . import claim as c
from . import ids
from . import redact
from . import worktree as wt
from . import verify as v
from . import runtime as rt


class PolicyViolation(Exception):
    """Raised when the change policy (strict) rejects the worktree diff."""


def _interrupted(repo_root: str | Path, node_id: str, since_ts: str) -> bool:
    for ev in g.load_events(repo_root):
        if (ev.get("type") == "human_interrupt" and ev.get("node_id") == node_id
                and ev.get("timestamp", "") >= since_ts):
            return True
    return False


def _heartbeat_loop(repo_root: str | Path, node_id: str, holder: str,
                    claim_token: Optional[str],
                    interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            c.heartbeat(str(repo_root), node_id, holder,
                        claim_token=claim_token)
        except c.ClaimError:
            # Lost ownership (reaped / released / re-claimed): stop
            # renewing a lease we no longer hold.
            break
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
                            adapter_exit: int, adapter_output: str
                            ) -> Tuple[Dict, int]:
    """Write the backend's output to an evidence file, redacted at write
    time so secrets never land in .skein/evidence/ verbatim. Returns
    (evidence_entry, redacted_hit_count)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    ev_file = v.evidence_subdir(repo_root) / ids.evidence_name(node_id, f"{ts}_adapter")
    ev_file.parent.mkdir(parents=True, exist_ok=True)
    hits = redact.write_redacted(
        ev_file,
        f"adapter={getattr(adapter, 'name', '?')} exit={adapter_exit}\n{adapter_output[-20000:]}")
    return ({"command": f"<adapter:{getattr(adapter, 'name', '?')}>",
             "exit_code": adapter_exit, "output_ref": str(ev_file)}, hits)


def _parse_adapter_output(adapter, result: "rt.ExecutionResult") -> Tuple[int, str]:
    """Apply the backend's output parser to the raw (stdout, stderr,
    exit_code) triple, so supervised evidence matches what
    ProfileAdapter.run() records. Adapters without a profile keep the
    legacy shape: stdout plus a stderr trailer."""
    parser = getattr(getattr(adapter, "profile", None), "output_parser", None)
    if parser is None:
        out = result.stdout
        if result.stderr:
            out += "\n--- stderr ---\n" + result.stderr
        return result.exit_code, out
    parsed = parser(result.stdout, result.stderr, result.exit_code)
    return parsed.exit_code, parsed.output


def _git_in(worktree_path: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=str(worktree_path),
                          capture_output=True, text=True)


def _changed_files(worktree_path: str | Path) -> List[str]:
    """Files changed in the worktree vs HEAD (tracked + untracked)."""
    r = _git_in(worktree_path, "status", "--porcelain")
    files = []
    for line in (r.stdout or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if not path:
            continue
        if " -> " in path:  # rename: take the new name
            path = path.split(" -> ", 1)[1].strip().strip('"')
        files.append(path.strip('"'))
    return files


def _diff_stats(worktree_path: str | Path, base_commit: str,
                result_commit: str) -> Dict[str, Dict[str, int]]:
    if not base_commit or not result_commit or base_commit == result_commit:
        return {}
    r = _git_in(worktree_path, "diff", "--numstat", base_commit, result_commit)
    stats: Dict[str, Dict[str, int]] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                stats[parts[2]] = {"added": int(parts[0]), "deleted": int(parts[1])}
            except ValueError:
                pass
    return stats


def _check_change_policy(changed: List[str], blast_radius: List[str],
                         policy: str) -> List[str]:
    """Return human-readable warnings for files outside the blast radius."""
    if policy == "off" or not blast_radius or not changed:
        return []
    warnings = []
    for f in changed:
        if not c.blast_overlap([f], blast_radius or []):
            warnings.append(
                f"{f} is outside the declared blast radius {blast_radius}")
    return warnings


def _checkpoint_result(repo_root: str | Path, worktree_path: str | Path,
                       node_id: str, attempt_id: str, base_commit: str,
                       node: Dict, actor: str) -> Tuple[Dict, List[str]]:
    """Stage the worktree, enforce the change policy, and create the
    durable result commit. Returns (result_record, warnings).

    A node is not a reusable dependency result until its work exists as
    a commit: downstream worktrees branch from result.commit.
    """
    policy = node.get("change_policy", "warn")
    blast_radius = node.get("blast_radius", [])
    changed = _changed_files(worktree_path)
    warnings = _check_change_policy(changed, blast_radius, policy)
    if warnings and policy == "strict":
        raise PolicyViolation(
            "change policy 'strict' rejected out-of-scope changes: "
            + "; ".join(warnings))
    if changed:
        r = _git_in(worktree_path, "add", "-A")
        if r.returncode != 0:
            raise RuntimeError(f"git add failed in worktree: {r.stderr[:500]}")
        # Commit with skein-local identity flags only: never touches the
        # user's repo git config (see cmd_init).
        r = _git_in(worktree_path, "-c", "user.name=skein",
                    "-c", "user.email=skein@localhost",
                    "commit", "-m",
                    f"skein: result {node_id} attempt {attempt_id} by {actor}")
        if r.returncode != 0:
            raise RuntimeError(f"result commit failed: {r.stderr[:500]}")
    result_commit = _git_in(worktree_path, "rev-parse", "HEAD").stdout.strip()
    result = {
        "base_commit": base_commit,
        "commit": result_commit,
        "changed_files": changed,
        "diff_stats": _diff_stats(worktree_path, base_commit, result_commit),
        "attempt_id": attempt_id,
    }
    return result, warnings


def run_node(repo_root: str | Path, node_id: str, holder: str,
             adapter=None, verify_timeout: int = 600,
             heartbeat_interval: float = 30.0,
             poll_interval: float = 1.0,
             adapter_timeout: float = 1800.0,
             extra_args: Optional[list] = None,
             actor: Optional[str] = None,
             sandbox: bool = False) -> Dict:
    """Full supervised path: claim -> worktree -> adapter -> verify -> report.

    sandbox=True scrubs the backend's environment to the allowlist and,
    on Linux, wraps it in prlimit(1) CPU/memory caps. The sandbox is
    best-effort: when it cannot apply, the run continues unsandboxed and
    a `security` event records why.
    """
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

    claim = node.get("claim") or {}
    attempt_id = claim.get("attempt_id")
    claim_token = claim.get("claim_token")

    def owns_attempt() -> bool:
        """Fencing check: is our attempt still the live owner?"""
        cur = c.current_claim(repo_root, node_id)
        return (bool(claim_token)
                and cur.get("claim_token") == claim_token
                and cur.get("holder") == holder)

    path, branch, base = wt.ensure_worktree(repo_root, node_id, actor=actor)
    # Durable base of this attempt, captured before the agent runs.
    base_commit = _git_in(path, "rev-parse", "HEAD").stdout.strip()

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
                          args=(repo_root, node_id, holder, claim_token,
                                heartbeat_interval, stop),
                          daemon=True)
    hb.start()

    def _audit_redaction(hits: int, source: str) -> None:
        # Count per event, never the secret itself.
        if hits:
            g.append_event(repo_root, actor, "security", node_id,
                           {"kind": "redaction", "source": source,
                            "redacted_count": hits})

    # launch backend headlessly against the worktree through the
    # canonical runtime: bounded, interruptible, whole-tree kill on
    # abort/timeout. The backend's environment is scrubbed to the
    # allowlist (ambient secrets are not inherited); sandbox=True
    # additionally applies prlimit caps on Linux. The profile's output
    # parser is applied so evidence matches what ProfileAdapter.run()
    # would record.
    if hasattr(adapter, "build_command"):
        cmd = adapter.build_command(node_for_adapter)
    else:
        prompt = adapter.build_prompt(node_for_adapter)
        binary = getattr(adapter, "binary", "claude")
        cmd = [binary, "--print", prompt]
    stdin_text = None
    profile = getattr(adapter, "profile", None)
    if getattr(profile, "prompt_mode", "") == "stdin":
        stdin_text = adapter.build_prompt(node_for_adapter)
    result = rt.execute(
        cmd, cwd=path, timeout=adapter_timeout, stdin_text=stdin_text,
        should_abort=lambda: _interrupted(repo_root, node_id, started_ts),
        poll_interval=poll_interval,
        env=rt.minimal_environ(),
        sandbox=sandbox,
    )
    if result.sandbox_note:
        g.append_event(repo_root, actor, "security", node_id,
                       {"kind": "sandbox_fallback", "note": result.sandbox_note})
    adapter_exit, adapter_output = _parse_adapter_output(adapter, result)
    aborted = result.aborted
    # Ceiling so a stalled agent process (e.g. waiting on a permission
    # prompt the headless flags didn't cover) becomes failed evidence,
    # not a hang.
    timed_out = result.timed_out
    if timed_out:
        adapter_output += (
            f"\n[skein] adapter timed out after {adapter_timeout}s; "
            f"process tree killed")
    if aborted:
        adapter_output += "\n[skein] aborted by human interrupt; process tree killed"

    if aborted:
        stop.set()
        # Do NOT release: the human_interrupt event already moved the node
        # to needs_human and cleared the claim atomically. Releasing here
        # would park it back in unclaimed and let the scheduler re-run
        # work the human explicitly stopped.
        return {"outcome": "interrupted", "adapter_exit": adapter_exit,
                "node": g.load_graph(repo_root).get(node_id)}

    if timed_out:
        stop.set()
        adapter_ev, adapter_redacted = _write_adapter_evidence(
            repo_root, adapter, node_id, adapter_exit, adapter_output)
        _audit_redaction(adapter_redacted, "adapter_evidence")
        try:
            c.fail_node(repo_root, node_id, holder, claim_token,
                        evidence=[adapter_ev],
                        error=f"backend timed out after {adapter_timeout}s (holder={holder}); see evidence",
                        outcome="timeout")
        except c.ClaimError:
            return {"outcome": "superseded", "adapter_exit": adapter_exit,
                    "node": g.load_graph(repo_root).get(node_id)}
        return {"outcome": "failed", "adapter_exit": adapter_exit,
                "evidence": [adapter_ev],
                "node": g.load_graph(repo_root).get(node_id)}

    # Fencing + interrupt re-check BEFORE verification: a human edit that
    # landed while the agent was finishing, or a lost lease, must stop us
    # here - verification must not run on a node we no longer own.
    if _interrupted(repo_root, node_id, started_ts):
        stop.set()
        # The adapter process already exited inside execute(); the
        # human_interrupt event parked the node in needs_human and
        # cleared the claim. Do not release it back to unclaimed.
        return {"outcome": "interrupted", "adapter_exit": adapter_exit,
                "node": g.load_graph(repo_root).get(node_id)}
    if not owns_attempt():
        stop.set()
        return {"outcome": "superseded", "adapter_exit": adapter_exit,
                "note": "attempt lost ownership before verification; not completing",
                "node": g.load_graph(repo_root).get(node_id)}

    # Verification gate: run completion commands for real. Agent self-report
    # (adapter_exit) is never sufficient on its own. Verification is
    # cancellable: an interrupt mid-command kills the command's process
    # tree and stops the remaining commands.
    ok, evidence, verify_redacted = v.run_completion(
        path, node.get("intent", {}).get("completion", ""),
        v.evidence_subdir(repo_root), node_id,
        timeout=verify_timeout,
        should_abort=lambda: _interrupted(
            repo_root, node_id, started_ts))
    adapter_ev, adapter_redacted = _write_adapter_evidence(
        repo_root, adapter, node_id, adapter_exit, adapter_output)
    _audit_redaction(verify_redacted, "verification_evidence")
    _audit_redaction(adapter_redacted, "adapter_evidence")
    evidence = evidence + [adapter_ev]
    stop.set()

    # Final fenced transition: re-check interrupt + ownership immediately
    # before appending the terminal event.
    if _interrupted(repo_root, node_id, started_ts):
        # human_interrupt already parked the node in needs_human and
        # cleared the claim; do not release it back to unclaimed.
        return {"outcome": "interrupted", "adapter_exit": adapter_exit,
                "node": g.load_graph(repo_root).get(node_id)}
    if not owns_attempt():
        return {"outcome": "superseded", "adapter_exit": adapter_exit,
                "note": "attempt lost ownership during verification; not completing",
                "node": g.load_graph(repo_root).get(node_id)}

    if ok:
        try:
            result, warnings = _checkpoint_result(
                repo_root, path, node_id, attempt_id, base_commit, node, actor)
        except PolicyViolation as e:
            c.fail_node(repo_root, node_id, holder, claim_token,
                        evidence=evidence, error=str(e))
            return {"outcome": "failed", "adapter_exit": adapter_exit,
                    "evidence": evidence,
                    "node": g.load_graph(repo_root).get(node_id)}
        summary = (adapter_output.strip().splitlines() or [""])[-1][:2000] if adapter_output.strip() else "completed; verification passed"
        handoff = f"Done. Verification passed. Last output: {summary}"
        if warnings:
            handoff += " Change-policy warnings: " + "; ".join(warnings)
        try:
            node = c.complete_node(
                repo_root, node_id, holder, claim_token,
                handoff_note=handoff, evidence=evidence,
                worktree={"branch": branch, "base_branch": base, "path": str(path)},
                result=result)
        except c.ClaimError:
            return {"outcome": "superseded", "adapter_exit": adapter_exit,
                    "note": "attempt lost ownership at completion; not completing",
                    "node": g.load_graph(repo_root).get(node_id)}
        return {"outcome": "done", "adapter_exit": adapter_exit, "evidence": evidence,
                "result": result, "node": node}
    else:
        try:
            c.fail_node(repo_root, node_id, holder, claim_token,
                        evidence=evidence,
                        error=f"completion command failed (adapter exit={adapter_exit}); see evidence")
        except c.ClaimError:
            return {"outcome": "superseded", "adapter_exit": adapter_exit,
                    "node": g.load_graph(repo_root).get(node_id)}
        return {"outcome": "failed", "adapter_exit": adapter_exit, "evidence": evidence,
                "node": g.load_graph(repo_root).get(node_id)}
