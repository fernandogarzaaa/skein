"""Regression tests for the usability/correctness audit fixes.

Each test pins one bug found by running skein end to end in a fresh repo:

- `skein init` left worktrees and evidence unignored, so every event
  commit recorded the worktree as a gitlink and committed evidence logs.
- the installed post-commit hook had no exec bit, so git printed an
  "ignored hook" hint on every user commit.
- `skein release <failed-node>` without --force crashed with an uncaught
  ValueError from the write-time state machine.
- a non-numeric TTL posted to the web claim API poisoned the log: every
  later `skein status` / reap raised on float("abc").
- the planner wrote the task prose into `completion`, which the gate runs
  as a shell command, so every planned node failed verification.
- the canvas sent the current status back on every save, which the
  server rejects for done/claimed nodes.
- `skein run --all` looped forever on a node whose claim kept failing
  after eligibility said yes.
"""

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from skein import claim as c
from skein import edits
from skein import graph as g
from skein import planner
from skein import worktree as wt
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


class FakeAdapter:
    name = "fake"

    def __init__(self, snippet):
        self.snippet = snippet

    def build_prompt(self, node):
        return "fake"

    def build_command(self, node):
        return [PY, "-c", self.snippet]


def _add(nid, completion="true", **extra):
    args = ["node", "add", nid, "--title", nid, "--goal", "g",
            "--completion", completion]
    for k, v in extra.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    assert skein_main(args) == 0


# ---------------------------------------------------------------------------
# init / commit scoping
# ---------------------------------------------------------------------------

def test_init_gitignores_worktrees_and_evidence(repo):
    text = (repo / ".gitignore").read_text()
    assert ".skein/worktrees/" in text
    assert ".skein/evidence/" in text
    # committed with the init commit
    assert ".gitignore" in git(repo, "ls-files").stdout.split()
    # idempotent: a second init adds nothing
    assert skein_main(["init"]) == 0
    assert (repo / ".gitignore").read_text() == text


