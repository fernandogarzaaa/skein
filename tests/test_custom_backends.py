"""Bring-your-own-backend: custom JSON profiles for any CLI.

No code changes, no new orchestration: `skein backends add` registers an
arbitrary executable (agent CLI, router binary, wrapper script) as a data
profile. Model providers and orchestration platforms plug in the same way -
through whatever headless CLI fronts them. Verified state for custom
profiles is always the owner's claim (default False).
"""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skein import graph as g
from skein.adapters import engine as engine_mod
from skein.adapters.engine import ProfileAdapter
from skein.adapters.profiles import (
    get_profile, list_profiles, load_custom_profiles, profile_from_dict,
    render_backends_table,
)
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
    # Isolate user-level custom profiles; repo scope is the tmp repo itself.
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path / "home") if p.startswith("~") else p)
    assert skein_main(["init"]) == 0
    return tmp_path


def _node(**kw):
    base = {"id": "n", "title": "T",
            "intent": {"goal": "g", "context": "c", "constraints": "",
                       "completion": "x"}}
    base.update(kw)
    return base


def test_profile_from_dict_round_trip_and_validation():
    p = profile_from_dict({
        "name": "mycli", "binary": "mycli", "prompt_mode": "flag",
        "headless_flag": ["-p"], "approval_bypass_flag": ["--yes"],
        "config_flags": {"model": ["--model"]}, "required_config": ["model"],
        "parser": "stream_json", "source_note": "test",
    })
    assert p.name == "mycli" and p.required_config == ["model"]
    cmd = ProfileAdapter(p, binary="mycli",
                         backend_config={"model": "m"}).build_command(_node())
    assert cmd == ["mycli", "-p", ProfileAdapter(p).build_prompt(_node()),
                   "--yes", "--model", "m"]
    with pytest.raises(ValueError, match="prompt_mode"):
        profile_from_dict({"name": "x", "binary": "x", "prompt_mode": "bogus"})
    with pytest.raises(ValueError, match="unknown parser"):
        profile_from_dict({"name": "x", "binary": "x", "parser": "bogus"})
    with pytest.raises(ValueError, match="'name' and 'binary'"):
        profile_from_dict({"name": "x"})


def test_backends_add_list_remove(repo, capsys):
    assert skein_main(["backends", "add", "--name", "mycli", "--binary", "mycli",
                       "--prompt-mode", "flag", "--headless=-p",
                       "--source-note", "test backend"]) == 0
    path = repo / ".skein" / "backends" / "mycli.json"
    assert path.exists()
    assert json.loads(path.read_text())["binary"] == "mycli"
    out = render_backends_table(str(repo))
    assert "mycli" in out and "custom profile (repo:mycli.json)" in out
    assert skein_main(["backends", "list"]) == 0
    assert "mycli" in capsys.readouterr().out
    # node add/run validation sees custom profiles too
    assert skein_main(["node", "add", "m1", "--title", "T", "--goal", "g",
                       "--completion", "true", "--backend", "mycli"]) == 0
    assert g.load_graph(str(repo))["m1"]["backend"] == "mycli"
    # builtins cannot be removed; unknown customs fail cleanly
    assert skein_main(["backends", "remove", "claude_code"]) == 1
    assert skein_main(["backends", "remove", "nope"]) == 1
    assert skein_main(["backends", "remove", "mycli"]) == 0
    assert not path.exists()


def test_custom_profile_mocked_run(repo):
    assert skein_main(["backends", "add", "--name", "router", "--binary", "router-bin",
                       "--headless", "go", "--parser", "stream_json_relaxed"]) == 0
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout='{"result": "routed ok"}\n', stderr="",
                               returncode=0)

    profile = get_profile("router", str(repo))
    assert profile.verified is False
    with patch.object(engine_mod.subprocess, "run", fake_run):
        code, out = ProfileAdapter(profile).run(_node(), ".")
    assert code == 0
    assert out == "routed ok"
    assert seen["cmd"][:2] == ["router-bin", "go"]
    assert seen["cmd"][-1] == ProfileAdapter(profile).build_prompt(_node())


def test_broken_custom_file_skipped(repo, capsys):
    d = repo / ".skein" / "backends"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "badprofile.json").write_text('{"name": "bad"}', encoding="utf-8")
    profiles = load_custom_profiles(str(repo))
    assert profiles == {}
    # registry still works; warning went to stderr, builtin intact
    assert get_profile("claude_code", str(repo)).verified is True
    assert "mycli" not in [p.name for p in list_profiles(str(repo))]
    err = capsys.readouterr().err
    assert "broken.json" in err and "badprofile.json" in err


def test_user_scope_and_repo_precedence(repo, tmp_path, monkeypatch):
    home = tmp_path / "home" / ".config" / "skein" / "backends"
    home.mkdir(parents=True)
    (home / "mine.json").write_text(json.dumps(
        {"name": "mine", "binary": "mine-bin"}), encoding="utf-8")
    assert get_profile("mine", str(repo)).binary == "mine-bin"
    # repo scope wins over user scope on name clash
    assert skein_main(["backends", "add", "--name", "mine", "--binary", "repo-bin",
                       "--scope", "repo", "--overwrite"]) == 0
    assert get_profile("mine", str(repo)).binary == "repo-bin"
