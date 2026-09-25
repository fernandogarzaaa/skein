"""Stage 4: verification gate tests (real command execution)."""

import sys

from skein import verify as v

PY = sys.executable


def test_passing_completion(tmp_path):
    ok, evidence = v.run_completion(tmp_path, f"{PY} -c \"print('hello')\"", tmp_path / "ev", "n1")
    assert ok is True
    assert evidence[0]["exit_code"] == 0
    assert evidence[0]["output_ref"] is not None
    import pathlib
    assert pathlib.Path(evidence[0]["output_ref"]).exists()


def test_failing_completion_marks_failed(tmp_path):
    ok, evidence = v.run_completion(tmp_path, f"{PY} -c \"raise SystemExit(3)\"", tmp_path / "ev", "n1")
    assert ok is False
    assert evidence[0]["exit_code"] == 3


def test_gate_drives_done_failed_transitions(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=str(repo), capture_output=True)
    (repo / ".skein").mkdir(exist_ok=True)

    from skein import graph as g
    g.append_event(repo, "t", "node_added", "bad", {
        "title": "bad", "intent": {"goal": "g", "context": "", "constraints": "",
                                   "completion": f"{PY} -c \"raise SystemExit(1)\""},
        "depends_on": [], "blast_radius": []})
    g.append_event(repo, "t", "claimed", "bad", {"holder": "h", "ttl_seconds": 60})
    node = g.load_graph(repo)["bad"]
    ok, evidence = v.run_completion(repo, node["intent"]["completion"],
                                    v.evidence_subdir(repo), "bad")
    assert ok is False
    g.append_event(repo, "sup", "failed", "bad",
                   {"evidence": evidence, "error": "boom"})
    assert g.load_graph(repo)["bad"]["status"] == "failed"
    assert g.load_graph(repo)["bad"]["evidence"][0]["exit_code"] != 0