def test_init_preserves_existing_gitignore(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    (tmp_path / ".gitignore").write_text("node_modules/")  # no trailing newline
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-m", "init")
    monkeypatch.chdir(tmp_path)
    assert skein_main(["init"]) == 0
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/"
    assert ".skein/worktrees/" in lines and ".skein/evidence/" in lines


def test_run_never_commits_worktree_or_evidence(repo):
    from skein.supervisor import run_node
    _add("n1", completion=f"{PY} -c \"print('ok')\"")
    result = run_node(repo, "n1", "a1",
                      adapter=FakeAdapter("open('x.txt','w').write('hi')"),
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    tracked = git(repo, "ls-files", ".skein").stdout.split()
    assert not any(p.startswith(".skein/worktrees") for p in tracked), tracked
    assert not any(p.startswith(".skein/evidence") for p in tracked), tracked
    assert (repo / ".skein" / "worktrees" / "n1").is_dir()
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


def test_commit_log_excludes_scratch_even_without_gitignore(repo):
    # A repo that predates the .gitignore rules must still not have its
    # evidence/worktrees swept up by the pathspec-scoped commits.
    (repo / ".gitignore").unlink()
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qam", "drop ignore")
    ev = repo / ".skein" / "evidence" / "stray.log"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("x")
    _add("n1")
    tracked = git(repo, "ls-files", ".skein").stdout.split()
    assert ".skein/evidence/stray.log" not in tracked
    assert ".skein/log.ndjson" in tracked


@pytest.mark.skipif(os.name == "nt", reason="exec bit is a POSIX concept")
def test_init_hook_is_executable(repo):
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.exists()
    assert os.access(str(hook), os.X_OK)


def test_doctor_flags_tracked_scratch_dirs(repo, capsys):
    ev = repo / ".skein" / "evidence" / "old.log"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("x")
    git(repo, "add", "-f", str(ev))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qm", "legacy tracked evidence")
    assert skein_main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL worktrees/evidence untracked" in out
    assert "git rm -r --cached .skein/evidence" in out


# ---------------------------------------------------------------------------
# release of parked nodes
# ---------------------------------------------------------------------------

def _fail_node(repo, nid):
    from skein.supervisor import run_node
    _add(nid, completion=f"{PY} -c \"raise SystemExit(1)\"", max_retries=0)
    r = run_node(repo, nid, "a1", adapter=FakeAdapter("pass"),
                 heartbeat_interval=0.2, poll_interval=0.05)
    assert r["outcome"] == "failed"
    assert g.load_graph(repo)[nid]["status"] == "failed"


def test_release_failed_node_needs_force(repo, capsys):
    _fail_node(repo, "f1")
    with pytest.raises(c.ClaimError, match="no live claim"):
        c.release_node(repo, "f1", actor="human")
    # the CLI reports it cleanly instead of a traceback
    assert skein_main(["release", "f1"]) == 1
    err = capsys.readouterr().err
    assert "use --force" in err
    assert skein_main(["release", "f1", "--force"]) == 0
    assert g.load_graph(repo)["f1"]["status"] == "unclaimed"


def test_run_on_failed_node_explains(repo, capsys):
    _fail_node(repo, "f2")
    assert skein_main(["run", "f2", "--agent-id", "a1"]) == 1
    err = capsys.readouterr().err
    assert "is 'failed', not runnable" in err
    assert "--status unclaimed" in err


# ---------------------------------------------------------------------------
# TTL validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", 0, -5, 2.5, True, None, ""])
def test_coerce_ttl_rejects_bad_values(bad):
    assert c.coerce_ttl(bad) is None
    assert c.coerce_ttl(bad, 1800) == 1800


def test_coerce_ttl_accepts_ints_and_numeric_strings():
    assert c.coerce_ttl(60) == 60
    assert c.coerce_ttl("60") == 60
    assert c.coerce_ttl(60.0) == 60


def test_claim_rejects_bad_ttl(repo):
    _add("t1")
    for bad in ("abc", 0, -1, 2.5):
        with pytest.raises(c.ClaimError, match="invalid ttl_seconds"):
            c.claim_node(repo, "t1", "h", ttl_seconds=bad)
    assert g.load_graph(repo)["t1"]["status"] == "unclaimed"


def test_status_survives_poisoned_ttl_in_log(repo, capsys):
    # A claim record with a bad TTL (synced from elsewhere, or written by
    # an older skein) must not crash every reader.
    _add("t2")
    g.append_event(repo, "h", "claimed", "t2",
                   {"holder": "h", "attempt_id": "a", "claim_token": "tok",
                    "ttl_seconds": "abc"})
    node = g.load_graph(repo)["t2"]
    assert c.is_expired(node) is False
    assert c.reap_expired(repo) == []
    assert skein_main(["status"]) == 0
    assert "t2" in capsys.readouterr().out


def test_web_claim_bad_ttl_is_400(repo):
    from skein.serve import make_server
    _add("t3")
    srv = make_server(str(repo), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/nodes/t3/claim"
        req = urllib.request.Request(
            url, data=json.dumps({"holder": "h", "ttl_seconds": "abc"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 400
    finally:
        srv.shutdown()
    assert g.load_graph(repo)["t3"]["status"] == "unclaimed"
    assert skein_main(["status"]) == 0


# ---------------------------------------------------------------------------
# planner: completion is a command, not prose
# ---------------------------------------------------------------------------

def test_planner_puts_task_prose_in_context_not_completion(repo):
    d = planner.parse_heuristic("Build auth: 1. design schema 2. implement login",
                                goal="Build auth: 1. design schema 2. implement login")
    assert [n["id"] for n in d["nodes"]] == ["design-schema", "implement-login"]
    for n in d["nodes"]:
        assert n["completion"] == ""
        assert "design schema" in n["context"] or "implement login" in n["context"]
    assert any("no completion command" in w for w in d["warnings"])
    rendered = planner.render_draft(d)
    assert "(none - set one before running)" in rendered
    path = planner.save_draft(repo, d)
    created, _ = planner.apply_draft(repo, "t", planner.load_draft(path), path.name)
    node = g.load_graph(repo)["design-schema"]
    assert node["intent"]["completion"] == ""
    assert "Complete this task: design schema" in node["intent"]["context"]


def test_planner_git_log_and_single_node_drafts_have_empty_completion(repo):
    d = planner.plan_from_git_log(repo, 5)
    assert all(n["completion"] == "" for n in d["nodes"])
    assert all(n["context"] for n in d["nodes"])
    single = planner.parse_heuristic("just one prose task", goal="just one prose task")
    assert len(single["nodes"]) == 1
    assert single["nodes"][0]["completion"] == ""


# ---------------------------------------------------------------------------
# edits: canvas-shaped payloads
# ---------------------------------------------------------------------------

def test_edit_drops_unchanged_status(repo):
    from skein.supervisor import run_node
    _add("e1", completion=f"{PY} -c \"print('ok')\"")
    r = run_node(repo, "e1", "a1", adapter=FakeAdapter("pass"),
                 heartbeat_interval=0.2, poll_interval=0.05)
    assert r["outcome"] == "done"
    # the canvas form re-sends the current status with every save
    assert edits.edit_node(repo, "web", "e1",
                           {"title": "renamed", "status": "done"}) == "edited"
    node = g.load_graph(repo)["e1"]
    assert node["title"] == "renamed" and node["status"] == "done"
    with pytest.raises(ValueError, match="cannot be set by direct edit"):
        edits.edit_node(repo, "web", "e1", {"status": "failed"})


def test_edit_rejects_scalar_list_fields(repo):
    _add("e2")
    with pytest.raises(ValueError, match="blast_radius must be a list"):
        edits.edit_node(repo, "web", "e2", {"blast_radius": "src/**"})
    with pytest.raises(ValueError, match="depends_on must be a list"):
        edits.edit_node(repo, "web", "e2", {"depends_on": "e1"})
    with pytest.raises(ValueError, match="backend_config must be an object"):
        edits.edit_node(repo, "web", "e2", {"backend_config": "model=x"})
    with pytest.raises(ValueError, match="intent must be an object"):
        edits.edit_node(repo, "web", "e2", {"intent": "goal"})
    with pytest.raises(ValueError, match="depends_on must be a list"):
        edits.add_node(repo, "web", "e3", depends_on="e2")


def test_canvas_status_select_offers_manual_statuses_only():
    from skein.serve import _index_html
    html = _index_html()
    assert 'MANUAL_STATUSES = ["unclaimed", "blocked", "needs_human"]' in html
    assert '"claimed","in_progress"' not in html


def test_node_page_shows_evidence_commands(repo):
    from skein.serve import _node_html
    from skein.supervisor import run_node
    _add("p1", completion=f"{PY} -c \"print('ok')\"")
    run_node(repo, "p1", "a1", adapter=FakeAdapter("pass"),
             heartbeat_interval=0.2, poll_interval=0.05)
    page = _node_html(str(repo), "p1").decode()
    assert "print(&#x27;ok&#x27;)" in page or "print('ok')" in page
    assert "&lt;adapter:fake&gt;" in page
    assert "<th>command</th><th>exit</th>" in page


# ---------------------------------------------------------------------------
# run paths
# ---------------------------------------------------------------------------

def test_run_all_does_not_loop_on_persistent_skip(repo, monkeypatch):
    from skein import cli
    _add("s1")
    calls = []

    def fake_run(root, nid, holder, actor, args):
        calls.append(nid)
        return "skipped: base unavailable"

    monkeypatch.setattr(cli, "_run_one_node", fake_run)
    rc = skein_main(["run", "--all", "--agent-id", "a1"])
    assert rc == 0
    assert calls == ["s1"]


def test_cli_run_reports_worktree_error_cleanly(repo, monkeypatch, capsys):
    from skein import supervisor
    _add("w1")

    def boom(*a, **k):
        raise wt.BaseCommitUnavailable("base ref gone")

    monkeypatch.setattr(supervisor.wt, "ensure_worktree", boom)
    assert skein_main(["run", "w1", "--agent-id", "a1"]) == 1
    assert "base ref gone" in capsys.readouterr().err
    # the claim taken for this run was handed back, not left to expire
    node = g.load_graph(repo)["w1"]
    assert node["status"] == "unclaimed"
    assert "worktree setup failed" in (node.get("handoff_note") or "")


def test_ensure_worktree_refuses_stale_pinned_base(repo):
    from skein.supervisor import run_node
    _add("dep", completion=f"{PY} -c \"print('ok')\"")
    _add("child", depends_on="dep")
    assert run_node(repo, "dep", "a1", adapter=FakeAdapter("open('a','w').write('1')"),
                    heartbeat_interval=0.2, poll_interval=0.05)["outcome"] == "done"
    first = g.load_graph(repo)["dep"]["result"]["commit"]
    # claim the child now: its base is pinned to the first result commit
    claimed = c.claim_node(repo, "child", "a2")
    assert claimed["claim"]["base_branch"] == first
    # the dependency re-runs and produces a new result commit
    assert edits.edit_node(repo, "h", "dep", {"status": "unclaimed"}) == "edited"
    wt.remove_worktree(repo, "dep", delete_branch=True)
    assert run_node(repo, "dep", "a1", adapter=FakeAdapter("open('b','w').write('2')"),
                    heartbeat_interval=0.2, poll_interval=0.05)["outcome"] == "done"
    assert g.load_graph(repo)["dep"]["result"]["commit"] != first
    with pytest.raises(wt.WorktreeError, match="dependency re-ran"):
        wt.ensure_worktree(repo, "child")


def test_checkpoint_git_failure_fails_attempt(repo, monkeypatch):
    from skein import supervisor
    from skein.supervisor import run_node
    _add("g1", completion=f"{PY} -c \"print('ok')\"", max_retries=0)

    def boom(*a, **k):
        raise RuntimeError("result commit failed: disk full")

    monkeypatch.setattr(supervisor, "_checkpoint_result", boom)
    r = run_node(repo, "g1", "a1", adapter=FakeAdapter("pass"),
                 heartbeat_interval=0.2, poll_interval=0.05)
    assert r["outcome"] == "failed"
    node = g.load_graph(repo)["g1"]
    assert node["status"] == "failed"
    assert "disk full" in node["handoff_note"]
