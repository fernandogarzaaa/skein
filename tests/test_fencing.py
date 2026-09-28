"""Phase 1 regression tests: fencing, state machine, result commits.

Each test pins one of the corrected invariants:
- claims are atomic (one winner under concurrency)
- lifecycle mutations are fenced by attempt tokens
- stale workers cannot complete/heartbeat/release after losing ownership
- the event log survives truncation/corruption
- reduction is deterministic (seq breaks timestamp ties)
- snapshots are validated by log identity
- node IDs cannot escape into paths
- terminal/worker states are never set by direct edit
- completion records a durable result commit used as the downstream base
- dependency graphs reject cycles, self-deps, missing deps
- change policies are enforced at the result checkpoint
"""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skein import graph as g
from skein import claim as c
from skein import ids
from skein import locks
from skein import worktree as wt
from skein.cli import main as skein_main

PY = sys.executable


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path),
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path),
                   capture_output=True)
    (tmp_path / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                   capture_output=True)
    monkeypatch.chdir(tmp_path)
    assert skein_main(["init"]) == 0
    return tmp_path


def add(repo, nid, depends=(), blast=(), completion="true", **kw):
    g.append_event(repo, "test", "node_added", nid, {
        "title": nid, "intent": {"goal": "g", "context": "c", "constraints": "",
                                 "completion": completion},
        "depends_on": list(depends), "blast_radius": list(blast),
        **kw})


class FakeAdapter:
    name = "fake"

    def __init__(self, script):
        self.script = script

    def build_prompt(self, node):
        return "fake"

    def build_command(self, node):
        return [sys.executable, "-c", self.script]


# ---------- atomic claim ----------

def test_simultaneous_claims_one_winner(repo):
    add(repo, "a")
    winners = []
    errors = []

    def go(i):
        try:
            n = c.claim_node(repo, "a", f"agent-{i}")
            winners.append((f"agent-{i}", n["claim"]["claim_token"]))
        except c.ClaimError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1, winners
    assert len(errors) == 7
    node = g.load_graph(repo)["a"]
    assert node["claim"]["holder"] == winners[0][0]
    assert node["claim"]["claim_token"] == winners[0][1]


# ---------- fencing ----------

def test_stale_worker_completion_rejected(repo):
    add(repo, "a")
    n1 = c.claim_node(repo, "a", "agent-1")
    tok1 = n1["claim"]["claim_token"]
    # agent-1 stalls; operator force-releases; agent-2 claims
    c.release_node(repo, "a", actor="human", force=True)
    n2 = c.claim_node(repo, "a", "agent-2")
    tok2 = n2["claim"]["claim_token"]
    assert tok1 != tok2
    # agent-1's late completion is rejected: it lost ownership
    with pytest.raises(c.ClaimError, match="stale attempt"):
        c.complete_node(repo, "a", "agent-1", tok1, handoff_note="late")
    with pytest.raises(c.ClaimError, match="stale attempt"):
        c.fail_node(repo, "a", "agent-1", tok1, error="late")
    with pytest.raises((c.ClaimError, ValueError), match="(?i)stale|mismatch|held by"):
        c.heartbeat(repo, "a", "agent-1", claim_token=tok1)
    # the live attempt still completes fine
    done = c.complete_node(repo, "a", "agent-2", tok2, handoff_note="ok")
    assert done["status"] == "done"
    assert done["handoff_note"] == "ok"  # not agent-1's "late" note


