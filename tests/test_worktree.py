"""Stage 3: worktree + branching against a real temp git repo."""

import subprocess

import pytest
from pathlib import Path

from skein import graph as g
from skein import claim as c
from skein import worktree as wt


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


def add(repo, nid, depends=(), **kw):
    g.append_event(repo, "test", "node_added", nid, {
        "title": nid, "intent": {"goal": "g", "context": "c", "constraints": "",
                                 "completion": "true"},
        "depends_on": list(depends), "blast_radius": []})


def complete_with_branch(repo, nid, holder="a1", content=None, newfile=None):
    node = c.claim_node(repo, nid, holder)
    token = (node.get("claim") or {})["claim_token"]
    path, branch, base = wt.ensure_worktree(repo, nid)
    if content:
        (path / "app.txt").write_text(content)
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
        subprocess.run(["git", "commit", "-m", f"{nid} work"], cwd=str(path),
                       capture_output=True)
    if newfile:
        (path / newfile[0]).write_text(newfile[1])
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
        subprocess.run(["git", "commit", "-m", f"{nid} work"], cwd=str(path),
                       capture_output=True)
    # Fenced completion: only the owning attempt's token can mark done,
    # and the result records the durable commit for downstream bases.
    # The result mirrors the supervisor: base_commit is the pinned SHA the
    # work was done on, diff_stats the real numstat shape.
    result_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path),
        capture_output=True, text=True).stdout.strip()
    base_sha = g.load_graph(repo)[nid]["worktree"]["base_commit"]
    c.complete_node(repo, nid, holder, token,
                    handoff_note="ok", evidence=[],
                    worktree={"branch": branch, "base_branch": base,
                              "path": str(path)},
                    result={"base_commit": base_sha, "commit": result_commit,
                            "changed_files": [],
                            "diff_stats": _numstat_dict(repo, base_sha,
                                                        result_commit)})
    return path, branch


def _numstat_dict(repo, base, commit):
    # independent reimplementation of the recorded diff_stats shape
    r = subprocess.run(["git", "diff", "--numstat", base, commit],
                       cwd=str(repo), capture_output=True, text=True)
    stats = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                stats[parts[2]] = {"added": int(parts[0]),
                                   "deleted": int(parts[1])}
            except ValueError:
                pass
    return stats


