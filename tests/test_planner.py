"""Phase 8: planner / automatic DAG generation."""

import json
import os
import subprocess

import pytest

from skein import graph as g
from skein import planner
from skein.planner import PlanError


def _write_plancmd(tmp_path, py_source):
    """Write a portable SKEIN_PLANNER_CMD stub running the given Python source.

    Extensionless shell scripts are not executable on Windows
    (WinError 193), so the stub is a .bat there that re-invokes the
    current interpreter; on POSIX it stays an executable shell script.
    Returns the path to put in SKEIN_PLANNER_CMD.
    """
    import sys
    impl = tmp_path / "plancmd_impl.py"
    impl.write_text(py_source)
    if os.name == "nt":
        stub = tmp_path / "plancmd.bat"
        stub.write_text(f'@echo off\r\n"{sys.executable}" "{impl}"\r\n')
    else:
        stub = tmp_path / "plancmd"
        stub.write_text(f"#!/bin/sh\nexec \"{sys.executable}\" \"{impl}\"\n")
        stub.chmod(0o755)
    return str(stub)


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
    from skein.cli import main as skein_main
    assert skein_main(["init"]) == 0
    return tmp_path


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    taken = set()
    assert planner.slugify("Design Schema!", taken, "task-1") == "design-schema"


def test_slugify_disambiguates():
    taken = set()
    a = planner.slugify("Login", taken, "task-1")
    b = planner.slugify("Login", taken, "task-2")
    assert a == "login"
    assert b == "login-2"


def test_slugify_fallback_for_empty():
    taken = set()
    assert planner.slugify("!!!", taken, "task-9") == "task-9"


# ---------------------------------------------------------------------------
# heuristic: numbered list -> chain
# ---------------------------------------------------------------------------

def test_numbered_list_chains():
    d = planner.parse_heuristic(
        "Build auth: 1. design schema 2. implement login 3. write tests",
        goal="Build auth: 1. design schema 2. implement login 3. write tests")
    ids = [n["id"] for n in d["nodes"]]
    assert ids == ["design-schema", "implement-login", "write-tests"]
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["design-schema"] == []
    assert deps["implement-login"] == ["design-schema"]
    assert deps["write-tests"] == ["implement-login"]
    assert d["goal"] == "Build auth"
    assert d["version"] == 1
    assert d["source"] == "heuristic"


def test_multiline_numbered_list_chains():
    d = planner.parse_heuristic("1. one\n2. two\n3. three", goal="g")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["two"] == ["one"]
    assert deps["three"] == ["two"]


# ---------------------------------------------------------------------------
# heuristic: bullets under headings -> parallel, stages sequential
# ---------------------------------------------------------------------------

def test_bullets_parallel_headings_sequential():
    d = planner.parse_heuristic(
        "# Backend\n- design schema\n- implement login\n"
        "# Frontend\n- login page\n- dashboard\n",
        goal="Build app")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["design-schema"] == []
    assert deps["implement-login"] == []
    assert sorted(deps["login-page"]) == ["design-schema", "implement-login"]
    assert sorted(deps["dashboard"]) == ["design-schema", "implement-login"]


# ---------------------------------------------------------------------------
# heuristic: explicit hints
# ---------------------------------------------------------------------------

def test_after_hint_creates_edge():
    d = planner.parse_heuristic(
        "1. design schema\n2. implement login\n3. write tests after implement login")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert "implement-login" in deps["write-tests"]
    # hint clause stripped from the title
    titles = {n["id"]: n["title"] for n in d["nodes"]}
    assert titles["write-tests"] == "write tests"


def test_depends_on_hint():
    d = planner.parse_heuristic(
        "- migrate db\n- backfill (depends on migrate db)")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["backfill"] == ["migrate-db"]


def test_once_done_hint():
    d = planner.parse_heuristic(
        "1. write code\n2. review once write code is done")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert "write-code" in deps["review"]


def test_unmatched_hint_warns_not_fails():
    d = planner.parse_heuristic("1. do thing after nothing real")
    assert d["warnings"], "expected a warning for the unmatched hint"
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["do-thing"] == []


# ---------------------------------------------------------------------------
# heuristic: cycles refused
# ---------------------------------------------------------------------------

