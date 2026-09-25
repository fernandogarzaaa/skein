"""Stage 5/6: CLI + end-to-end run + human-interrupt tests."""

import json
import sys
import os
import subprocess
import threading
import time

import pytest

from skein import graph as g
from skein import claim as c
from skein.cli import main as skein_main

PY = sys.executable


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
    assert skein_main(["init"]) == 0
    return tmp_path


class FakeAdapter:
    name = "fake"

    def __init__(self, script_args):
        # script_args: python -c snippet run with the current interpreter
        self.script_args = script_args

    def build_prompt(self, node):
        return "fake"

    def build_command(self, node):
        import sys
        return [sys.executable, "-c", self.script_args]

    def run(self, node, path, timeout=600):
        import sys
        r = subprocess.run([sys.executable, "-c", self.script_args], cwd=str(path),
                           capture_output=True, text=True)
        return r.returncode, r.stdout


def test_cli_add_claim_status_log(repo, capsys):
    assert skein_main(["node", "add", "n1", "--title", "T", "--goal", "gg",
                       "--completion", f"{PY} -c \"print('ok')\""]) == 0
    assert skein_main(["claim", "n1", "--agent-id", "a1"]) == 0
    assert skein_main(["status"]) == 0
    out = capsys.readouterr().out
    assert "n1" in out and "a1" in out
    assert skein_main(["graph"]) == 0
    assert skein_main(["log"]) == 0


def test_end_to_end_run_real_worktree_branch_evidence(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--title", "T", "--goal", "write file",
                       "--completion", f"{PY} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('hello.txt').exists() else 1)\""]) == 0
    adapter = FakeAdapter("open('hello.txt','w').write('hi')")
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    node = g.load_graph(repo)["n1"]
    assert node["status"] == "done"
    assert node["worktree"]["branch"] == "skein/n1"
    assert (repo / ".skein" / "worktrees" / "n1" / "hello.txt").exists()
    assert len(node["evidence"]) >= 2  # completion evidence + adapter evidence
    assert node["handoff_note"]


def test_run_failing_completion_ends_failed(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n2", "--title", "T", "--goal", "g",
                       "--completion", f"{PY} -c \"raise SystemExit(1)\""]) == 0
    adapter = FakeAdapter("pass")
    result = run_node(repo, "n2", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "failed"
    assert g.load_graph(repo)["n2"]["status"] == "failed"


def test_human_interrupt_aborts_supervisor(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n3", "--title", "T", "--goal", "g",
                       "--completion", f"{PY} -c \"print('ok')\""]) == 0
    adapter = FakeAdapter("import time; time.sleep(10)")
    outcome = {}

    def target():
        outcome.update(run_node(repo, "n3", "agent-9", adapter=adapter,
                                heartbeat_interval=0.2, poll_interval=0.05))

    t = threading.Thread(target=target)
    t.start()
    # wait until claimed, then a human edits the claimed node
    for _ in range(100):
        n = g.load_graph(repo).get("n3")
        if n and n["status"] in ("claimed", "in_progress"):
            break
        time.sleep(0.05)
    g.append_event(repo, "human", "human_interrupt", "n3",
                   {"action": "edit", "fields": {"title": "changed by human"}})
    t.join(timeout=30)
    assert not t.is_alive()
    assert outcome.get("outcome") == "interrupted"
    # supervisor must not complete the node after an interrupt
    assert g.load_graph(repo)["n3"]["status"] != "done"
