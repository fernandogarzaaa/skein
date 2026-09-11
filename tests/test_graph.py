"""Stage 1: event log + graph reduction tests."""

import json
import subprocess

import pytest

from skein import graph as g


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / ".skein").mkdir(exist_ok=True)
    return tmp_path


def test_append_and_reduce(repo):
    g.append_event(repo, "alice", "node_added", "auth-3", {
        "title": "Auth", "intent": {"goal": "g", "context": "c", "constraints": "-", "completion": "true"},
        "depends_on": [], "blast_radius": ["src/auth/**"]})
    g.append_event(repo, "bob", "claimed", "auth-3", {"holder": "bob", "ttl_seconds": 60})
    nodes = g.load_graph(repo)
    assert nodes["auth-3"]["status"] == "claimed"
    assert nodes["auth-3"]["claim"]["holder"] == "bob"
    assert nodes["auth-3"]["version"] == 2
    # log committed to git
    import subprocess
    r = subprocess.run(["git", "log", "--oneline"], cwd=str(repo), capture_output=True, text=True)
    assert "claimed auth-3" in r.stdout


def test_out_of_order_last_write_wins():
    events = [
        {"timestamp": "2026-01-02T00:00:00+00:00", "actor": "a", "type": "node_added",
         "node_id": "n1", "payload": {"title": "v1"}},
        {"timestamp": "2026-01-01T00:00:00+00:00", "actor": "a", "type": "node_edited",
         "node_id": "n1", "payload": {"title": "older-edit"}},
    ]
    # events appended out of order: earlier-timestamp edit arrives after add;
    # reduction sorts by timestamp so the add (later ts) wins over the stale edit.
    # To assert LWW properly, craft competing edits:
    evs = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "actor": "a", "type": "node_added",
         "node_id": "n1", "payload": {"title": "first"}},
        {"timestamp": "2026-01-03T00:00:00+00:00", "actor": "a", "type": "node_edited",
         "node_id": "n1", "payload": {"title": "new-title"}},
        {"timestamp": "2026-01-02T00:00:00+00:00", "actor": "a", "type": "node_edited",
         "node_id": "n1", "payload": {"title": "stale-title"}},
    ]
    nodes = g.reduce_events(evs)
    assert nodes["n1"]["title"] == "new-title"


def test_graph_json_is_derived(repo):
    g.append_event(repo, "a", "node_added", "n1", {"title": "T"})
    data = json.loads((repo / ".skein" / "graph.json").read_text())
    assert data["n1"]["title"] == "T"
    # removed nodes are hidden from derived view
    g.append_event(repo, "a", "node_removed", "n1", {})
    data = json.loads((repo / ".skein" / "graph.json").read_text())
    assert "n1" not in data


def test_log_commit_scoped_to_skein(repo):
    """Pin: a log commit must contain only .skein paths. A bare
    `git commit` would sweep in unrelated user-staged files (caught
    once in a live walkthrough); this test fails if that regresses."""
    (repo / "user.txt").write_text("user work")
    subprocess.run(["git", "add", "user.txt"], cwd=str(repo),
                   capture_output=True, check=True)
    g.append_event(repo, "alice", "node_added", "n-scope", {"title": "T"})
    r = subprocess.run(["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    files = [line for line in r.stdout.splitlines() if line.strip()]
    assert files, "log commit should contain files"
    assert all(f.startswith(".skein") for f in files), files
    # the user's staged file is still theirs, not committed away
    r = subprocess.run(["git", "show", "HEAD:user.txt"], cwd=str(repo),
                       capture_output=True)
    assert r.returncode != 0
