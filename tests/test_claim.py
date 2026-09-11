"""Stage 2: claim protocol tests."""

from datetime import datetime, timedelta, timezone

import pytest

from skein import graph as g
from skein import claim as c


@pytest.fixture
def repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / ".skein").mkdir(exist_ok=True)
    return tmp_path


def add(repo, nid, depends=(), blast=(), **kw):
    g.append_event(repo, "test", "node_added", nid, {
        "title": nid, "intent": {"goal": "g", "context": "c", "constraints": "",
                                 "completion": "true"},
        "depends_on": list(depends), "blast_radius": list(blast)})


def test_dependency_blocks_claim(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    with pytest.raises(c.ClaimError, match="dependency"):
        c.claim_node(repo, "b", "agent-1")
    # complete dep then claim works
    g.append_event(repo, "agent-1", "claimed", "a", {"holder": "agent-1", "ttl_seconds": 60})
    g.append_event(repo, "agent-1", "completed", "a", {"handoff_note": "ok", "evidence": []})
    node = c.claim_node(repo, "b", "agent-1")
    assert node["status"] == "claimed"


def test_blast_radius_overlap_blocks_claim(repo):
    add(repo, "a", blast=["src/auth/**"])
    add(repo, "b", blast=["src/auth/login.py"])
    c.claim_node(repo, "a", "agent-1")
    with pytest.raises(c.ClaimError, match="blast-radius overlap"):
        c.claim_node(repo, "b", "agent-2")


def test_no_overlap_allows_parallel_claim(repo):
    add(repo, "a", blast=["src/auth/**"])
    add(repo, "b", blast=["src/billing/**"])
    c.claim_node(repo, "a", "agent-1")
    node = c.claim_node(repo, "b", "agent-2")
    assert node["claim"]["holder"] == "agent-2"


def test_cas_conflict_rejected(repo):
    add(repo, "a")
    v0 = g.load_graph(repo)["a"]["version"]
    c.claim_node(repo, "a", "agent-1", expected_version=v0)
    # stale version now rejected
    g.append_event(repo, "x", "heartbeat", "a", {})
    v_stale = v0
    with pytest.raises(c.ClaimError, match="version conflict"):
        c.claim_node(repo, "a", "agent-2", expected_version=v_stale)


def test_dead_agent_reaped(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1", ttl_seconds=60)
    # simulate heartbeats stopping: move clock past TTL
    future = datetime.now(timezone.utc) + timedelta(seconds=3600)
    released = c.reap_expired(repo, at=future)
    assert released == ["a"]
    nodes = g.load_graph(repo)
    assert nodes["a"]["status"] == "unclaimed"
    assert "did not complete" in (nodes["a"]["handoff_note"] or "")


def test_heartbeat_renews_lease(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1", ttl_seconds=3600)
    c.heartbeat(repo, "a", "agent-1")
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert c.reap_expired(repo, at=future) == []


def test_force_release_explicit(repo):
    add(repo, "a")
    c.claim_node(repo, "a", "agent-1")
    # non-holder without --force fails
    with pytest.raises(c.ClaimError, match="--force"):
        c.release_node(repo, "a", actor="human", force=False)
    node = c.release_node(repo, "a", actor="human", force=True)
    assert node["status"] == "unclaimed"
    # force-release logged with actor
    events = g.load_events(repo)
    assert events[-1]["type"] == "released" and events[-1]["actor"] == "human"
