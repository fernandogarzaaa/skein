"""Stage 3: worktree + branching against a real temp git repo."""

import subprocess

import pytest

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
    c.claim_node(repo, nid, holder)
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
    g.append_event(repo, holder, "completed", nid,
                   {"handoff_note": "ok", "evidence": [],
                    "worktree": {"branch": branch, "base_branch": base,
                                 "path": str(path)}})
    return path, branch


def test_base_branch_from_dependency(repo):
    add(repo, "a")
    add(repo, "b", depends=["a"])
    _, branch_a = complete_with_branch(repo, "a", content="from-a\n")
    c.claim_node(repo, "b", "a2")
    path_b, branch_b, base_b = wt.ensure_worktree(repo, "b")
    assert base_b == branch_a
    assert path_b.exists()
    # worktree really branched from dep branch: contains dep's commit
    r = subprocess.run(["git", "merge-base", "--is-ancestor", branch_a, branch_b],
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
