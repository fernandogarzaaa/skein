"""Phase 4: retry policy, attempt history, backoff, run --all scheduler."""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import timedelta

import pytest

from skein import graph as g
from skein import claim as c
from skein.edits import add_node, edit_node

PY = sys.executable


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / ".skein").mkdir(exist_ok=True)
    return tmp_path


def add(repo, nid, **kw):
    kw.setdefault("completion", "true")
    return add_node(repo, "test", nid, **kw)


def claim_fail(repo, nid, holder="agent-1", **kw):
    node = c.claim_node(repo, nid, holder)
    tok = node["claim"]["claim_token"]
    return c.fail_node(repo, nid, holder, tok, **kw)


def last_failed_event(repo):
    events = [e for e in g.load_events(repo) if e["type"] == "failed"]
    assert events, "no failed event found"
    return events[-1]


def test_retry_policy_defaults(repo):
    node = add(repo, "a")
    assert node["max_retries"] == 3
    assert node["retry_backoff_seconds"] == 60.0
    assert node["retry_at"] is None
    assert node["attempts"] == []
    assert node["attempts_used"] == 0


def test_retry_policy_defaults_from_config(repo):
    (g.config_path(repo)).write_text(json.dumps({
        "default_max_retries": 5, "default_retry_backoff_seconds": 10}))
    node = add(repo, "a")
    assert node["max_retries"] == 5
    assert node["retry_backoff_seconds"] == 10.0


def test_retry_policy_explicit_and_edit(repo):
    node = add(repo, "a", max_retries=1, retry_backoff_seconds=2)
    assert node["max_retries"] == 1
    assert node["retry_backoff_seconds"] == 2.0
    assert edit_node(repo, "test", "a", {"max_retries": 4,
                                        "retry_backoff_seconds": 30}) == "edited"
    node = g.load_graph(repo)["a"]
    assert node["max_retries"] == 4
    assert node["retry_backoff_seconds"] == 30.0


def test_retry_policy_validation(repo):
    with pytest.raises(ValueError, match="max_retries"):
        add(repo, "a", max_retries=-1)
    with pytest.raises(ValueError, match="retry_backoff"):
        add(repo, "b", retry_backoff_seconds=-5)
    add(repo, "c")
    with pytest.raises(ValueError, match="max_retries"):
        edit_node(repo, "test", "c", {"max_retries": -2})
    with pytest.raises(ValueError, match="retry_backoff"):
        edit_node(repo, "test", "c", {"retry_backoff_seconds": -1})


def test_fail_parks_unclaimed_with_backoff(repo):
    add(repo, "a", max_retries=1, retry_backoff_seconds=3600)
    node = claim_fail(repo, "a", error="boom")
    assert node["status"] == "unclaimed"
    assert node["retry_at"], "retry_at must be set while backing off"
    ra = c.parse_ts(node["retry_at"])
    assert ra > c.now_utc()
    assert node["attempts_used"] == 0
    assert len(node["attempts"]) == 1
    entry = node["attempts"][0]
    assert set(entry) == {"attempt_id", "holder", "started_at",
                          "ended_at", "outcome", "error"}
    assert entry["outcome"] == "failed"
    assert entry["holder"] == "agent-1"
    assert entry["error"] == "boom"
    assert entry["started_at"] and entry["ended_at"]
    ev = last_failed_event(repo)
    assert ev["payload"]["retry"] is True
    assert ev["payload"]["attempts_used"] == 0
    # first backoff is ~base with +/-25% jitter
    assert 0.75 * 3600 <= ev["payload"]["backoff_seconds"] <= 1.25 * 3600


