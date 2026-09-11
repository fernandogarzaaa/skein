"""Stage 8b: codex profile tests.

Real `codex` binary is not installed in this environment, so end-to-end
runs use a stub via SKEIN_CODEX_BIN — the same pattern (and the same
honesty) as the v0.1 SKEIN_CLAUDE_BIN stub: the stub proves real process
spawn, argv assembly, worktree isolation, evidence capture, and the
done transition. It does NOT prove codex's real CLI accepts this
invocation shape; the profile stays verified=False for that reason.
"""

import os
import stat
import subprocess

import pytest

from skein import graph as g
from skein.adapters.engine import ProfileAdapter
from skein.adapters.profiles import get_profile
from skein.cli import main as skein_main


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


def test_codex_argv_construction():
    profile = get_profile("codex")
    assert profile.verified is False
    node = {"id": "n", "title": "T",
            "intent": {"goal": "g", "context": "c", "constraints": "",
                       "completion": "x"}}
    adapter = ProfileAdapter(profile, binary="codex")
    cmd = adapter.build_command(node)
    assert cmd[0] == "codex"
    assert cmd[1:3] == ["exec", "--full-auto"]
    assert cmd[3] == adapter.build_prompt(node)
    assert len(cmd) == 4


def test_codex_binary_env_override(monkeypatch):
    profile = get_profile("codex")
    monkeypatch.setenv("SKEIN_CODEX_BIN", "/tmp/fake-codex")
    assert ProfileAdapter(profile).resolve_binary() == "/tmp/fake-codex"


@pytest.mark.skipif(os.name == "nt",
                    reason="stub is a POSIX shell script; Windows argv/parsing "
                           "covered by mocked tests, real-binary verification pending")
def test_codex_stub_end_to_end(repo, monkeypatch):
    """Real run_node through the real ProfileAdapter engine against a stub
    binary: proves spawn, worktree, evidence, and done — not codex's real
    CLI shape (see module docstring)."""
    stub = repo / "stub-codex.sh"
    stub.write_text("#!/bin/sh\ntouch hello.txt\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SKEIN_CODEX_BIN", str(stub))

    assert skein_main(["node", "add", "cx-1", "--title", "T", "--goal", "write file",
                       "--completion", "test -f hello.txt",
                       "--backend", "codex"]) == 0
    node = g.load_graph(str(repo))["cx-1"]
    assert node["backend"] == "codex"

    from skein.supervisor import run_node
    result = run_node(str(repo), "cx-1", "agent-x", adapter=None,
                      heartbeat_interval=0.2, poll_interval=0.05)
    assert result["outcome"] == "done"
    node = g.load_graph(str(repo))["cx-1"]
    assert node["status"] == "done"
    assert node["worktree"]["branch"] == "skein/cx-1"
    assert (repo / ".skein" / "worktrees" / "cx-1" / "hello.txt").exists()
    assert any(e["command"] == "test -f hello.txt" and e["exit_code"] == 0
               for e in node["evidence"])


# ---------- Stage 8c: gemini_cli, opencode, cursor_agent (mocked only) ----------

from unittest.mock import patch
from types import SimpleNamespace

from skein.adapters import engine as engine_mod
from skein.adapters.profiles import (
    BackendProfile, cursor_stream_parser, gemini_stream_parser,
    passthrough_parser,
)


def _node(**kw):
    base = {"id": "n", "title": "T",
            "intent": {"goal": "g", "context": "c", "constraints": "",
                       "completion": "x"}}
    base.update(kw)
    return base


def test_claude_legacy_argv_preserved():
    """Stage 8a regression pin: migrated profile builds byte-identical argv."""
    from skein.adapters.claude_code import ClaudeCodeAdapter
    node = _node()
    expected_prompt = ClaudeCodeAdapter().build_prompt(node)
    assert ClaudeCodeAdapter().build_command(node) == [
        "claude", "-p", expected_prompt,
        "--dangerously-skip-permissions", "--output-format", "text"]
    assert ClaudeCodeAdapter(extra_args=["--model", "x"]).build_command(node)[-2:] == [
        "--model", "x"]


def test_passthrough_parser_shape():
    r = passthrough_parser("out", "err", 3)
    assert (r.exit_code, r.output) == (3, "out\n--- stderr ---\nerr")
    r = passthrough_parser("out", "", 0)
    assert (r.exit_code, r.output) == (0, "out")


def test_run_missing_binary_message_shape():
    from skein.adapters.claude_code import ClaudeCodeAdapter
    with patch.object(engine_mod.subprocess, "run",
                      side_effect=FileNotFoundError("nope")):
        code, out = ClaudeCodeAdapter(binary="nope-bin").run(_node(), ".")
    assert code == 127
    assert out.startswith("nope-bin binary not found: nope-bin:")


def test_gemini_argv_and_parser():
    profile = get_profile("gemini_cli")
    assert profile.verified is False
    node = _node()
    cmd = ProfileAdapter(profile, binary="gemini").build_command(node)
    assert cmd[:2] == ["gemini", "-p"]
    assert cmd[3:6] == ["--yolo", "--output-format", "stream-json"]
    assert cmd[2] == ProfileAdapter(profile).build_prompt(node)
    canned = ('{"response": {"candidates": [{"content": {"parts": '
              '[{"text": "hello "}]}}]}}\n'
              '{"response": {"candidates": [{"content": {"parts": '
              '[{"text": "world"}]}}]}}\n'
              'not json at all\n')
    r = gemini_stream_parser(canned, "", 0)
    assert r.exit_code == 0
    assert r.output == "hello \nworld\nnot json at all"


