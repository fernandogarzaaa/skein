"""Phase 5: shipping lifecycle - ship, ship --all, conflict parking, release."""

import json
import subprocess
import threading
import urllib.request
import urllib.error

import pytest

from skein import graph as g
from skein import claim as c
from skein import worktree as wt
from skein import shipping as sh
from skein.serve import make_server


def git(repo, *args):
    r = subprocess.run(["git"] + list(args), cwd=str(repo),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    (tmp_path / ".skein").mkdir(exist_ok=True)
    return tmp_path


def add(repo, nid, depends=()):
    g.append_event(repo, "test", "node_added", nid, {
        "title": nid, "intent": {"goal": "g", "context": "c", "constraints": "",
                                "completion": "true"},
        "depends_on": list(depends), "blast_radius": []})


def complete_with_file(repo, nid, filename, content, holder="a1"):
    """Claim, write one file in the node's worktree, commit it there, and
    complete with a real result record. Returns the result commit SHA."""
    node = c.claim_node(repo, nid, holder)
    token = node["claim"]["claim_token"]
    path, branch, base = wt.ensure_worktree(repo, nid)
    (path / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", f"{nid} work"], cwd=str(path),
                   capture_output=True)
    result_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path),
        capture_output=True, text=True).stdout.strip()
    base_sha = g.load_graph(repo)[nid]["worktree"]["base_commit"]
    c.complete_node(repo, nid, holder, token,
                    handoff_note="ok", evidence=[],
                    worktree={"branch": branch, "base_branch": base,
                              "path": str(path)},
                    result={"base_commit": base_sha, "commit": result_commit,
                            "changed_files": [filename],
                            "diff_stats": {filename: {"added": 1,
                                                     "deleted": 0}}})
    return result_commit


def shipped_events(repo, nid=None):
    evs = [e for e in g.load_events(repo) if e.get("type") == "shipped"]
    if nid:
        evs = [e for e in evs if e.get("node_id") == nid]
    return evs


def test_ship_merges_result_commit_to_main(repo):
    add(repo, "a")
    result = complete_with_file(repo, "a", "feat.txt", "hello\n")
    r = sh.ship_node(repo, "a")
    assert r["status"] == "shipped"
    assert r["target"] == "main"
    assert not r["diverged"]
    # default merge is --no-ff with the skein message
    msg = git(repo, "log", "-1", "--format=%s",
              r["merge_commit"]).stdout.strip()
    assert msg == f"skein: ship a ({result[:8]})"
    parents = git(repo, "rev-list", "--parents", "-1",
                  r["merge_commit"]).stdout.strip().split()
    assert len(parents) == 3  # merge commit, not a fast-forward
    assert (repo / "feat.txt").read_text() == "hello\n"
    assert git(repo, "merge-base", "--is-ancestor",
               result, "main").returncode == 0
    # shipped event recorded and derived into node state
    evs = shipped_events(repo, "a")
    assert len(evs) == 1
    assert evs[0]["payload"]["merge_commit"] == r["merge_commit"]
    node = g.load_graph(repo)["a"]
    assert node["shipped"]["main"]["merge_commit"] == r["merge_commit"]
    assert node["shipped"]["main"]["result_commit"] == result


