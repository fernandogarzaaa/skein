"""Stage 4: verification gate tests (real command execution)."""

import sys

from skein import verify as v

PY = sys.executable


def test_passing_completion(tmp_path):
    ok, evidence, _redacted = v.run_completion(tmp_path, f"{PY} -c \"print('hello')\"", tmp_path / "ev", "n1")
    assert ok is True
    assert evidence[0]["exit_code"] == 0
    assert evidence[0]["output_ref"] is not None
    import pathlib
    assert pathlib.Path(evidence[0]["output_ref"]).exists()


def test_failing_completion_marks_failed(tmp_path):
    ok, evidence, _redacted = v.run_completion(tmp_path, f"{PY} -c \"raise SystemExit(3)\"", tmp_path / "ev", "n1")
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
    ok, evidence, _redacted = v.run_completion(repo, node["intent"]["completion"],
                                    v.evidence_subdir(repo), "bad")
    assert ok is False
    g.append_event(repo, "sup", "failed", "bad",
                   {"evidence": evidence, "error": "boom"})
    assert g.load_graph(repo)["bad"]["status"] == "failed"
    assert g.load_graph(repo)["bad"]["evidence"][0]["exit_code"] != 0


def test_python_completion_leaves_no_bytecode(tmp_path):
    # A completion check that imports project code must not drop
    # __pycache__/ into the worktree (it would land in the result commit
    # and trip the blast-radius change policy).
    (tmp_path / "mod_under_test.py").write_text("X = 1\n")
    ok, _evidence, _redacted = v.run_completion(
        tmp_path, f"{PY} -c \"import mod_under_test; assert mod_under_test.X == 1\"",
        tmp_path / "ev", "n1")
    assert ok is True
    assert not (tmp_path / "__pycache__").exists()


def test_changed_files_lists_files_inside_new_directories(tmp_path):
    # A node that creates a new directory must report the files in it, not
    # the collapsed "dir/" entry, or blast-radius globs never match.
    import subprocess
    from skein import supervisor as s
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "sub" / "b.py").write_text("y = 2\n")
    files = sorted(s._changed_files(tmp_path))
    assert files == ["pkg/a.py", "pkg/sub/b.py"]
    assert s._check_change_policy(files, ["pkg/**"], "strict") == []


def _write_harness(tmp_path, early_exit):
    (tmp_path / "solution.py").write_text(
        "import sys\nsys.exit(0)\n" if early_exit else "def solve(n):\n    return n * n\n")
    (tmp_path / "check.py").write_text(
        "import os\nimport solution\n"
        "assert solution.solve(3) == 9\n"
        "print(os.environ['SKEIN_GATE_NONCE'])\n")


def test_nonce_gate_rejects_early_exit(tmp_path):
    # Plain exit-code gating accepts code that sys.exit(0)s before any
    # assertion; @nonce requires the harness to reach its last line.
    _write_harness(tmp_path, early_exit=True)
    ok_plain, _, _ = v.run_completion(tmp_path, f"{PY} check.py", tmp_path / "ev", "n1")
    assert ok_plain is True  # documents the weakness @nonce closes
    ok, evidence, _ = v.run_completion(tmp_path, f"@nonce {PY} check.py", tmp_path / "ev", "n1")
    assert ok is False
    assert evidence[0]["nonce_ok"] is False


def test_nonce_gate_accepts_complete_run_and_hides_nonce(tmp_path):
    import pathlib
    _write_harness(tmp_path, early_exit=False)
    ok, evidence, _ = v.run_completion(tmp_path, f"@nonce {PY} check.py", tmp_path / "ev", "n1")
    assert ok is True
    assert evidence[0]["nonce_ok"] is True
    assert evidence[0]["command"].startswith("@nonce ")
    text = pathlib.Path(evidence[0]["output_ref"]).read_text()
    assert "<nonce>" in text and "nonce check: ok" in text
