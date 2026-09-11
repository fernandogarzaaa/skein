"""Multi-machine sync tests: two clones sharing one file:// remote."""

import subprocess

import pytest

from skein import graph as g
from skein.sync import sync_repo
from skein.cli import main as skein_main


def _git(where, *args):
    r = subprocess.run(["git"] + list(args), cwd=str(where),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """Two clones (m1, m2) of a shared bare remote, both skein-initialized."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "origin.git")
    m1 = tmp_path / "m1"
    _git(tmp_path, "clone", str(origin), "m1")
    _git(m1, "config", "user.email", "t@t")
    _git(m1, "config", "user.name", "t")
    _git(m1, "checkout", "-b", "main")
    (m1 / "app.txt").write_text("base\n")
    _git(m1, "add", ".")
    _git(m1, "commit", "-m", "init")
    _git(m1, "push", "-u", "origin", "main")
    _git(tmp_path, "--git-dir", "origin.git", "symbolic-ref", "HEAD",
         "refs/heads/main")
    m2 = tmp_path / "m2"
    _git(tmp_path, "clone", str(origin), "m2")
    _git(m2, "config", "user.email", "t@t")
    _git(m2, "config", "user.name", "t")
    machines = [m1, m2]
    monkeypatch.chdir(machines[0])
    return machines[0], machines[1]


def test_sync_shares_nodes_both_ways(pair, monkeypatch):
    from skein.cli import main as cli_main
    m1, m2 = pair
    monkeypatch.chdir(m1)
    assert cli_main(["init"]) == 0
    assert cli_main(["node", "add", "a1", "--title", "A", "--goal", "g",
                     "--completion", "true"]) == 0
    assert cli_main(["sync"]) == 0
    # m2 pulls and sees a1, then adds b1 and pushes
    monkeypatch.chdir(m2)
    assert cli_main(["sync"]) == 0
    assert "a1" in g.load_graph(str(m2))
    assert cli_main(["node", "add", "b1", "--title", "B", "--goal", "g",
                     "--completion", "true"]) == 0
    assert cli_main(["sync"]) == 0
    # m1 converges to both
    monkeypatch.chdir(m1)
    assert cli_main(["sync"]) == 0
    nodes = g.load_graph(str(m1))
    assert {"a1", "b1"} <= set(nodes)


def test_concurrent_appends_auto_merge(pair, monkeypatch):
    from skein.cli import main as cli_main
    m1, m2 = pair
    for m in (m1, m2):
        monkeypatch.chdir(m)
        assert cli_main(["init"]) == 0
    assert cli_main(["sync"]) == 0  # converge inits
    # concurrent appends on both sides without syncing
    monkeypatch.chdir(m1)
    assert cli_main(["node", "add", "m1-node", "--title", "M1", "--goal", "g",
                     "--completion", "true"]) == 0
    monkeypatch.chdir(m2)
    assert cli_main(["node", "add", "m2-node", "--title", "M2", "--goal", "g",
                     "--completion", "true"]) == 0
    assert cli_main(["sync"]) == 0  # m2 pushes
    monkeypatch.chdir(m1)
    result = sync_repo(str(m1), push=True)
    assert result["status"] == "ok", result
    nodes = g.load_graph(str(m1))
    assert {"m1-node", "m2-node"} <= set(nodes)
    # and m2 converges too
    monkeypatch.chdir(m2)
    assert cli_main(["sync"]) == 0
    assert {"m1-node", "m2-node"} <= set(g.load_graph(str(m2)))


def test_non_skein_conflict_aborts(pair, monkeypatch):
    from skein.cli import main as cli_main
    m1, m2 = pair
    for m in (m1, m2):
        monkeypatch.chdir(m)
        assert cli_main(["init"]) == 0
    assert cli_main(["sync"]) == 0
    (m1 / "app.txt").write_text("m1 version\n")
    _git(m1, "add", ".")
    _git(m1, "commit", "-m", "m1 edit")
    (m2 / "app.txt").write_text("m2 version\n")
    _git(m2, "add", ".")
    _git(m2, "commit", "-m", "m2 edit")
    _git(m2, "push", "origin", "main")
    monkeypatch.chdir(m1)
    result = sync_repo(str(m1), push=False)
    assert result["status"] == "conflict"
    assert "outside .skein" in result["detail"]