def test_ship_is_idempotent(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    first = sh.ship_node(repo, "a")
    second = sh.ship_node(repo, "a")
    assert second["status"] == "already-shipped"
    assert second["result_commit"] == first["result_commit"]
    # no second merge commit, no second event
    count = git(repo, "log", "--oneline", "main").stdout.count("skein: ship a")
    assert count == 1
    assert len(shipped_events(repo, "a")) == 1


def test_ship_refuses_bad_states(repo):
    add(repo, "a")
    with pytest.raises(sh.ShipError, match="not done"):
        sh.ship_node(repo, "a")
    with pytest.raises(sh.ShipError, match="unknown node"):
        sh.ship_node(repo, "nope")


def test_ship_refuses_dead_result_ref(repo):
    add(repo, "a")
    node = c.claim_node(repo, "a", "h1")
    token = node["claim"]["claim_token"]
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    c.complete_node(repo, "a", "h1", token, handoff_note="ok",
                    result={"base_commit": base, "commit": "0" * 40,
                            "changed_files": [], "diff_stats": {}})
    with pytest.raises(wt.BaseCommitUnavailable, match="dead ref"):
        sh.ship_node(repo, "a")


def test_ship_divergence_requires_force(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    # target moves in a real file after the node ran
    (repo / "other.txt").write_text("moved on\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unrelated work")
    with pytest.raises(sh.ShipError, match="--force"):
        sh.ship_node(repo, "a")
    r = sh.ship_node(repo, "a", force=True)
    assert r["status"] == "shipped"
    assert r["diverged"] is True
    ev = shipped_events(repo, "a")[0]["payload"]
    assert ev["diverged"] is True and ev["forced"] is True
    assert (repo / "feat.txt").read_text() == "hello\n"


def test_ship_to_other_branch_uses_throwaway_worktree(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    git(repo, "branch", "staging")
    r = sh.ship_node(repo, "a", target="staging")
    assert r["status"] == "shipped" and r["target"] == "staging"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert not (repo / "feat.txt").exists()  # main untouched
    assert git(repo, "show", "staging:feat.txt").stdout == "hello\n"
    # no leftover worktree registrations
    assert "skein-ship-" not in git(repo, "worktree", "list").stdout


def test_ship_ff_only(repo):
    add(repo, "a")
    # pin the staging branch before the run's .skein churn: the result
    # will be a strict descendant, so the merge is a true fast-forward
    git(repo, "branch", "staging")
    result = complete_with_file(repo, "a", "feat.txt", "hello\n")
    r = sh.ship_node(repo, "a", target="staging", ff_only=True)
    assert r["status"] == "shipped"
    assert r["merge_commit"] == result  # fast-forwarded, no merge commit
    assert git(repo, "rev-parse", "staging").stdout.strip() == result


def test_ship_ff_only_fails_when_not_fast_forwardable(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    (repo / "other.txt").write_text("moved on\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unrelated work")
    with pytest.raises(sh.ShipError, match="cannot fast-forward"):
        sh.ship_node(repo, "a", ff_only=True, force=True)


def test_ship_conflict_reports_files_and_aborts(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "app.txt", "from-a\n")
    (repo / "app.txt").write_text("from-main\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main moved")
    with pytest.raises(sh.ShipError, match="app.txt"):
        sh.ship_node(repo, "a", force=True)
    # merge was aborted: no half-merged state, no merge commit
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert "skein: ship a" not in git(repo, "log", "--oneline").stdout
    assert len(shipped_events(repo, "a")) == 0


def force_done(repo, nid, filename, content):
    """Mark a node done with a real result commit, bypassing claim: for
    nodes whose dependency base cannot be resolved through the normal
    path (e.g. the dep records a dead result ref)."""
    import tempfile
    import uuid
    from pathlib import Path
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", f"skein/{nid}", "HEAD")
    tmp = Path(tempfile.mkdtemp(prefix="skein-force-"))
    try:
        git(repo, "worktree", "add", str(tmp), f"skein/{nid}")
        (tmp / filename).write_text(content)
        git(tmp, "add", ".")
        git(tmp, "commit", "-m", f"{nid} work")
        commit = git(tmp, "rev-parse", "HEAD").stdout.strip()
    finally:
        git(repo, "worktree", "remove", "--force", str(tmp))
        git(repo, "worktree", "prune")
    aid, tok = uuid.uuid4().hex, uuid.uuid4().hex
    g.append_event(repo, "test", "claimed", nid,
                   {"holder": "h", "attempt_id": aid, "claim_token": tok,
                    "ttl_seconds": 60})
    g.append_event(repo, "test", "completed", nid,
                   {"handoff_note": "ok", "evidence": [],
                    "attempt_id": aid, "claim_token": tok,
                    "result": {"base_commit": base, "commit": commit,
                               "changed_files": [filename],
                               "diff_stats": {filename: {"added": 1,
                                                        "deleted": 0}}}})
    return commit


def test_ship_all_dependency_order_and_skip(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    add(repo, "c", depends=["b"])
    add(repo, "e")
    add(repo, "d", depends=["e"])
    complete_with_file(repo, "a", "a.txt", "a\n")
    complete_with_file(repo, "b", "b.txt", "b\n")
    complete_with_file(repo, "c", "c.txt", "c\n")
    # e is done but records a dead result ref: it can never ship
    node = c.claim_node(repo, "e", "h1")
    token = node["claim"]["claim_token"]
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    c.complete_node(repo, "e", "h1", token, handoff_note="ok",
                    result={"base_commit": base, "commit": "f" * 40,
                            "changed_files": [], "diff_stats": {}})
    # d is done, but its dependency e can never land
    force_done(repo, "d", "d.txt", "d\n")
    results = sh.ship_all(repo)
    by_id = {r["node_id"]: r for r in results}
    assert by_id["a"]["status"] == "shipped"
    assert by_id["b"]["status"] == "shipped"
    assert by_id["c"]["status"] == "shipped"
    assert by_id["e"]["status"] == "skipped"
    assert by_id["d"]["status"] == "skipped"
    assert by_id["d"]["reason"] == "dependency e not shipped"
    # dependency order: a before b before c
    order = [r["node_id"] for r in results]
    assert order.index("a") < order.index("b") < order.index("c")


def test_ship_all_integration_nodes_last(repo):
    add(repo, "a")
    add(repo, "b")
    add(repo, "i", depends=["a", "b"])
    complete_with_file(repo, "a", "a.txt", "a\n")
    complete_with_file(repo, "b", "b.txt", "b\n")
    integ = wt.ensure_integration_node(repo, "i")
    assert integ == "i__integrate"
    assert g.load_graph(repo)[integ]["status"] == "done"
    results = sh.ship_all(repo)
    order = [r["node_id"] for r in results]
    assert order[-1] == "i__integrate"
    assert all(r["status"] == "shipped" for r in results)


def test_integration_conflict_parks_needs_human(repo):
    add(repo, "a")
    add(repo, "b")
    add(repo, "c", depends=["a", "b"])
    complete_with_file(repo, "a", "app.txt", "from-a\n")
    complete_with_file(repo, "b", "app.txt", "from-b\n")
    integ = wt.ensure_integration_node(repo, "c")
    assert integ == "c__integrate"
    node = g.load_graph(repo)[integ]
    assert node["status"] == "needs_human"
    assert node["claim"]["holder"] is None  # claim cleared atomically
    assert "app.txt" in (node["handoff_note"] or "")
    evs = [e for e in g.load_events(repo)
           if e.get("type") == "human_interrupt" and e.get("node_id") == integ]
    assert len(evs) == 1
    assert evs[0]["payload"]["conflicts"] == ["app.txt"]
    # the child stays blocked with a reason that names the parked node
    ok, reason = c.eligibility(repo, "c")
    assert not ok and "needs_human" in reason


def test_release_tags_head_and_records_shipped_nodes(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    sh.ship_node(repo, "a")
    r = sh.release_tag(repo, "v0.1")
    assert r["tag"] == "v0.1"
    assert r["target"] == "main"
    assert r["shipped_nodes"] == ["a"]
    assert git(repo, "cat-file", "-t", "v0.1").stdout.strip() == "tag"
    # the tag points at the released HEAD; the release event's own
    # .skein commit legitimately advances main past it
    assert git(repo, "rev-parse", "v0.1^{commit}").stdout.strip() == r["head"]
    releases = sh.list_releases(repo)
    assert len(releases) == 1
    assert releases[0]["payload"]["tag"] == "v0.1"
    assert releases[0]["payload"]["shipped_nodes"] == ["a"]


def test_shipped_nodes_since_last_release(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    complete_with_file(repo, "a", "a.txt", "a\n")
    complete_with_file(repo, "b", "b.txt", "b\n")
    sh.ship_node(repo, "a")
    # b is done but not yet shipped: v1 is cut with an explicit override
    sh.release_tag(repo, "v1", allow_unshipped=True)
    sh.ship_node(repo, "b")
    assert sh.shipped_nodes_since_last_release(repo) == ["b"]
    sh.release_tag(repo, "v2")
    assert sh.shipped_nodes_since_last_release(repo) == []
    assert sh.list_releases(repo)[1]["payload"]["shipped_nodes"] == ["b"]


def test_release_refuses_dirty_tree(repo):
    add(repo, "a")
    complete_with_file(repo, "a", "feat.txt", "hello\n")
    sh.ship_node(repo, "a")
    (repo / "app.txt").write_text("dirty\n")
    with pytest.raises(sh.ReleaseError, match="dirty"):
        sh.release_tag(repo, "v0.1")
    (repo / "scratch.txt").write_text("untracked\n")
    with pytest.raises(sh.ReleaseError, match="dirty"):
        sh.release_tag(repo, "v0.1")


def test_release_allows_manually_landed_result(repo):
    # a done node whose result landed on main outside skein (e.g. after
    # a manual conflict resolution) does not block the release
    add(repo, "a")
    result = complete_with_file(repo, "a", "feat.txt", "hello\n")
    git(repo, "merge", "--no-ff", "-m", "manual land", result)
    r = sh.release_tag(repo, "v9")
    assert r["tag"] == "v9"
    assert git(repo, "cat-file", "-t", "v9").stdout.strip() == "tag"


def test_release_refuses_unshipped_nodes(repo):
    add(repo, "a")
    add(repo, "b")
    complete_with_file(repo, "a", "a.txt", "a\n")
    complete_with_file(repo, "b", "b.txt", "b\n")
    sh.ship_node(repo, "a")
    with pytest.raises(sh.ReleaseError, match="unshipped done nodes: b"):
        sh.release_tag(repo, "v0.1")
    r = sh.release_tag(repo, "v0.1", allow_unshipped=True)
    assert r["tag"] == "v0.1"
    ev = sh.list_releases(repo)[0]["payload"]
    assert ev["unshipped_overridden"] == ["b"]


def test_release_rejects_bad_and_duplicate_tags(repo):
    with pytest.raises(sh.ReleaseError, match="invalid tag"):
        sh.release_tag(repo, "bad tag")
    with pytest.raises(sh.ReleaseError, match="invalid tag"):
        sh.release_tag(repo, "v/1")
    sh.release_tag(repo, "v1")
    with pytest.raises(sh.ReleaseError, match="already exists"):
        sh.release_tag(repo, "v1")


def test_release_message_recorded(repo):
    sh.release_tag(repo, "v2", message="second release")
    ev = sh.list_releases(repo)[0]["payload"]
    assert ev["message"] == "second release"
    assert "second release" in git(repo, "tag", "-l", "--format=%(contents)",
                                    "v2").stdout


# ---------- CLI + web surface ----------

@pytest.fixture
def cli_repo(repo, monkeypatch):
    monkeypatch.chdir(repo)
    return repo


def test_cli_ship_and_release(cli_repo, capsys):
    from skein.cli import main as skein_main
    add(cli_repo, "a")
    complete_with_file(cli_repo, "a", "feat.txt", "hello\n")
    assert skein_main(["ship", "a"]) == 0
    out = capsys.readouterr().out
    assert "shipped a -> main" in out
    assert skein_main(["ship", "a"]) == 0
    assert "already shipped" in capsys.readouterr().out
    assert skein_main(["status"]) == 0
    out = capsys.readouterr().out
    assert "main" in out  # shipped column shows the target branch
    assert skein_main(["release", "v0.1-test"]) == 0
    assert "released v0.1-test" in capsys.readouterr().out
    r = subprocess.run(["git", "rev-parse", "--verify", "v0.1-test"],
                       cwd=str(cli_repo), capture_output=True, text=True)
    assert r.returncode == 0


def test_cli_ship_all_table(cli_repo, capsys):
    from skein.cli import main as skein_main
    add(cli_repo, "a")
    add(cli_repo, "b", depends=["a"])
    complete_with_file(cli_repo, "a", "a.txt", "a\n")
    complete_with_file(cli_repo, "b", "b.txt", "b\n")
    assert skein_main(["ship", "--all"]) == 0
    out = capsys.readouterr().out
    assert "shipped" in out and "2 shipped, 0 already shipped, 0 skipped" in out


def test_cli_ship_errors(cli_repo, capsys):
    from skein.cli import main as skein_main
    add(cli_repo, "a")
    assert skein_main(["ship", "a"]) == 1  # not done
    assert "not done" in capsys.readouterr().err
    assert skein_main(["release", "bad tag"]) == 1
    assert "invalid tag" in capsys.readouterr().err


def _call(method, url, body=None):
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def server(cli_repo):
    srv = make_server(str(cli_repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_serve_ship_and_release(server, cli_repo):
    add(cli_repo, "w1")
    complete_with_file(cli_repo, "w1", "w.txt", "w\n")
    code, data = _call("POST", server + "/api/nodes/w1/ship", {"actor": "web"})
    assert code == 200
    assert data["result"]["status"] == "shipped"
    # read-only shipped state rides on /api/graph
    code, data = _call("GET", server + "/api/graph")
    assert code == 200
    shipped = data["nodes"]["w1"]["shipped"]
    assert "main" in shipped and shipped["main"]["merge_commit"]
    code, data = _call("POST", server + "/api/release",
                       {"tag": "v-web", "actor": "web"})
    assert code == 200
    assert data["result"]["tag"] == "v-web"
    code, data = _call("POST", server + "/api/nodes/nope/ship", {})
    assert code == 409
    code, data = _call("POST", server + "/api/release", {"tag": "bad tag"})
    assert code == 409
