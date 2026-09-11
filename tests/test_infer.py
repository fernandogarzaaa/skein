"""Blast-radius inference tests (heuristic suggestions, never applied)."""

import subprocess

import pytest

from skein import graph as g
from skein.infer import keywords, suggest_blast_radius
from skein.cli import main as skein_main


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    for f in ["src/auth/login.py", "src/auth/session.py", "src/auth/token.py",
              "src/billing/invoice.py", "README.md"]:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    monkeypatch.chdir(tmp_path)
    assert skein_main(["init"]) == 0
    return tmp_path


def test_keywords_drop_stopwords():
    keys = keywords("Add session login for users with email")
    assert "session" in keys and "login" in keys
    assert "for" not in keys and "add" not in keys


def test_suggest_auth_globs(repo):
    assert skein_main(["node", "add", "a1", "--title", "Auth login",
                       "--goal", "Implement session login and token refresh",
                       "--completion", "true"]) == 0
    result = suggest_blast_radius(str(repo), "a1")
    globs = [s["glob"] for s in result["suggestions"]]
    assert globs, "expected at least one suggestion"
    assert globs[0] == "src/auth/**/*.py", globs
    assert "billing" not in " ".join(globs)


def test_suggest_nothing_without_match(repo):
    assert skein_main(["node", "add", "z9", "--title", "Zebra",
                       "--goal", "Reticulate quantum zebras",
                       "--completion", "true"]) == 0
    result = suggest_blast_radius(str(repo), "z9")
    assert result["suggestions"] == []


def test_inherited_and_declared_shown(repo):
    assert skein_main(["node", "add", "p1", "--title", "Parent",
                       "--goal", "billing invoice work",
                       "--completion", "true",
                       "--blast-radius", "src/billing/**"]) == 0
    assert skein_main(["node", "add", "c1", "--title", "Child",
                       "--goal", "child work", "--completion", "true",
                       "--depends-on", "p1"]) == 0
    result = suggest_blast_radius(str(repo), "c1")
    assert result["inherited"] == ["src/billing/**"]
    assert result["declared"] == []
    # inference never writes: no events appended by suggesting
    n_before = len(g.load_events(str(repo)))
    suggest_blast_radius(str(repo), "c1")
    assert len(g.load_events(str(repo))) == n_before


def test_unknown_node(repo):
    with pytest.raises(ValueError, match="unknown node"):
        suggest_blast_radius(str(repo), "nope")
