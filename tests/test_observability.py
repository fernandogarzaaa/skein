"""Phase 7: observability - rejected events, timeline/node UI, metrics,
health, status --watch, log --tail/--follow/--rejected, doctor."""

import json
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error

import pytest

from skein import claim as c
from skein import graph as g


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
    from skein.cli import main as skein_main
    assert skein_main(["init"]) == 0
    return tmp_path


@pytest.fixture
def server(repo):
    from skein.serve import make_server
    srv = make_server(str(repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            s = socket.create_connection(
                ("127.0.0.1", srv.server_address[1]), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.02)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def _add_node(repo, nid, **kw):
    from skein.cli import main as skein_main
    argv = ["node", "add", nid, "--title", f"t-{nid}"]
    if "max_retries" in kw:
        argv += ["--max-retries", str(kw["max_retries"])]
    assert skein_main(argv) == 0


# --- rejected events -------------------------------------------------

def test_double_claim_records_rejected(repo):
    _add_node(repo, "a")
    c.claim_node(str(repo), "a", "w1")
    with pytest.raises(c.ClaimError):
        c.claim_node(str(repo), "a", "w2")
    events = [e for e in g.load_events(str(repo)) if e["type"] == "rejected"]
    assert len(events) == 1
    assert events[0]["node_id"] == "a"
    assert events[0]["payload"]["op"] == "claim"
    node = g.load_graph(str(repo))["a"]
    assert node["rejected_count"] == 1
    # the rejection changed nothing about the live attempt
    assert node["status"] == "claimed"
    assert node["claim"]["holder"] == "w1"


def test_stale_token_rejection_recorded(repo):
    _add_node(repo, "a")
    c.claim_node(str(repo), "a", "w1")
    with pytest.raises(c.ClaimError):
        c.heartbeat(str(repo), "a", "w1", claim_token="wrong-token")
    events = [e for e in g.load_events(str(repo)) if e["type"] == "rejected"]
    assert len(events) == 1
    assert events[0]["payload"]["op"] == "heartbeat"
    assert "wrong-token" not in json.dumps(events[0])
    node = g.load_graph(str(repo))["a"]
    assert node["rejected_count"] == 1
    assert node["claim"]["holder"] == "w1"


def test_rejected_event_for_unknown_node_is_informational(repo):
    with pytest.raises(c.ClaimError):
        c.claim_node(str(repo), "nope", "w1")
    events = [e for e in g.load_events(str(repo)) if e["type"] == "rejected"]
    assert len(events) == 1
    assert events[0]["node_id"] == "nope"
    # no node record was created by the rejection
    assert "nope" not in g.load_graph(str(repo))


def test_rejected_bypasses_lifecycle_fencer(repo):
    # a rejected event for a done node still applies (informational)
    _add_node(repo, "a", max_retries=0)
    n = c.claim_node(str(repo), "a", "w1")
    c.fail_node(str(repo), "a", "w1", n["claim"]["claim_token"])
    assert g.load_graph(str(repo))["a"]["status"] == "failed"
    g.append_event(str(repo), "w1", "rejected", "a",
                   {"op": "claim", "reason": "synthetic"})
    assert g.load_graph(str(repo))["a"]["rejected_count"] == 1
    assert g.load_graph(str(repo))["a"]["status"] == "failed"


def test_rejected_event_type_validated():
    assert "rejected" in g.VALID_EVENT_TYPES
    assert "rejected" not in g.LIFECYCLE_TYPES


# --- serve observability endpoints ------------------------------------

def test_health_endpoint(server):
    code, body = _get(server + "/api/health")
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["git"] is True
    assert data["log_writable"] is True
    assert data["worktree_root_writable"] is True
    assert data["version"] == "0.2.0"
    assert data["repo"]


def test_metrics_endpoint_json(server, repo):
    _add_node(repo, "a", max_retries=0)
    n = c.claim_node(str(repo), "a", "w1")
    c.fail_node(str(repo), "a", "w1", n["claim"]["claim_token"])
    code, body = _get(server + "/api/metrics")
    assert code == 200
    data = json.loads(body)
    assert data["nodes_by_status"].get("failed") == 1
    assert data["attempts_by_outcome"].get("failed") == 1
    assert data["total_events"] >= 3
    assert data["uptime_seconds"] >= 0
    assert "claude_code" in data["backends"]
    assert data["backends"]["claude_code"]["failed"] == 1


def test_prometheus_endpoint(server, repo):
    _add_node(repo, "a")
    code, body = _get(server + "/metrics")
    assert code == 200
    assert 'skein_nodes{status="unclaimed"} 1' in body
    assert "skein_events_total" in body
    assert "skein_uptime_seconds" in body
    assert "skein_retries_pending" in body


def test_timeline_page_and_filters(server, repo):
    _add_node(repo, "a")
    _add_node(repo, "b")
    code, body = _get(server + "/timeline")
    assert code == 200
    assert "<table>" in body and "node_added" in body
    code, body = _get(server + "/timeline?node=a")
    assert code == 200
    assert ">a</td>" in body
    assert ">b</td>" not in body
    code, body = _get(server + "/timeline?type=claimed")
    assert code == 200
    assert "node_added" not in body


def test_node_detail_page(server, repo):
    _add_node(repo, "a")
    c.claim_node(str(repo), "a", "w1")
    code, body = _get(server + "/node/a")
    assert code == 200
    assert "Node a" in body
    assert "claimed" in body
    assert "w1" in body
    assert "Attempt history" in body
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server + "/node/nope")
    assert exc.value.code == 404


def test_timeline_escapes_html(repo, server):
    # a title with markup must not break out of the page
    from skein.cli import main as skein_main
    assert skein_main(["node", "add", "x", "--title",
                       "<script>alert(1)</script>"]) == 0
    code, body = _get(server + "/timeline")
    assert code == 200
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# --- CLI observability ------------------------------------------------

def test_status_rejected_column(repo, capsys):
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    capsys.readouterr()  # drain the "added node" print
    c.claim_node(str(repo), "a", "w1")
    with pytest.raises(c.ClaimError):
        c.claim_node(str(repo), "a", "w2")
    assert skein_main(["status"]) == 0
    out = capsys.readouterr().out
    assert "REJ" in out.splitlines()[0]
    row = [l for l in out.splitlines()
           if l.split() and l.split()[0] == "a"][0]
    # fixed-width columns: ID:22 STATUS:12 HOLDER:16 LEASE:14 TRIES:6 REJ:5
    assert row[70:75].strip() == "1"  # REJ column


def test_log_rejected_filter(repo, capsys):
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    capsys.readouterr()  # drain the "added node" print
    c.claim_node(str(repo), "a", "w1")
    with pytest.raises(c.ClaimError):
        c.claim_node(str(repo), "a", "w2")
    assert skein_main(["log", "--rejected"]) == 0
    out = capsys.readouterr().out
    assert out.strip() != ""
    assert all("rejected" in line for line in out.strip().splitlines())


def test_log_tail_and_limit(repo, capsys):
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    capsys.readouterr()  # drain the "added node" print
    assert skein_main(["log", "--tail", "1"]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1


def test_status_watch_exits_on_interrupt(repo, monkeypatch, capsys):
    import skein.cli as cli_mod
    monkeypatch.setattr(cli_mod.time, "sleep",
                        lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    assert skein_main(["status", "--watch"]) == 0
    assert "STATUS" in capsys.readouterr().out


def test_log_follow_streams_new_events(repo, monkeypatch, capsys):
    import skein.cli as cli_mod
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    n = c.claim_node(str(repo), "a", "w1")
    tok = n["claim"]["claim_token"]
    calls = {"n": 0}

    def fake_sleep(s):
        calls["n"] += 1
        if calls["n"] == 1:
            c.release_node(str(repo), "a", "w1", claim_token=tok,
                           note="streamed")
            return
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)
    assert skein_main(["log", "--follow", "--tail", "0"]) == 0
    out = capsys.readouterr().out
    assert "released" in out and "streamed" in out


def test_doctor_healthy(repo, capsys):
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    assert skein_main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert out.count("ok  ") == 6


def test_doctor_bad_config(repo, capsys):
    from skein.cli import main as skein_main
    (repo / ".skein" / "config.json").write_text("{not json")
    assert skein_main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL config valid" in out


def test_doctor_orphaned_worktree(repo, capsys):
    from skein.cli import main as skein_main
    stale = repo / ".skein" / "worktrees" / "stale-dir"
    stale.mkdir(parents=True)
    assert skein_main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL no orphaned worktrees" in out
    assert "skein worktree gc" in out


def test_doctor_expired_lease(repo, capsys):
    from skein.cli import main as skein_main
    _add_node(repo, "a")
    c.claim_node(str(repo), "a", "w1", ttl_seconds=1)
    time.sleep(1.2)
    assert skein_main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL no expired leases" in out
    assert "skein reap" in out


def test_doctor_unknown_config_key(repo, capsys):
    from skein.cli import main as skein_main
    cfg_path = repo / ".skein" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["bogus_key"] = 1
    cfg_path.write_text(json.dumps(cfg))
    assert skein_main(["doctor"]) == 1
    assert "FAIL config valid" in capsys.readouterr().out