def test_backoff_blocks_eligibility_then_elapses(repo, monkeypatch):
    add(repo, "a", max_retries=1, retry_backoff_seconds=3600)
    claim_fail(repo, "a")
    ok, reason = c.eligibility(repo, "a")
    assert not ok
    assert reason.startswith("backoff until ")
    # no sleeping in tests: travel past the deadline instead
    future = c.now_utc() + timedelta(hours=2)
    monkeypatch.setattr(c, "now_utc", lambda: future)
    ok, reason = c.eligibility(repo, "a")
    assert ok, reason
    node = c.claim_node(repo, "a", "agent-1")
    assert node["status"] == "claimed"
    assert node["retry_at"] is None  # claim consumes the backoff
    tok = node["claim"]["claim_token"]
    # second failure exhausts max_retries=1 -> parks at failed
    monkeypatch.undo()
    node = c.fail_node(repo, "a", "agent-1", tok, error="again")
    assert node["status"] == "failed"
    assert node["retry_at"] is None
    assert node["attempts_used"] == 1
    assert len(node["attempts"]) == 2
    ev = last_failed_event(repo)
    assert ev["payload"]["retry"] is False


def test_max_retries_zero_parks_immediately(repo):
    add(repo, "a", max_retries=0)
    node = claim_fail(repo, "a", error="nope")
    assert node["status"] == "failed"
    assert node["retry_at"] is None
    assert node["attempts_used"] == 0
    assert len(node["attempts"]) == 1
    assert last_failed_event(repo)["payload"]["retry"] is False


def test_timeout_outcome_is_retryable(repo):
    add(repo, "a", max_retries=2, retry_backoff_seconds=3600)
    node = claim_fail(repo, "a", error="timed out", outcome="timeout")
    assert node["status"] == "unclaimed"
    assert node["retry_at"] is not None
    assert node["attempts"][0]["outcome"] == "timeout"


def test_fail_node_rejects_unknown_outcome(repo):
    add(repo, "b")
    node = c.claim_node(repo, "b", "agent-1")
    tok = node["claim"]["claim_token"]
    with pytest.raises(c.ClaimError, match="unknown failure outcome"):
        c.fail_node(repo, "b", "agent-1", tok, outcome="exploded")
    # the rejected call changed nothing
    assert g.load_graph(repo)["b"]["status"] == "claimed"


def test_backoff_is_exponential(repo, monkeypatch):
    add(repo, "a", max_retries=5, retry_backoff_seconds=60)
    t0 = c.now_utc()
    claim_fail(repo, "a")
    first = last_failed_event(repo)["payload"]["backoff_seconds"]
    assert 0.75 * 60 <= first <= 1.25 * 60
    # travel past the first backoff, fail again: ~2*base now
    monkeypatch.setattr(c, "now_utc", lambda: t0 + timedelta(hours=2))
    node = c.claim_node(repo, "a", "agent-1")
    tok = node["claim"]["claim_token"]
    monkeypatch.undo()
    c.fail_node(repo, "a", "agent-1", tok, error="again")
    second = last_failed_event(repo)["payload"]["backoff_seconds"]
    assert 0.75 * 120 <= second <= 1.25 * 120


def test_attempt_history_bounded(repo):
    add(repo, "a", max_retries=100, retry_backoff_seconds=0)
    first_ids = []
    for _ in range(25):
        node = c.claim_node(repo, "a", "agent-1")
        if not first_ids:
            first_ids.append(node["claim"]["attempt_id"])
        tok = node["claim"]["claim_token"]
        c.fail_node(repo, "a", "agent-1", tok, error="x")
    node = g.load_graph(repo)["a"]
    assert len(node["attempts"]) == g.MAX_ATTEMPT_HISTORY
    assert node["attempts"][0]["attempt_id"] != first_ids[0]
    for entry in node["attempts"]:
        assert set(entry) == {"attempt_id", "holder", "started_at",
                              "ended_at", "outcome", "error"}


def test_human_interrupt_not_retryable_and_recorded(repo):
    add(repo, "a", max_retries=3, retry_backoff_seconds=3600)
    c.claim_node(repo, "a", "agent-1")
    assert edit_node(repo, "human", "a", {"title": "changed"}) == "interrupt"
    node = g.load_graph(repo)["a"]
    assert node["status"] == "needs_human"
    assert node["retry_at"] is None
    assert len(node["attempts"]) == 1
    assert node["attempts"][0]["outcome"] == "interrupted"
    # a human-cleared node is immediately claimable: interrupts never
    # consume retries and never schedule backoff
    assert edit_node(repo, "human", "a", {"status": "unclaimed"}) == "edited"
    ok, reason = c.eligibility(repo, "a")
    assert ok, reason