def test_gemini_run_mocked():
    profile = get_profile("gemini_cli")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return SimpleNamespace(stdout='{"text": "did it"}\n', stderr="",
                               returncode=0)

    with patch.object(engine_mod.subprocess, "run", fake_run):
        code, out = ProfileAdapter(profile, binary="gemini").run(_node(), "/tmp/wt")
    assert code == 0
    assert out == "did it"
    assert seen["cmd"][:2] == ["gemini", "-p"]
    assert seen["cwd"] == "/tmp/wt"


def test_opencode_argv_requires_model():
    profile = get_profile("opencode")
    assert profile.verified is False
    node = _node(backend_config={"model": "openai/gpt-5"})
    cmd = ProfileAdapter(profile, binary="opencode").build_command(node)
    assert cmd[:2] == ["opencode", "run"]
    assert cmd[2:4] == ["--model", "openai/gpt-5"]
    assert cmd[-1] == ProfileAdapter(profile).build_prompt(node)
    with pytest.raises(ValueError, match="requires backend_config\\['model'\\]"):
        ProfileAdapter(profile, binary="opencode").build_command(_node())


def test_opencode_config_merge_prefers_adapter():
    profile = get_profile("opencode")
    node = _node(backend_config={"model": "from-node"})
    adapter = ProfileAdapter(profile, binary="opencode",
                             backend_config={"model": "from-adapter"})
    assert "--model" in adapter.build_command(node)
    assert "from-adapter" in adapter.build_command(node)


def test_cursor_argv_and_parser():
    profile = get_profile("cursor_agent")
    assert profile.verified is False
    node = _node()
    adapter = ProfileAdapter(profile, binary="cursor-agent")
    cmd = adapter.build_command(node)
    assert cmd[:4] == ["cursor-agent", "-p", "--output-format", "stream-json"]
    assert cmd[-1] == adapter.build_prompt(node)
    canned = ('{"type": "assistant", "message": "working"}\n'
              '{"type": "result", "result": "all done"}\n')
    r = cursor_stream_parser(canned, "warn", 0)
    assert r.exit_code == 0
    assert r.output == "working\nall done\n--- stderr ---\nwarn"


def test_stdin_prompt_mode():
    profile = BackendProfile(name="stdin_probe", binary="probe",
                             prompt_mode="stdin", headless_flag=["go"],
                             verified=False)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        seen["cmd"] = cmd
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    node = _node()
    adapter = ProfileAdapter(profile)
    assert "ok" not in " ".join(adapter.build_command(node))
    with patch.object(engine_mod.subprocess, "run", fake_run):
        code, out = adapter.run(node, ".")
    assert (code, out) == (0, "ok")
    assert seen["input"] == adapter.build_prompt(node)


# ---------- Stage 8d: backends list + README matrix in lockstep ----------

from skein.adapters.profiles import list_profiles, render_backends_table


def _readme_matrix_block():
    from pathlib import Path
    text = Path(__file__).resolve().parent.parent.joinpath("README.md").read_text(
        encoding="utf-8")
    begin = "<!-- skein:backends:begin -->\n```\n"
    end = "\n```\n<!-- skein:backends:end -->"
    assert begin in text and end in text
    return text.split(begin)[1].split(end)[0]


def test_backends_list_matches_readme(monkeypatch, tmp_path):
    # Isolate user-level custom profiles so the comparison is deterministic.
    import os
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path) if p.startswith("~") else p)
    assert render_backends_table() == _readme_matrix_block()


def test_backends_list_cli_output(capsys):
    from skein.cli import main as cli_main
    assert cli_main(["backends", "list"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == render_backends_table()
    assert "claude_code" in out and "codex" in out


def test_registry_verified_state():
    by_name = {p.name: p for p in list_profiles()}
    assert by_name["claude_code"].verified is True
    for name in ("codex", "gemini_cli", "opencode", "cursor_agent", "aider"):
        assert by_name[name].verified is False, name
        assert by_name[name].source_note, name


def test_no_universal_claim():
    """The word 'universal' must not appear in shipped docs or CLI output
    without the verified/unverified distinction attached - enforced here
    by absence."""
    import io
    from contextlib import redirect_stdout
    from pathlib import Path
    from skein.cli import build_parser
    readme = Path(__file__).resolve().parent.parent.joinpath("README.md").read_text(
        encoding="utf-8")
    assert "universal" not in readme.lower()
    buf = io.StringIO()
    with redirect_stdout(buf):
        from skein.cli import main as cli_main
        cli_main(["backends", "list"])
    assert "universal" not in buf.getvalue().lower()
    assert "universal" not in build_parser().format_help().lower()


# ---------- Stage 8e: sixth profile via schema alone ----------

def test_aider_profile_schema_only():
    """Aider was added as pure data: correct argv, default parser, and no
    orchestration modules touched (claim/worktree/verify unchanged)."""
    profile = get_profile("aider")
    assert profile.verified is False
    assert profile.prompt_mode == "flag"
    node = _node()
    adapter = ProfileAdapter(profile, binary="aider")
    cmd = adapter.build_command(node)
    assert cmd[:2] == ["aider", "--message"]
    assert cmd[2] == adapter.build_prompt(node)
    assert cmd[3:] == ["--yes"]
    assert profile.output_parser is passthrough_parser