def test_cycle_refused_with_path():
    with pytest.raises(PlanError, match="do-a -> do-b -> do-a"):
        planner.parse_heuristic("1. do A after do B\n2. do B after do A")


def test_self_hint_no_self_edge():
    # bullets (parallel): "login" hints at "login page" without chaining
    d = planner.parse_heuristic("- login after login page\n- login page")
    deps = {n["id"]: n["depends_on"] for n in d["nodes"]}
    assert deps["login"] == ["login-page"]
    assert deps["login-page"] == []


# ---------------------------------------------------------------------------
# heuristic: prose-only goal -> single node
# ---------------------------------------------------------------------------

def test_prose_goal_single_node():
    d = planner.parse_heuristic("migrate the billing system to stripe")
    assert len(d["nodes"]) == 1
    assert d["nodes"][0]["depends_on"] == []


def test_deterministic():
    text = "# A\n- x\n- y\n# B\n- z after x"
    a = planner.parse_heuristic(text)
    b = planner.parse_heuristic(text)
    a.pop("created_at")
    b.pop("created_at")
    assert a == b


# ---------------------------------------------------------------------------
# draft persistence: versioning, redaction
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(repo):
    d = planner.parse_heuristic("1. a\n2. b", goal="g")
    path = planner.save_draft(str(repo), d)
    assert path.name.startswith("plan-")
    loaded = planner.load_draft(path)
    assert loaded["nodes"] == d["nodes"]
    assert planner.latest_draft(str(repo)) == path


def test_load_rejects_unknown_version(repo, tmp_path):
    p = tmp_path / "plan-x.json"
    p.write_text(json.dumps({"version": 99, "nodes": []}))
    with pytest.raises(PlanError, match="unsupported version"):
        planner.load_draft(p)


def test_goal_secrets_redacted_in_draft_file(repo):
    d = planner.parse_heuristic(
        "rotate key", goal="deploy with sk-ant-fake000000000000000000")
    path = planner.save_draft(str(repo), d)
    raw = path.read_text()
    assert "sk-ant-fake000000000000000000" not in raw
    assert "[REDACTED]" in raw


def test_draft_hash_stable():
    d = planner.parse_heuristic("1. a\n2. b", goal="g")
    assert planner.draft_hash(d) == planner.draft_hash(
        planner.parse_heuristic("1. a\n2. b", goal="g"))


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def test_apply_creates_nodes(repo):
    from skein.cli import main as skein_main
    d = planner.parse_heuristic(
        "Build auth: 1. design schema 2. implement login 3. write tests")
    path = planner.save_draft(str(repo), d)
    created, skipped = planner.apply_draft(str(repo), "tester", d, path.name)
    assert created == ["design-schema", "implement-login", "write-tests"]
    assert skipped == []
    nodes = g.load_graph(str(repo))
    assert nodes["implement-login"]["depends_on"] == ["design-schema"]
    assert nodes["write-tests"]["depends_on"] == ["implement-login"]
    assert nodes["design-schema"]["status"] == "unclaimed"


def test_apply_twice_refused_without_force(repo):
    d = planner.parse_heuristic("1. a\n2. b", goal="g")
    path = planner.save_draft(str(repo), d)
    planner.apply_draft(str(repo), "tester", d, path.name)
    with pytest.raises(PlanError, match="already applied"):
        planner.apply_draft(str(repo), "tester", d, path.name)


def test_apply_force_reapplies_after_delete(repo):
    from skein import edits
    d = planner.parse_heuristic("1. a\n2. b", goal="g")
    path = planner.save_draft(str(repo), d)
    planner.apply_draft(str(repo), "tester", d, path.name)
    # delete leaf-first: dependents block deletion of their deps
    edits.edit_node(str(repo), "tester", "b", {}, delete=True)
    edits.edit_node(str(repo), "tester", "a", {}, delete=True)
    created, skipped = planner.apply_draft(str(repo), "tester", d, path.name,
                                           force=True)
    assert created == ["a", "b"]
    assert skipped == []
    nodes = g.load_graph(str(repo))
    assert nodes["b"]["depends_on"] == ["a"]


