"""Skills: agent-authored scripts with an evidence-gated trust lifecycle.

What is pinned here:
- the lifecycle proposed -> tested -> promoted, each step an event in the
  log, trust bound to the script's content hash (editing demotes);
- promotion needs test evidence and a pass rate at the threshold;
- the index is one line per skill and never contains script bodies, and
  the supervisor puts it in the backend prompt;
- skills run through the bounded runtime with the caller's cwd;
- skills commands from inside an agent worktree write to the main repo's
  control plane, never into the worktree's checked-out .skein/.
"""

import json
import os
import subprocess
import sys

import pytest

from skein import graph as g
from skein import skills as sk
from skein.cli import main as skein_main

PY = sys.executable


def git(repo, *args):
    return subprocess.run(["git"] + list(args), cwd=str(repo),
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    monkeypatch.chdir(tmp_path)
    assert skein_main(["init"]) == 0
    return tmp_path


SKILL_OK = '''# skein-skill
# name: {name}
# description: {desc}
# usage: skein skills run {name} -- <word>
# triggers: {triggers}
# self-test: --self-test
import sys
args = sys.argv[1:]
if args == ["--self-test"]:
    print("self-test ok"); sys.exit(0)
print("hello " + " ".join(args))
sys.exit(0)
'''

SKILL_FAILING_TEST = '''# description: always fails its self-test
import sys
sys.exit(3)
'''


def _write_skill(repo, name, text):
    d = repo / ".skein" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.py"
    p.write_text(text)
    return p


def _skill(repo, name="greet", desc="Print a greeting", triggers="greet, hello"):
    return _write_skill(repo, name, SKILL_OK.format(name=name, desc=desc,
                                                    triggers=triggers))


# ---------------------------------------------------------------------------
# header + files
# ---------------------------------------------------------------------------

def test_parse_header_reads_comment_forms():
    text = ("#!/usr/bin/env python3\n# skein-skill\n# name: x\n"
            "// description: Does X\n-- usage: skein skills run x\n"
            "# triggers: A, b ,c\n# self-test: none\n")
    h = sk.parse_header(text)
    assert h["name"] == "x" and h["description"] == "Does X"
    assert h["usage"] == "skein skills run x"
    assert h["triggers"] == ["a", "b", "c"]
    assert sk.self_test_argv(h) is None
    assert sk.self_test_argv({}) == ["--self-test"]
    assert sk.self_test_argv({"self_test": "--check --fast"}) == ["--check", "--fast"]


def test_scaffold_then_add_registers_proposed(repo):
    assert skein_main(["skills", "new", "list-todos", "--description",
                       "List TODOs", "--triggers", "todo,cleanup"]) == 0
    path = repo / ".skein" / "skills" / "list-todos.py"
    assert path.exists()
    assert sk.registry(repo)["list-todos"]["stage"] is None  # on disk, unregistered
    assert skein_main(["skills", "add", "list-todos"]) == 0
    rec = sk.registry(repo)["list-todos"]
    assert rec["stage"] == "proposed"
    assert rec["hash"] == sk.file_hash(path)
    assert rec["triggers"] == ["todo", "cleanup"]
    # the script is control-plane state: committed with the proposal
    assert ".skein/skills/list-todos.py" in git(repo, "ls-files").stdout
    # adding the exact same content again is a no-op error, not a reset
    with pytest.raises(sk.SkillError, match="already registered"):
        sk.add(repo, "t", "list-todos")


def test_add_requires_description_header(repo):
    _write_skill(repo, "bare", "import sys\nsys.exit(0)\n")
    with pytest.raises(sk.SkillError, match="description"):
        sk.add(repo, "t", "bare")


def test_add_from_file_copies_into_skills_dir(repo, tmp_path):
    src = tmp_path / "elsewhere.py"
    src.write_text(SKILL_OK.format(name="copy", desc="Copied in", triggers="x"))
    rec = sk.add(repo, "t", "copy", source=src)
    assert rec["stage"] == "proposed"
    assert (repo / ".skein" / "skills" / "copy.py").exists()


def test_add_rejects_mismatched_header_name(repo):
    _write_skill(repo, "other", SKILL_OK.format(name="greet", desc="d", triggers="x"))
    with pytest.raises(sk.SkillError, match="declares name"):
        sk.add(repo, "t", "other")


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_lifecycle_proposed_tested_promoted(repo):
    _skill(repo)
    sk.add(repo, "agent-1", "greet")
    with pytest.raises(sk.SkillError, match="needs test evidence"):
        sk.promote(repo, "greet")
    rec = sk.test(repo, "greet", actor="agent-1")
    assert rec["stage"] == "tested"
    assert rec["last_test"]["passed"]
    assert rec["fitness"] == 1.0
    assert os.path.exists(rec["last_test"]["output_ref"])
    rec = sk.promote(repo, "greet", actor="human")
    assert rec["stage"] == "promoted"
    types = [(e["type"], e["payload"].get("action")) for e in g.load_events(repo)
             if e["type"] == "skill"]
    assert types == [("skill", "proposed"), ("skill", "tested"), ("skill", "promoted")]
    # promoting again is idempotent
    assert sk.promote(repo, "greet")["stage"] == "promoted"


def test_failing_self_test_cannot_be_promoted(repo):
    _write_skill(repo, "flaky", SKILL_FAILING_TEST)
    sk.add(repo, "t", "flaky")
    rec = sk.test(repo, "flaky")
    assert rec["stage"] == "tested" and rec["fitness"] == 0.0
    with pytest.raises(sk.SkillError, match="below the threshold"):
        sk.promote(repo, "flaky")


def test_threshold_from_config(repo):
    cfg = g.load_config(repo)
    cfg["skill_promote_threshold"] = 0.5
    g.config_path(repo).write_text(json.dumps(cfg))
    path = _skill(repo, "half")
    sk.add(repo, "t", "half")
    sk.test(repo, "half")  # pass
    # record a failing test on the same hash directly (simulates a flaky run)
    g.append_event(repo, "t", "skill", sk.SKILL_ANCHOR,
                   {"action": "tested", "name": "half", "hash": sk.file_hash(path),
                    "passed": False})
    rec = sk.registry(repo)["half"]
    assert rec["fitness"] == 0.5
    assert sk.promote(repo, "half")["stage"] == "promoted"


def test_editing_a_promoted_skill_demotes_until_retested(repo):
    path = _skill(repo)
    sk.add(repo, "t", "greet")
    sk.test(repo, "greet")
    sk.promote(repo, "greet")
    old_hash = sk.registry(repo)["greet"]["hash"]
    path.write_text(path.read_text() + "# edited\n")
    rec = sk.registry(repo)["greet"]
    assert rec["stage"] == "modified"
    assert sk.index_lines(repo) == []  # never pointed at by the index
    with pytest.raises(sk.SkillError, match="changed on disk"):
        sk.test(repo, "greet")
    with pytest.raises(sk.SkillError, match="changed on disk"):
        sk.promote(repo, "greet")
    rec = sk.add(repo, "t", "greet")  # re-register the new content
    assert rec["stage"] == "proposed" and rec["tests"] == []
    assert rec["archived_hashes"] == [old_hash]
    sk.test(repo, "greet")
    assert sk.promote(repo, "greet")["stage"] == "promoted"


def test_failing_test_on_promoted_content_revokes_trust(repo):
    path = _skill(repo)
    sk.add(repo, "t", "greet")
    sk.test(repo, "greet")
    sk.promote(repo, "greet")
    g.append_event(repo, "t", "skill", sk.SKILL_ANCHOR,
                   {"action": "tested", "name": "greet", "hash": sk.file_hash(path),
                    "passed": False})
    assert sk.registry(repo)["greet"]["stage"] == "tested"


def test_evidence_for_other_content_never_counts(repo):
    _skill(repo)
    sk.add(repo, "t", "greet")
    g.append_event(repo, "t", "skill", sk.SKILL_ANCHOR,
                   {"action": "tested", "name": "greet", "hash": "deadbeef",
                    "passed": True})
    rec = sk.registry(repo)["greet"]
    assert rec["stage"] == "proposed" and rec["tests"] == []
    g.append_event(repo, "t", "skill", sk.SKILL_ANCHOR,
                   {"action": "promoted", "name": "greet", "hash": "deadbeef"})
    assert sk.registry(repo)["greet"]["stage"] == "proposed"


def test_self_test_none_cannot_earn_trust(repo):
    _write_skill(repo, "notest", "# description: no self test\n# self-test: none\n"
                                 "print('x')\n")
    sk.add(repo, "t", "notest")
    with pytest.raises(sk.SkillError, match="self-test: none"):
        sk.test(repo, "notest")


def test_retire_and_reject(repo):
    _skill(repo)
    sk.add(repo, "t", "greet")
    rec = sk.retire(repo, "greet", reason="superseded")
    assert rec["stage"] == "retired" and rec["reason"] == "superseded"
    with pytest.raises(sk.SkillError, match="retired"):
        sk.run(repo, "greet", record=False)
    assert sk.index_lines(repo) == []
    _skill(repo, "bad")
    sk.add(repo, "t", "bad")
    assert sk.retire(repo, "bad", reject=True, reason="wrong")["stage"] == "rejected"
    # re-adding revives a retired skill as a fresh proposal (same content
    # is fine: the retire reset its trust, so it must earn it again)
    rec = sk.add(repo, "t", "greet")
    assert rec["stage"] == "proposed" and rec["tests"] == []


def test_missing_file_shows_as_missing(repo):
    path = _skill(repo)
    sk.add(repo, "t", "greet")
    path.unlink()
    rec = sk.registry(repo)["greet"]
    assert rec["stage"] == "missing"
    assert sk.index_lines(repo) == []


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def test_run_passes_args_uses_cwd_and_records(repo, tmp_path):
    _skill(repo)
    sk.add(repo, "t", "greet")
    work = tmp_path / "work"
    work.mkdir()
    r = sk.run(repo, "greet", ["there", "friend"], cwd=work)
    assert r.exit_code == 0 and r.stdout.strip() == "hello there friend"
    rec = sk.registry(repo)["greet"]
    assert rec["runs"] == 1 and rec["run_failures"] == 0
    r = sk.run(repo, "greet", [], record=False)
    assert sk.registry(repo)["greet"]["runs"] == 1  # not recorded


def test_run_promoted_only_refuses_untrusted(repo):
    _skill(repo)
    sk.add(repo, "t", "greet")
    with pytest.raises(sk.SkillError, match="not promoted"):
        sk.run(repo, "greet", allow_untrusted=False, record=False)


def test_run_is_bounded(repo):
    _write_skill(repo, "slow", "# description: sleeps\nimport time\ntime.sleep(30)\n")
    sk.add(repo, "t", "slow")
    r = sk.run(repo, "slow", timeout=1, record=True)
    assert r.timed_out
    assert sk.registry(repo)["slow"]["run_failures"] == 1


def test_cli_run_separates_skein_options_from_skill_args(repo, capsys):
    _skill(repo)
    sk.add(repo, "t", "greet")
    assert skein_main(["skills", "run", "greet", "--no-record", "--", "a", "--b"]) == 0
    assert capsys.readouterr().out.strip() == "hello a --b"
    assert sk.registry(repo)["greet"]["runs"] == 0
    assert skein_main(["skills", "run", "greet", "x"]) == 0
    assert capsys.readouterr().out.strip() == "hello x"
    assert sk.registry(repo)["greet"]["runs"] == 1
    assert skein_main(["skills", "run", "greet", "--promoted-only"]) == 1
    assert "not promoted" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the index: one line per skill, bodies never included
# ---------------------------------------------------------------------------

def test_index_lines_rank_promoted_and_trigger_matches_first(repo):
    _skill(repo, "greet", "Print a greeting", "greet, hello")
    _skill(repo, "lint", "Run the linter", "lint, style")
    _skill(repo, "deploy", "Deploy to staging", "deploy")
    for n in ("greet", "lint", "deploy"):
        sk.add(repo, "t", n)
    sk.test(repo, "lint")
    sk.promote(repo, "lint")
    lines = sk.index_lines(repo, {"title": "Greet users", "intent": {"goal": "say hello"}})
    assert len(lines) == 3
    assert lines[0].startswith("lint  [promoted]")
    assert lines[1].startswith("greet  [proposed (untrusted)]")  # trigger hit beats deploy
    assert lines[2].startswith("deploy  [proposed (untrusted)]")
    # bodies never leak into the index
    assert "import sys" not in "\n".join(lines)
    assert sk.index_lines(repo, include_untrusted=False) == [lines[0]]
    assert sk.index_lines(repo, max_lines=1)[1].startswith("... 2 more")


def test_prompt_block_respects_config_and_reaches_adapter_prompt(repo):
    from skein.adapters.engine import ProfileAdapter
    from skein.adapters.profiles import CLAUDE_CODE_PROFILE
    assert sk.prompt_block(repo) == ""  # no skills -> no block
    _skill(repo)
    sk.add(repo, "t", "greet")
    block = sk.prompt_block(repo, {"title": "x", "intent": {}})
    assert "greet  [proposed (untrusted)]" in block
    assert "skein skills run <name>" in block
    node = {"id": "n", "title": "T", "intent": {"goal": "g"}, "skills_index": block}
    prompt = ProfileAdapter(CLAUDE_CODE_PROFILE).build_prompt(node)
    assert block.strip() in prompt
    cfg = g.load_config(repo)
    cfg["skills_in_prompt"] = False
    g.config_path(repo).write_text(json.dumps(cfg))
    assert sk.prompt_block(repo) == ""


def test_supervisor_injects_index_into_backend_prompt(repo):
    from skein.supervisor import run_node
    _skill(repo)
    sk.add(repo, "t", "greet")
    sk.test(repo, "greet")
    sk.promote(repo, "greet")
    assert skein_main(["node", "add", "n1", "--title", "T", "--goal", "g",
                       "--completion", f"{PY} -c \"print('ok')\""]) == 0
    seen = {}

    class Capture:
        name = "capture"

        def build_prompt(self, node):
            seen["node"] = node
            return "p"

        def build_command(self, node):
            self.build_prompt(node)
            return [PY, "-c", "pass"]

    assert run_node(repo, "n1", "a1", adapter=Capture(), heartbeat_interval=0.2,
                    poll_interval=0.05)["outcome"] == "done"
    assert "greet  [promoted]" in seen["node"]["skills_index"]


def test_token_estimate_counts_index_not_bodies(repo):
    _skill(repo)
    sk.add(repo, "t", "greet")
    est = sk.token_estimate(repo)
    assert est["skills"] == 1
    assert 0 < est["index_tokens"] < est["body_tokens"]


# ---------------------------------------------------------------------------
# worktrees, web, install, doctor
# ---------------------------------------------------------------------------

def test_control_root_resolves_main_repo_from_worktree(repo, monkeypatch):
    from skein import worktree as wt
    from skein.cli import control_root
    assert skein_main(["node", "add", "n1", "--title", "T", "--goal", "g",
                       "--completion", "true"]) == 0
    from skein import claim as c
    c.claim_node(repo, "n1", "a1")
    path, _, _ = wt.ensure_worktree(repo, "n1")
    monkeypatch.chdir(path)
    assert os.path.realpath(control_root()) == os.path.realpath(str(repo))
    # a skill added from inside the worktree lands in the main registry
    _skill(repo, "from-wt")
    assert skein_main(["skills", "add", "from-wt"]) == 0
    assert sk.registry(repo)["from-wt"]["stage"] == "proposed"
    assert not (path / ".skein" / "skills" / "from-wt.py").exists()


def test_api_skills_endpoint(repo):
    import threading
    import urllib.request
    from skein.serve import make_server
    _skill(repo)
    sk.add(repo, "t", "greet")
    srv = make_server(str(repo), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/skills"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
    finally:
        srv.shutdown()
    assert data["skills"][0]["name"] == "greet"
    assert data["skills"][0]["stage"] == "proposed"
    assert data["index"][0].startswith("greet  [proposed (untrusted)]")


def test_install_writes_lean_skill_md(repo, capsys):
    assert skein_main(["skills", "install"]) == 0
    dest = repo / ".claude" / "skills" / "skein" / "SKILL.md"
    text = dest.read_text()
    assert text.startswith("---\nname: skein\n")
    assert "skein skills index" in text and "skein skills run" in text
    assert len(text) < 2500  # lean core, references on demand
    assert skein_main(["skills", "install", "--platform", "codex"]) == 0
    assert (repo / ".codex" / "skills" / "skein" / "SKILL.md").exists()


def test_cli_list_and_index(repo, capsys):
    assert skein_main(["skills", "list"]) == 0
    assert "no skills" in capsys.readouterr().out
    _skill(repo)
    sk.add(repo, "t", "greet")
    assert skein_main(["skills", "list"]) == 0
    out = capsys.readouterr().out
    assert "greet" in out and "proposed" in out and "never sent to agents" in out
    assert skein_main(["skills", "index"]) == 0
    assert capsys.readouterr().out.startswith("greet  [proposed (untrusted)]")
    assert skein_main(["skills", "show", "greet"]) == 0
    assert "import sys" in capsys.readouterr().out
    assert skein_main(["doctor"]) == 0