def test_raw_lifecycle_events_rejected_without_token(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1")
    # raw appends that bypass the fenced API are rejected at write time
    with pytest.raises(ValueError, match="stale attempt"):
        g.append_event(repo, "agent-1", "completed", "a",
                       {"handoff_note": "sneaky", "evidence": []})
    with pytest.raises(ValueError, match="stale attempt"):
        g.append_event(repo, "agent-1", "heartbeat", "a", {})
    # ... and rejected at reduction time too (e.g. arriving via sync)
    evs = [{
        "event_id": "x" * 32, "schema_version": 1, "seq": 999,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": "agent-1", "type": "completed", "node_id": "a",
        "payload": {"handoff_note": "sneaky"},
    }]
    nodes, rejected = g.reduce_events_diagnostics(
        g.load_events(repo) + evs)
    assert rejected == 1
    assert nodes["a"]["status"] == "claimed"


def test_reaper_revokes_token_and_stale_complete_fails(repo):
    add(repo, "a")
    n1 = c.claim_node(repo, "a", "agent-1", ttl_seconds=60)
    tok1 = n1["claim"]["claim_token"]
    future = datetime.now(timezone.utc) + timedelta(seconds=3600)
    assert c.reap_expired(repo, at=future) == ["a"]
    assert g.load_graph(repo)["a"]["status"] == "unclaimed"
    # a new attempt takes over; the reaped attempt's token is dead
    n2 = c.claim_node(repo, "a", "agent-2")
    assert n2["claim"]["claim_token"] != tok1
    with pytest.raises(c.ClaimError, match="stale attempt"):
        c.complete_node(repo, "a", "agent-1", tok1, handoff_note="late")


def test_reaper_refuses_unexpired_lease(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1", ttl_seconds=3600)
    # a forged reaper-style release for a live lease is rejected
    with pytest.raises(ValueError, match="not expired"):
        g.append_event(repo, "reaper", "released", "a",
                       {"note": "forged", "expired": True})
    assert g.load_graph(repo)["a"]["status"] == "claimed"


# ---------- log durability ----------

def test_reaper_release_scoped_to_reaped_attempt(repo):
    # A delayed duplicate of the reaper's release (as it could arrive via
    # sync) targets agent-1's attempt; it must not wipe agent-2's live
    # attempt.
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1", ttl_seconds=60)
    future = datetime.now(timezone.utc) + timedelta(seconds=3600)
    assert c.reap_expired(repo, at=future) == ["a"]
    c.claim_node(repo, "a", "agent-2")
    events = g.load_events(repo)
    reaper_ev = next(e for e in events if e["type"] == "released")
    assert reaper_ev["payload"]["reaped_holder"] == "agent-1"
    late_copy = dict(reaper_ev)
    late_copy["event_id"] = "e" * 32
    late_copy["timestamp"] = datetime.now(timezone.utc).isoformat()
    nodes, rejected = g.reduce_events_diagnostics(events + [late_copy])
    assert rejected == 1
    assert nodes["a"]["status"] == "claimed"
    assert nodes["a"]["claim"]["holder"] == "agent-2"


def test_truncated_and_malformed_lines_recovered(repo):
    add(repo, "a")
    add(repo, "b")
    lp = g.log_path(repo)
    with lp.open("a", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write('{"type": "claimed"}\n')  # bad shape
        f.write('{"type": "node_added", "node_id": "c", "payload": {"title": "x"}')  # truncated
    events, skipped = g.load_events_diagnostics(repo)
    assert skipped == 3
    nodes = g.load_graph(repo)
    assert set(nodes) == {"a", "b"}  # repo still loads


def test_same_timestamp_reduction_is_deterministic(repo):
    add(repo, "a")
    node = c.claim_node(repo, "a", "agent-1")
    tok = node["claim"]["claim_token"]
    ts = "2026-09-28T00:00:00+00:00"
    # take the real node_added + claimed events, pin them to one timestamp,
    # renumber seqs; reduction must order claimed before completed by seq
    # regardless of input order
    added, claimed = g.load_events(repo)[0], g.load_events(repo)[1]
    added = dict(added); added.update({"timestamp": ts, "seq": 49})
    claimed = dict(claimed); claimed.update({"timestamp": ts, "seq": 50})
    completed = {
        "event_id": "f" * 32, "schema_version": 1, "seq": 51,
        "timestamp": ts, "actor": "agent-1", "type": "completed",
        "node_id": "a",
        "payload": {"handoff_note": "ok", "claim_token": tok,
                    "evidence": []},
    }
    for order in ([added, claimed, completed], [completed, claimed, added]):
        nodes, rejected = g.reduce_events_diagnostics(order)
        assert rejected == 0, order
        assert nodes["a"]["status"] == "done"


def test_snapshot_staleness_detected(repo):
    add(repo, "a")
    g.load_graph(repo)  # build snapshot
    # corrupt the derived snapshot: loader must rebuild from the log
    g.graph_path(repo).write_text('{"bogus": true}', encoding="utf-8")
    nodes = g.load_graph(repo)
    assert "a" in nodes and nodes["a"]["status"] == "unclaimed"
    # tamper with the log behind the snapshot's back
    with g.log_path(repo).open("a", encoding="utf-8") as f:
        f.write('{"oops": 1}\n')
    nodes = g.load_graph(repo)
    assert "a" in nodes  # rebuilt, malformed line skipped


# ---------- node id safety ----------

@pytest.mark.parametrize("bad", [
    "../evil", "..", "/abs/path", "a/b", "a\\b", "", "a;b", "a b",
    "a\nb", ".", "x" * 200,
    "semi;colon", "dollar$ign", "back`tick",
])
def test_malicious_node_ids_rejected(repo, bad):
    with pytest.raises(ids.NodeIdError):
        ids.validate_node_id(bad)
    with pytest.raises((ids.NodeIdError, ValueError)):
        add(repo, bad)


def test_base_branch_never_silently_substitutes(repo):
    # a node whose dependency record is missing must raise, not branch
    # from the ambient checkout
    g.append_event(repo, "test", "node_added", "b", {
        "title": "b",
        "intent": {"goal": "g", "context": "c", "constraints": "",
                   "completion": "true"},
        "depends_on": ["ghost"], "blast_radius": []})
    with pytest.raises(wt.BaseCommitUnavailable):
        wt.base_branch_for(repo, "b")
    with pytest.raises(wt.BaseCommitUnavailable):
        wt.base_branch_for(repo, "no-such-node")


def test_node_id_cannot_escape_worktree_root(repo):
    p = ids.worktree_path_for(repo, "ok-id")
    assert str(p).startswith(str(Path(repo) / ".skein" / "worktrees"))
    with pytest.raises(ids.NodeIdError):
        ids.worktree_path_for(repo, "../../etc")


# ---------- centralized validation ----------

def test_illegal_direct_terminal_status_rejected(repo):
    add(repo, "a")
    assert skein_main(["node", "edit", "a", "--status", "done"]) != 0
    assert skein_main(["node", "edit", "a", "--status", "failed"]) != 0
    assert skein_main(["node", "edit", "a", "--status", "claimed"]) != 0
    assert g.load_graph(repo)["a"]["status"] == "unclaimed"
    # legal manual transitions still work
    assert skein_main(["node", "edit", "a", "--status", "blocked"]) == 0
    assert g.load_graph(repo)["a"]["status"] == "blocked"


def test_dependency_validation(repo):
    add(repo, "a")
    # missing dep
    assert skein_main(["node", "add", "b", "--depends-on", "nope"]) != 0
    # self dep
    assert skein_main(["node", "add", "s", "--depends-on", "s"]) != 0
    # cycle a -> b -> a
    assert skein_main(["node", "add", "b", "--depends-on", "a"]) == 0
    assert skein_main(["node", "edit", "a", "--depends-on", "b"]) != 0
    assert g.load_graph(repo)["a"]["depends_on"] == []


def test_delete_with_dependents_rejected(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    assert skein_main(["node", "edit", "a", "--delete"]) != 0
    assert "a" in g.load_graph(repo)
    # deleting the leaf works
    assert skein_main(["node", "edit", "b", "--delete"]) == 0
    assert "b" not in g.load_graph(repo)


# ---------- result commits + change policy ----------

def test_run_records_durable_result_commit(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--title", "T", "--goal", "write",
                       "--completion", f"{PY} -c \"import pathlib; raise SystemExit(0 if pathlib.Path('hello.txt').exists() else 1)\""]) == 0
    adapter = FakeAdapter("open('hello.txt','w').write('hi')")
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    node = g.load_graph(repo)["n1"]
    res = node["result"]
    assert res and len(res["commit"]) == 40
    assert res["base_commit"] and len(res["base_commit"]) == 40
    assert "hello.txt" in res["changed_files"]
    # the commit really exists in the repo
    r = subprocess.run(["git", "cat-file", "-t", res["commit"]],
                       cwd=str(repo), capture_output=True, text=True)
    assert r.stdout.strip() == "commit"
    # downstream branches from the result commit, not the branch name
    assert skein_main(["node", "add", "n2", "--depends-on", "n1",
                       "--completion", "true"]) == 0
    node2 = c.claim_node(repo, "n2", "agent-2")
    assert node2["claim"]["base_branch"] == res["commit"]


def test_strict_change_policy_fails_out_of_scope_changes(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--blast-radius", "src/allowed/",
                       "--change-policy", "strict",
                       "--completion", "true"]) == 0
    adapter = FakeAdapter("open('elsewhere.txt','w').write('oops')")
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "failed"
    node = g.load_graph(repo)["n1"]
    assert node["status"] == "failed"
    assert "strict" in (node["handoff_note"] or "")
    assert node["result"] is None  # no durable result for rejected work


def test_warn_change_policy_records_warnings(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--blast-radius", "src/allowed/",
                       "--change-policy", "warn",
                       "--completion", "true"]) == 0
    adapter = FakeAdapter("open('elsewhere.txt','w').write('oops')")
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    node = g.load_graph(repo)["n1"]
    assert node["status"] == "done"
    assert "outside the declared blast radius" in (node["handoff_note"] or "")


def test_superseded_run_does_not_complete(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--completion", "true"]) == 0
    adapter = FakeAdapter("import time; time.sleep(5)")
    outcome = {}

    def target():
        outcome.update(run_node(repo, "n1", "agent-1", adapter=adapter,
                                heartbeat_interval=0.2, poll_interval=0.05))

    t = threading.Thread(target=target)
    t.start()
    for _ in range(100):
        n = g.load_graph(repo).get("n1")
        if n and n["status"] in ("claimed", "in_progress"):
            break
        time.sleep(0.05)
    # ownership moves on while agent-1's backend is still running
    c.release_node(repo, "n1", actor="human", force=True,
                   note="operator takeover")
    c.claim_node(repo, "n1", "agent-2")
    t.join(timeout=30)
    assert not t.is_alive()
    assert outcome.get("outcome") == "superseded"
    node = g.load_graph(repo)["n1"]
    assert node["status"] == "claimed"
    assert node["claim"]["holder"] == "agent-2"


def test_interrupt_during_verification_aborts(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--completion",
                       f"{PY} -c \"import time; time.sleep(5)\""]) == 0
    adapter = FakeAdapter("pass")
    outcome = {}

    def target():
        outcome.update(run_node(repo, "n1", "agent-1", adapter=adapter,
                                heartbeat_interval=0.2, poll_interval=0.05))

    t = threading.Thread(target=target)
    t.start()
    # wait until verification starts (adapter done, node still claimed),
    # then interrupt mid-verification
    time.sleep(1.5)
    g.append_event(repo, "human", "human_interrupt", "n1",
                   {"action": "edit", "fields": {"title": "stop"}})
    t.join(timeout=30)
    assert not t.is_alive()
    assert outcome.get("outcome") == "interrupted"
    node = g.load_graph(repo)["n1"]
    assert node["status"] == "needs_human"
    assert node["result"] is None


# ---------- lock + init hygiene ----------

def test_lock_file_lives_under_git_and_is_not_committed(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1")
    lp = locks.lock_path(repo)
    assert str(lp).startswith(str(Path(repo) / ".git"))
    assert lp.exists()
    assert not (Path(repo) / ".skein" / "skein.lock").exists()
    tracked = subprocess.run(["git", "ls-files"], cwd=str(repo),
                             capture_output=True, text=True).stdout
    assert "skein.lock" not in tracked


def test_init_does_not_overwrite_git_identity(repo):
    email = subprocess.run(["git", "config", "user.email"], cwd=str(repo),
                           capture_output=True, text=True).stdout.strip()
    name = subprocess.run(["git", "config", "user.name"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()
    assert email == "t@t" and name == "t"
    assert skein_main(["init"]) == 0
    email2 = subprocess.run(["git", "config", "user.email"], cwd=str(repo),
                            capture_output=True, text=True).stdout.strip()
    name2 = subprocess.run(["git", "config", "user.name"], cwd=str(repo),
                           capture_output=True, text=True).stdout.strip()
    assert (email2, name2) == (email, name)


def test_heartbeats_do_not_spam_git_commits(repo):
    add(repo, "a")
    node = c.claim_node(repo, "a", "agent-1")
    tok = node["claim"]["claim_token"]
    before = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                            cwd=str(repo), capture_output=True,
                            text=True).stdout.strip()
    for _ in range(5):
        c.heartbeat(repo, "a", "agent-1", claim_token=tok)
    after = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                           cwd=str(repo), capture_output=True,
                           text=True).stdout.strip()
    assert before == after