def test_apply_force_skips_existing(repo):
    d = planner.parse_heuristic("1. a\n2. b", goal="g")
    path = planner.save_draft(str(repo), d)
    planner.apply_draft(str(repo), "tester", d, path.name)
    created, skipped = planner.apply_draft(str(repo), "tester", d, path.name,
                                           force=True)
    assert created == []
    assert skipped == ["a", "b"]


def test_apply_goes_through_validation(repo):
    # planner ids that collide with existing nodes are refused by add_node
    from skein import edits
    edits.add_node(str(repo), "tester", "taken", title="taken")
    d = planner.parse_heuristic("1. taken\n2. other", goal="g")
    path = planner.save_draft(str(repo), d)
    with pytest.raises(ValueError, match="already exists"):
        planner.apply_draft(str(repo), "tester", d, path.name)


def test_plan_applied_event_in_log(repo):
    d = planner.parse_heuristic("1. a", goal="g")
    path = planner.save_draft(str(repo), d)
    planner.apply_draft(str(repo), "tester", d, path.name)
    kinds = [e["type"] for e in g.load_events(str(repo))]
    assert "plan_applied" in kinds
    ev = next(e for e in g.load_events(str(repo))
              if e["type"] == "plan_applied")
    assert ev["payload"]["draft_hash"] == planner.draft_hash(d)
    assert ev["payload"]["node_ids"] == ["a"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_plan_dry_run(repo, capsys):
    from skein.cli import main as skein_main
    rc = skein_main(["plan", "Build auth: 1. design schema 2. implement login",
                     "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[design-schema]" in out
    assert "depends_on: design-schema" in out
    assert planner.list_drafts(str(repo)) == []


def test_cli_plan_saves_and_applies(repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["plan", "1. alpha\n2. beta"]) == 0
    drafts = planner.list_drafts(str(repo))
    assert len(drafts) == 1
    capsys.readouterr()
    assert skein_main(["plan", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "created 2 node(s)" in out
    nodes = g.load_graph(str(repo))
    assert nodes["beta"]["depends_on"] == ["alpha"]
    # second apply refuses
    assert skein_main(["plan", "--apply"]) == 1
    err = capsys.readouterr().err
    assert "already applied" in err


def test_cli_plan_apply_named_draft(repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["plan", "1. one"]) == 0
    name = planner.list_drafts(str(repo))[0].name
    assert skein_main(["plan", "--apply", name]) == 0
    assert "one" in g.load_graph(str(repo))


def test_cli_plan_from_git_log(repo, capsys):
    from skein.cli import main as skein_main
    subprocess.run(["git", "commit", "--allow-empty", "-m", "second change"],
                   cwd=str(repo), capture_output=True)
    assert skein_main(["plan", "--from-git-log", "--commits", "2",
                       "--dry-run"]) == 0
    out = capsys.readouterr().out
    # newest two commits are "second change" and the "skein: init"
    # housekeeping commit; oldest-first chain links them sequentially
    assert "second change" in out and "skein: init" in out
    assert "depends_on: skein-init" in out


def test_cli_plan_list(repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["plan", "--list"]) == 0
    assert "no saved drafts" in capsys.readouterr().out
    assert skein_main(["plan", "1. x", "--dry-run"]) == 0
    assert skein_main(["plan", "1. x"]) == 0
    out = capsys.readouterr().out
    assert "plan-" in out and out.strip().endswith(".json")


def test_cli_plan_needs_input(repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["plan"]) == 1
    assert "need a goal" in capsys.readouterr().err


def test_cli_plan_cycle_refused(repo, capsys):
    from skein.cli import main as skein_main
    assert skein_main(["plan", "1. do A after do B\n2. do B after do A"]) == 1
    assert "cycle" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# LLM brain
# ---------------------------------------------------------------------------

def test_llm_missing_env_errors(repo, monkeypatch):
    monkeypatch.delenv("SKEIN_PLANNER_CMD", raising=False)
    with pytest.raises(PlanError, match="SKEIN_PLANNER_CMD is not set"):
        planner.parse_llm("goal", "text", str(repo))


def test_llm_happy_path(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(tmp_path,
        "import json, sys\n"
        "spec = json.load(sys.stdin)\n"
        "assert spec['goal'] == 'ship it'\n"
        "json.dump({'version': 1, 'goal': 'ship it', 'nodes': [\n"
        "  {'id': 'llm-a', 'title': 'A', 'depends_on': []},\n"
        "  {'id': 'llm-b', 'title': 'B', 'depends_on': ['llm-a']}]}, sys.stdout)\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    d = planner.parse_llm("ship it", "text", str(repo))
    assert d["brain"] == cmd
    assert [n["id"] for n in d["nodes"]] == ["llm-a", "llm-b"]


def test_llm_bad_json_errors(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(tmp_path, "print('not json')\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    with pytest.raises(PlanError, match="invalid JSON"):
        planner.parse_llm("g", "t", str(repo))


def test_llm_nonzero_exit_errors(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(
        tmp_path, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    with pytest.raises(PlanError, match="exited 3"):
        planner.parse_llm("g", "t", str(repo))


def test_llm_wrong_version_refused(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(tmp_path, "print('{\"version\": 2, \"nodes\": []}')\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    with pytest.raises(PlanError, match="version"):
        planner.parse_llm("g", "t", str(repo))


def test_llm_cycle_refused(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(
        tmp_path,
        "print('{\"version\": 1, \"nodes\": ['\n"
        "'{\"id\": \"a\", \"title\": \"A\", \"depends_on\": [\"b\"]}, '\n"
        "'{\"id\": \"b\", \"title\": \"B\", \"depends_on\": [\"a\"]}]}')\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    with pytest.raises(PlanError, match="cycle"):
        planner.parse_llm("g", "t", str(repo))


def test_llm_unknown_dep_refused(repo, tmp_path, monkeypatch):
    cmd = _write_plancmd(
        tmp_path,
        "print('{\"version\": 1, \"nodes\": ['\n"
        "'{\"id\": \"a\", \"title\": \"A\", \"depends_on\": [\"ghost\"]}]}')\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    with pytest.raises(PlanError, match="unknown"):
        planner.parse_llm("g", "t", str(repo))


def test_llm_never_falls_back_to_heuristic(repo, tmp_path, monkeypatch,
                                           capsys):
    # a failing command must error, not silently produce a heuristic plan
    cmd = _write_plancmd(tmp_path, "import sys\nsys.exit(1)\n")
    monkeypatch.setenv("SKEIN_PLANNER_CMD", cmd)
    from skein.cli import main as skein_main
    assert skein_main(["plan", "1. a", "--llm"]) == 1
    assert "exited 1" in capsys.readouterr().err
    assert planner.list_drafts(str(repo)) == []


def _llm_script(tmp_path, payload):
    import json
    return _write_plancmd(
        tmp_path,
        "import json, sys\n"
        f"json.dump({payload!r}, sys.stdout)\n")


def test_llm_string_warnings_refused(repo, tmp_path, monkeypatch):
    # a string 'warnings' must not be rendered per-character
    script = _llm_script(
        tmp_path,
        {"version": 1, "goal": "g", "warnings": "oops",
         "nodes": [{"id": "a", "title": "A", "depends_on": []}]})
    monkeypatch.setenv("SKEIN_PLANNER_CMD", str(script))
    with pytest.raises(PlanError, match="warnings.*list of strings"):
        planner.parse_llm("g", "text", str(repo))


def test_llm_nonstring_dep_refused(repo, tmp_path, monkeypatch):
    # an int in depends_on must be a clean PlanError, not TypeError
    script = _llm_script(
        tmp_path,
        {"version": 1, "goal": "g",
         "nodes": [{"id": "a", "title": "A", "depends_on": [5]}]})
    monkeypatch.setenv("SKEIN_PLANNER_CMD", str(script))
    with pytest.raises(PlanError, match="depends_on.*list of strings"):
        planner.parse_llm("g", "text", str(repo))


def test_llm_nonstring_node_field_refused(repo, tmp_path, monkeypatch):
    script = _llm_script(
        tmp_path,
        {"version": 1, "goal": "g",
         "nodes": [{"id": "a", "title": ["not", "a", "string"],
                    "depends_on": []}]})
    monkeypatch.setenv("SKEIN_PLANNER_CMD", str(script))
    with pytest.raises(PlanError, match="field 'title' must be a string"):
        planner.parse_llm("g", "text", str(repo))
