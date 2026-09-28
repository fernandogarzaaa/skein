"""Runtime hazard regression tests: pipe deadlock, bounded output,
process-tree kill.

The old supervisor attached stdout=PIPE and read it only after the child
exited: any backend writing more than the ~64KB pipe buffer deadlocked.
terminate() also killed only the direct child, orphaning grandchildren.
"""

import os
import subprocess
import sys
import time

import pytest

from skein import graph as g
from skein import runtime as rt
from skein.cli import main as skein_main

PY = sys.executable


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
    assert skein_main(["init"]) == 0
    return tmp_path


class FakeAdapter:
    name = "fake"

    def __init__(self, script):
        self.script = script

    def build_prompt(self, node):
        return "fake"

    def build_command(self, node):
        return [sys.executable, "-c", self.script]


def test_bounded_buffer_keeps_tail():
    b = rt.BoundedBuffer(cap=100)
    b.append(b"a" * 60)
    b.append(b"b" * 60)
    t = b.text()
    assert "output truncated" in t
    assert t.endswith("b" * 60)
    assert "a" * 60 not in t


def test_run_bounded_chatty_no_deadlock(tmp_path):
    # 500KB >> 64KB pipe buffer: must not deadlock
    code, out = rt.run_bounded(
        [PY, "-c", "import sys; sys.stdout.write('y' * 500000)"],
        cwd=str(tmp_path), timeout=30)
    assert code == 0
    assert len(out) == 500000


def test_run_bounded_caps_output(tmp_path):
    code, out = rt.run_bounded(
        [PY, "-c", "import sys; sys.stdout.write('z' * 3_000_000)"],
        cwd=str(tmp_path), timeout=30)
    assert code == 0
    assert "output truncated" in out
    assert len(out) < 3_000_000


def test_run_bounded_timeout_kills_tree(tmp_path):
    pidfile = tmp_path / "gc.pid"
    script = (
        "import subprocess, sys, os, time; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import os, time; open('{pidfile.as_posix()}', 'w').write(str(os.getpid())); "
        f"time.sleep(120)\"]); "
        "time.sleep(120)"
    )
    start = time.monotonic()
    code, out = rt.run_bounded([PY, "-c", script], cwd=str(tmp_path), timeout=2)
    assert code == 124
    assert "timed out" in out
    assert time.monotonic() - start < 30  # kill, not the 120s sleep
    assert pidfile.exists()
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            break  # grandchild is dead
        time.sleep(0.2)
    else:
        pytest.fail("grandchild survived kill_tree")


def test_supervisor_chatty_backend_completes(repo):
    # end to end: a backend louder than the pipe buffer finishes and the
    # node still completes with evidence
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--completion", "true"]) == 0
    adapter = FakeAdapter("import sys; sys.stdout.write('w' * 300000)")
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    assert g.load_graph(repo)["n1"]["status"] == "done"


def test_supervisor_timeout_kills_backend_tree(repo):
    from skein.supervisor import run_node
    assert skein_main(["node", "add", "n1", "--completion", "true"]) == 0
    pidfile = repo / "gc.pid"
    script = (
        "import subprocess, sys, os, time; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import os, time; open('{pidfile.as_posix()}', 'w').write(str(os.getpid())); "
        f"time.sleep(120)\"]); "
        "time.sleep(120)"
    )
    adapter = FakeAdapter(script)
    result = run_node(repo, "n1", "agent-1", adapter=adapter,
                      heartbeat_interval=0.2, poll_interval=0.05,
                      adapter_timeout=2)
    assert result["outcome"] == "failed"
    assert "timed out" in (g.load_graph(repo)["n1"]["handoff_note"] or "")
    assert pidfile.exists()
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            break
        time.sleep(0.2)
    else:
        pytest.fail("backend grandchild survived the supervisor abort")