def test_completed_and_release_record_attempts(repo):
    add(repo, "a")
    node = c.claim_node(repo, "a", "agent-1")
    tok = node["claim"]["claim_token"]
    node = c.complete_node(repo, "a", "agent-1", tok, handoff_note="ok")
    assert node["attempts"][0]["outcome"] == "done"
    assert node["attempts"][0]["ended_at"]
    add(repo, "b")
    node = c.claim_node(repo, "b", "agent-1")
    tok = node["claim"]["claim_token"]
    node = c.release_node(repo, "b", actor="agent-1", claim_token=tok)
    assert node["status"] == "unclaimed"
    assert node["attempts"][0]["outcome"] == "released"


def test_reaped_attempt_recorded(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1", ttl_seconds=60)
    released = c.reap_expired(repo, at=c.now_utc() + timedelta(hours=2))
    assert released == ["a"]
    node = g.load_graph(repo)["a"]
    assert node["attempts"][0]["outcome"] == "reaped"
    assert node["retry_at"] is None


def test_legacy_failed_event_still_parks_failed(repo):
    # events written before the retry fields existed carry no retry
    # decision: they reduce to terminal failed, as before
    g.append_event(repo, "t", "node_added", "old",
                   {"title": "old",
                    "intent": {"goal": "g", "context": "", "constraints": "",
                               "completion": "true"}})
    g.append_event(repo, "t", "claimed", "old",
                   {"holder": "h", "ttl_seconds": 60})
    g.append_event(repo, "t", "failed", "old",
                   {"evidence": [], "error": "boom"})
    node = g.load_graph(repo)["old"]
    assert node["status"] == "failed"
    assert node["max_retries"] == 3  # pre-policy default
    assert node["retry_backoff_seconds"] == 60.0
    # the legacy tokenless claim has no attempt_id, so no history entry
    # can be attributed to it
    assert node["attempts"] == []


# ---------- scheduler ----------

@pytest.fixture
def cli_repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    monkeypatch.chdir(tmp_path)
    from skein.cli import main as skein_main
    assert skein_main(["init"]) == 0
    # stub backends: okstub exits 0, failstub exits 1.
    # Extensionless shell scripts are not executable on Windows
    # (WinError 193), so use .bat stubs there; CreateProcess runs
    # those via cmd.
    if os.name == "nt":
        ok_stub = tmp_path / "ok-stub.bat"
        ok_stub.write_text("@echo off\r\nexit /b 0\r\n")
        fail_stub = tmp_path / "fail-stub.bat"
        fail_stub.write_text("@echo off\r\nexit /b 1\r\n")
    else:
        ok_stub = tmp_path / "ok-stub"
        ok_stub.write_text("#!/bin/sh\nexit 0\n")
        fail_stub = tmp_path / "fail-stub"
        fail_stub.write_text("#!/bin/sh\nexit 1\n")
        ok_stub.chmod(0o755)
        fail_stub.chmod(0o755)
    assert skein_main(["backends", "add", "--name", "okstub",
                       "--binary", str(ok_stub)]) == 0
    assert skein_main(["backends", "add", "--name", "failstub",
                       "--binary", str(fail_stub)]) == 0
    return tmp_path, str(fail_stub)


def test_run_all_retries_then_parks_and_blocks_dependent(cli_repo, capsys):
    from skein.cli import main as skein_main
    repo, fail_stub = cli_repo
    assert skein_main(["node", "add", "s1", "--backend", "failstub",
                       "--completion", fail_stub,
                       "--max-retries", "1", "--retry-backoff-seconds", "1"]) == 0
    assert skein_main(["node", "add", "s2", "--backend", "okstub",
                       "--depends-on", "s1", "--completion", "true"]) == 0
    assert skein_main(["node", "add", "s3", "--backend", "okstub",
                       "--completion", "true"]) == 0
    rc = skein_main(["run", "--all", "--max-parallel", "2"])
    assert rc == 2  # s1 failed
    out = capsys.readouterr().out
    nodes = g.load_graph(str(repo))
    # s1 retried once after backoff, then parked at failed
    assert nodes["s1"]["status"] == "failed"
    assert len(nodes["s1"]["attempts"]) == 2
    assert nodes["s1"]["attempts_used"] == 1
    # s3 (independent) ran and completed
    assert nodes["s3"]["status"] == "done"
    # s2 depended on s1 and never ran
    assert nodes["s2"]["status"] == "unclaimed"
    assert nodes["s2"]["attempts"] == []
    # summary table printed
    assert "s1" in out and "failed" in out
    assert "s3" in out and "done" in out
    assert "not run:" in out and "s2" in out


def test_run_all_max_nodes(cli_repo, capsys):
    from skein.cli import main as skein_main
    repo, _ = cli_repo
    assert skein_main(["node", "add", "m1", "--backend", "okstub",
                       "--completion", "true"]) == 0
    assert skein_main(["node", "add", "m2", "--backend", "okstub",
                       "--completion", "true"]) == 0
    rc = skein_main(["run", "--all", "--max-nodes", "1"])
    assert rc == 0
    nodes = g.load_graph(str(repo))
    done = [nid for nid, n in nodes.items() if n["status"] == "done"]
    waiting = [nid for nid, n in nodes.items() if n["status"] == "unclaimed"]
    assert len(done) == 1 and len(waiting) == 1
    out = capsys.readouterr().out
    assert "ran 1 node(s)" in out


def test_run_all_multi_dep_runs_integration_node(cli_repo, capsys):
    # the scheduler mirrors run_node's integration pre-step: a
    # multi-dependency node gets its __integrate node (deterministic
    # auto-merge of the two parent branches) before eligibility
    from skein.cli import main as skein_main
    repo, _ = cli_repo
    assert skein_main(["node", "add", "p1", "--backend", "okstub",
                       "--completion", "true"]) == 0
    assert skein_main(["node", "add", "p2", "--backend", "okstub",
                       "--completion", "true"]) == 0
    assert skein_main(["node", "add", "kid", "--backend", "okstub",
                       "--depends-on", "p1,p2", "--completion", "true"]) == 0
    rc = skein_main(["run", "--all", "--max-parallel", "2"])
    assert rc == 0
    nodes = g.load_graph(str(repo))
    assert nodes["p1"]["status"] == "done"
    assert nodes["p2"]["status"] == "done"
    assert nodes["kid__integrate"]["status"] == "done"
    assert nodes["kid"]["status"] == "done"


def test_run_needs_id_or_all(cli_repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["run"]) == 1
    assert "node id required" in capsys.readouterr().err


def test_run_all_rejects_backend_override(cli_repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["run", "--all", "--backend", "okstub"]) == 1
    assert "cannot use --backend with --all" in capsys.readouterr().err


# ---------- web API parity ----------

def _call(method, url, body=None):
    import json as _json
    data = _json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, _json.loads(e.read() or b"{}")


@pytest.fixture
def server(cli_repo):
    from skein.serve import make_server
    repo, _ = cli_repo
    srv = make_server(str(repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", repo
    srv.shutdown()


def test_serve_retry_policy_roundtrip(server):
    base, repo = server
    code, data = _call("POST", base + "/api/nodes",
                       {"id": "w9", "title": "T", "goal": "g",
                        "completion": "true", "max_retries": 2,
                        "retry_backoff_seconds": 5})
    assert code == 201
    assert data["node"]["max_retries"] == 2
    assert data["node"]["retry_backoff_seconds"] == 5.0
    # retry state visible in the graph/status payload
    code, data = _call("GET", base + "/api/graph")
    assert code == 200
    w9 = data["nodes"]["w9"]
    assert w9["max_retries"] == 2
    assert w9["retry_at"] is None
    assert w9["attempts"] == []
    # edit path shares the CLI validation
    code, data = _call("POST", base + "/api/nodes/w9", {"max_retries": 7})
    assert code == 200
    assert g.load_graph(str(repo))["w9"]["max_retries"] == 7
    code, data = _call("POST", base + "/api/nodes/w9", {"max_retries": -1})
    assert code == 400
