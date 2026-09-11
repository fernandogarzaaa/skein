"""Web canvas tests: API round-trips against a live in-process server."""

import json
import subprocess
import threading
import urllib.request
import urllib.error

import pytest

from skein import graph as g
from skein.serve import make_server


@pytest.fixture
def repo(tmp_path, monkeypatch):
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
    return tmp_path


@pytest.fixture
def server(repo):
    srv = make_server(str(repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(method, url, body=None):
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_index_and_graph(server):
    with urllib.request.urlopen(server + "/", timeout=10) as r:
        assert r.status == 200
        assert "Skein Canvas" in r.read().decode()
    code, data = _call("GET", server + "/api/graph")
    assert code == 200 and data["nodes"] == {}


def test_add_edit_node(server, repo):
    code, data = _call("POST", server + "/api/nodes",
                       {"id": "w1", "title": "Web node", "goal": "g",
                        "completion": "true"})
    assert code == 201
    assert data["node"]["title"] == "Web node"
    code, data = _call("POST", server + "/api/nodes/w1", {"title": "Renamed"})
    assert code == 200 and data["outcome"] == "edited"
    assert g.load_graph(str(repo))["w1"]["title"] == "Renamed"
    code, _ = _call("POST", server + "/api/nodes/w1", {"title": "x"})
    assert code == 200
    # validation parity with CLI
    code, data = _call("POST", server + "/api/nodes", {"title": "no id"})
    assert code == 400
    code, data = _call("POST", server + "/api/nodes/nope", {"title": "x"})
    assert code == 404


def test_web_edit_of_claimed_node_interrupts(server, repo):
    _call("POST", server + "/api/nodes",
          {"id": "w2", "title": "T", "goal": "g", "completion": "true"})
    code, _ = _call("POST", server + "/api/nodes/w2/claim", {"holder": "agent-1"})
    assert code == 200
    code, data = _call("POST", server + "/api/nodes/w2", {"title": "human change"})
    assert code == 200 and data["outcome"] == "interrupt"
    assert g.load_graph(str(repo))["w2"]["status"] == "needs_human"


def test_claim_release_conflict(server):
    _call("POST", server + "/api/nodes",
          {"id": "w3", "title": "T", "goal": "g", "completion": "true"})
    assert _call("POST", server + "/api/nodes/w3/claim", {"holder": "a1"})[0] == 200
    code, data = _call("POST", server + "/api/nodes/w3/claim", {"holder": "a2"})
    assert code == 409
    code, _ = _call("POST", server + "/api/nodes/w3/release", {"force": True})
    assert code == 200


def test_events_polling(server):
    _, d0 = _call("GET", server + "/api/events?since=0")
    n0 = d0["count"]
    _call("POST", server + "/api/nodes",
          {"id": "w4", "title": "T", "goal": "g", "completion": "true"})
    code, data = _call("GET", server + f"/api/events?since={n0}")
    assert code == 200
    assert [e["node_id"] for e in data["events"]] == ["w4"]
    assert data["count"] == n0 + 1