def test_base_branch_from_dependency(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    _, branch_a = complete_with_branch(repo, "a", content="from-a\n")
    dep_commit = g.load_graph(repo)["a"]["result"]["commit"]
    c.claim_node(repo, "b", "a2")
    path_b, branch_b, base_b = wt.ensure_worktree(repo, "b")
    # downstream branches from the dep's durable result commit, not the branch name
    assert base_b == dep_commit
    assert path_b.exists()
    # worktree really branched from dep result: contains dep's commit
    r = subprocess.run(["git", "merge-base", "--is-ancestor", dep_commit, branch_b],
                       cwd=str(repo), capture_output=True)
    assert r.returncode == 0


def test_multi_dependency_integration_node(repo):
    add(repo, "a")
    add(repo, "b")
    add(repo, "c", depends=["a", "b"])
    complete_with_branch(repo, "a", newfile=("file_a.txt", "line-a\n"))
    complete_with_branch(repo, "b", newfile=("file_b.txt", "line-b\n"))
    integ = wt.ensure_integration_node(repo, "c")
    assert integ == "c__integrate"
    nodes = g.load_graph(repo)
    assert integ in nodes
    # non-conflicting branches auto-merge deterministically
    assert nodes[integ]["status"] == "done"
    assert nodes["c"]["worktree"]["base_branch"] == f"skein/{integ}-base"
    # integration branch exists and contains both parents
    r = subprocess.run(["git", "rev-parse", "--verify", f"skein/{integ}-base"],
                       cwd=str(repo), capture_output=True)
    assert r.returncode == 0


# ---------- Phase 3: worktree lifecycle, base pinning, result introspection ----------

from skein import edits as e
from skein.cli import main as skein_main


def _branch_exists(repo, branch):
    r = subprocess.run(["git", "rev-parse", "--verify", branch],
                       cwd=str(repo), capture_output=True, text=True)
    return r.returncode == 0


def test_node_delete_removes_worktree_keeps_branch(repo):
    add(repo, "a")
    path, branch = complete_with_branch(repo, "a", content="work\n")
    assert path.exists()
    # delete through the shared edits path (CLI and web canvas use it)
    assert e.edit_node(repo, "test", "a", {}, delete=True) == "removed"
    assert not path.exists()  # worktree gone
    assert _branch_exists(repo, branch)  # branch kept by default
    assert "a" not in g.load_graph(repo)  # removed nodes are hidden
    reduced = g.reduce_events(g.load_events(repo))
    assert reduced["a"]["removed"] is True


def test_node_delete_branch_flag_removes_branch(repo):
    add(repo, "a")
    path, branch = complete_with_branch(repo, "a", content="work\n")
    assert e.edit_node(repo, "test", "a", {}, delete=True,
                       delete_branch=True) == "removed"
    assert not path.exists()
    assert not _branch_exists(repo, branch)


def test_worktree_gc_removes_orphans_and_stale_dirs(repo, monkeypatch):
    monkeypatch.chdir(repo)
    add(repo, "a")
    path_a, _ = complete_with_branch(repo, "a", content="work\n")
    # orphan: registered worktree whose node is gone (removed without the
    # shared delete path, e.g. legacy data)
    add(repo, "orph")
    path_o, _ = complete_with_branch(repo, "orph", content="x\n")
    g.append_event(repo, "test", "node_removed", "orph", {})
    # stale directory: under .skein/worktrees but not a git worktree
    stale = repo / ".skein" / "worktrees" / "stale-dir"
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("junk")
    removed = wt.gc_worktrees(repo)
    assert not path_o.exists()
    assert not stale.exists()
    assert any("orph" in line or str(path_o) in line for line in removed)
    assert any("stale-dir" in line for line in removed)
    # the live node's worktree and the main working tree are untouched
    assert path_a.exists()
    assert (repo / "app.txt").exists()
    assert _branch_exists(repo, "main")


def test_worktree_gc_removes_branchless_worktree(repo):
    add(repo, "a")
    path_a, branch_a = complete_with_branch(repo, "a", content="work\n")
    # branch ref deleted out from under the worktree (plumbing, the way
    # an external `git branch -D` after worktree removal would leave it)
    subprocess.run(["git", "update-ref", "-d", f"refs/heads/{branch_a}"],
                   cwd=str(repo), capture_output=True)
    removed = wt.gc_worktrees(repo)
    assert not path_a.exists()
    assert any(str(path_a) in line for line in removed)


def test_ensure_worktree_reuse_ok_on_base_and_descendant(repo):
    add(repo, "a")
    path, branch, base = wt.ensure_worktree(repo, "a")
    # fresh reuse: HEAD == base
    path2, _, _ = wt.ensure_worktree(repo, "a")
    assert path2 == path
    # reuse after work was committed on top: HEAD is a descendant of base
    (path / "app.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "wip"], cwd=str(path),
                   capture_output=True)
    path3, _, _ = wt.ensure_worktree(repo, "a")
    assert path3 == path


def test_ensure_worktree_refuses_diverged_base(repo):
    add(repo, "a")
    path, branch, base = wt.ensure_worktree(repo, "a")
    # swing the worktree's branch onto an unrelated history: the recorded
    # base is no longer an ancestor of HEAD, so this is genuinely a
    # different base
    r = subprocess.run(["git", "hash-object", "-t", "tree", "/dev/null"],
                       cwd=str(repo), capture_output=True, text=True)
    empty_tree = r.stdout.strip()
    r = subprocess.run(["git", "commit-tree", empty_tree, "-m", "unrelated"],
                       cwd=str(repo), capture_output=True, text=True)
    unrelated = r.stdout.strip()
    subprocess.run(["git", "-C", str(path), "reset", "--hard", unrelated],
                   capture_output=True)
    with pytest.raises(wt.WorktreeError, match="different base"):
        wt.ensure_worktree(repo, "a")


def test_ensure_worktree_refuses_when_dependency_reran(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    path_a, _ = complete_with_branch(repo, "a", content="v1\n")
    wt.ensure_worktree(repo, "b")  # pins a's result commit as its base
    # operator resets the dependency and re-runs it: new result commit
    assert e.edit_node(repo, "test", "a", {"status": "unclaimed"}) == "edited"
    node = c.claim_node(repo, "a", "a1")
    token = node["claim"]["claim_token"]
    wt.ensure_worktree(repo, "a")  # reuses a's own worktree, still on base
    (path_a / "app.txt").write_text("v2\n")
    subprocess.run(["git", "add", "."], cwd=str(path_a), capture_output=True)
    subprocess.run(["git", "commit", "-m", "rerun"], cwd=str(path_a),
                   capture_output=True)
    new_commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                cwd=str(path_a), capture_output=True,
                                text=True).stdout.strip()
    base_sha = g.load_graph(repo)["a"]["worktree"]["base_commit"]
    c.complete_node(repo, "a", "a1", token, handoff_note="rerun", evidence=[],
                    worktree={"branch": "skein/a", "base_branch": "main",
                              "path": str(path_a)},
                    result={"base_commit": base_sha, "commit": new_commit,
                            "changed_files": [],
                            "diff_stats": _numstat_dict(repo, base_sha,
                                                        new_commit)})
    with pytest.raises(wt.WorktreeError, match="base changed"):
        wt.ensure_worktree(repo, "b")


def test_ensure_worktree_records_base_commit(repo):
    add(repo, "a")
    # capture the base SHA before ensure_worktree: its own node_edited event
    # commits .skein, which advances the main branch afterwards
    expected = subprocess.run(["git", "rev-parse", "main"], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
    path, branch, base = wt.ensure_worktree(repo, "a")
    rec = g.load_graph(repo)["a"]["worktree"]
    assert rec["base_commit"] == expected


def test_base_branch_for_raises_without_result_or_branch(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    with pytest.raises(wt.BaseCommitUnavailable):
        wt.base_branch_for(repo, "b")


def test_base_branch_for_raises_unknown_node(repo):
    with pytest.raises(wt.BaseCommitUnavailable):
        wt.base_branch_for(repo, "nope")


def test_base_branch_for_raises_when_result_commit_pruned(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    path_a, _ = complete_with_branch(repo, "a", content="work\n")
    dep_commit = g.load_graph(repo)["a"]["result"]["commit"]
    # simulate object-store pruning: drop every ref to the result commit
    subprocess.run(["git", "worktree", "remove", "--force", str(path_a)],
                   cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-D", "skein/a"], cwd=str(repo),
                   capture_output=True)
    subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"],
                   cwd=str(repo), capture_output=True)
    subprocess.run(["git", "gc", "--prune=now"], cwd=str(repo),
                   capture_output=True)
    r = subprocess.run(["git", "cat-file", "-e", dep_commit], cwd=str(repo),
                       capture_output=True)
    assert r.returncode != 0  # precondition: the object is really gone
    with pytest.raises(wt.BaseCommitUnavailable, match="object is gone"):
        wt.base_branch_for(repo, "b")


def test_result_verify_ok_and_tampered(repo):
    add(repo, "a")
    complete_with_branch(repo, "a", newfile=("hello.txt", "hi\n"))
    assert wt.verify_result_record(repo, "a") == []
    # complete with tampered diff stats: verify must catch the mismatch
    # against the recomputed numstat
    assert e.edit_node(repo, "test", "a", {"status": "unclaimed"}) == "edited"
    node = c.claim_node(repo, "a", "a1")
    token = node["claim"]["claim_token"]
    path = Path(g.load_graph(repo)["a"]["worktree"]["path"])
    base_sha = g.load_graph(repo)["a"]["worktree"]["base_commit"]
    rc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                        capture_output=True, text=True).stdout.strip()
    c.complete_node(repo, "a", "a1", token, handoff_note="ok", evidence=[],
                    worktree={"branch": "skein/a", "base_branch": "main",
                              "path": str(path)},
                    result={"base_commit": base_sha, "commit": rc,
                            "changed_files": [],
                            "diff_stats": {"hello.txt": {"added": 999,
                                                        "deleted": 0}}})
    problems = wt.verify_result_record(repo, "a")
    assert any("diff stats" in p for p in problems)


def test_result_verify_problems(repo):
    add(repo, "a")
    # no result record at all
    assert wt.verify_result_record(repo, "a") == [
        "node 'a' has no result record"]
    # unknown node raises
    with pytest.raises(wt.WorktreeError):
        wt.verify_result_record(repo, "nope")
    # result pointing at objects missing from the store
    node = c.claim_node(repo, "a", "a1")
    token = node["claim"]["claim_token"]
    c.complete_node(repo, "a", "a1", token, handoff_note="ok", evidence=[],
                    result={"base_commit": "0" * 40, "commit": "1" * 40,
                            "changed_files": [], "diff_stats": {},
                            "attempt_id": "x"})
    problems = wt.verify_result_record(repo, "a")
    assert any("missing from the object store" in p for p in problems)
    assert any("result commit" in p for p in problems)
    assert any("base commit" in p for p in problems)


def test_result_cli_show_and_verify(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    add(repo, "a")
    complete_with_branch(repo, "a", newfile=("hello.txt", "hi\n"))
    assert skein_main(["result", "show", "a"]) == 0
    out = capsys.readouterr().out
    assert "base commit:" in out
    assert "result commit:" in out
    assert "hello.txt" in out
    assert skein_main(["result", "verify", "a"]) == 0
    assert "verified" in capsys.readouterr().out
    # no result record: error, not empty output
    add(repo, "b")
    assert skein_main(["result", "show", "b"]) == 1
    assert "no result record" in capsys.readouterr().err
    assert skein_main(["result", "verify", "b"]) == 1
    # unknown node: error
    assert skein_main(["result", "show", "nope"]) == 1


def test_node_delete_cli(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    add(repo, "a")
    path, branch = complete_with_branch(repo, "a", content="work\n")
    assert skein_main(["node", "delete", "a"]) == 0
    out = capsys.readouterr().out
    assert "removed node a" in out
    assert "removed worktree" in out
    assert not path.exists()
    assert _branch_exists(repo, branch)  # kept by default


def test_worktree_gc_cli(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    stale = repo / ".skein" / "worktrees" / "stale-dir"
    stale.mkdir(parents=True)
    assert skein_main(["worktree", "gc"]) == 0
    out = capsys.readouterr().out
    assert "stale-dir" in out
    assert not stale.exists()
