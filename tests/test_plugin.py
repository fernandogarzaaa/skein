import argparse
import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from skein.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
MARKDOWN = sorted(PLUGIN.rglob("*.md"))


def _load(path):
    return json.loads(path.read_text())


def _frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "missing frontmatter"
    return dict(
        line.split(":", 1) for line in m.group(1).splitlines() if ":" in line
    )


def test_manifests_agree():
    market = _load(ROOT / ".claude-plugin" / "marketplace.json")
    entry = market["plugins"][0]
    src = (ROOT / entry["source"]).resolve()
    manifest = _load(src / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == entry["name"]


def test_version_matches_pyproject():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    manifest = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["version"] == py["project"]["version"]


@pytest.mark.parametrize("path", sorted((PLUGIN / "commands").glob("*.md")), ids=lambda p: p.name)
def test_commands_have_description(path):
    assert _frontmatter(path.read_text())["description"].strip()


def _subparsers(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def _code_chunks(text):
    fenced = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    yield from (ln for block in fenced for ln in block.splitlines())
    yield from re.findall(r"`([^`\n]+)`", prose)


def _invocations(text):
    for chunk in _code_chunks(text):
        for seg in re.split(r"&&|\|\||[|;]|>", chunk):
            toks = seg.split()
            while toks and re.fullmatch(r"[A-Z_]+=\S*", toks[0]):
                toks.pop(0)
            if len(toks) > 1 and toks[0] == "skein":
                yield toks


def _validate(toks, where):
    parser = build_parser()
    args = toks[1:]
    for tok in args:
        subs = _subparsers(parser)
        if tok.startswith("--"):
            flag = tok.split("=", 1)[0]
            known = {o for a in parser._actions for o in a.option_strings}
            assert flag in known, f"{where}: {flag} is not a flag of {parser.prog}"
        elif subs:
            if tok.startswith("-"):
                continue
            assert tok in subs, f"{where}: {tok!r} is not a subcommand of {parser.prog}"
            parser = subs[tok]


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(PLUGIN)))
def test_skein_invocations_match_parser(path):
    found = list(_invocations(path.read_text()))
    for toks in found:
        _validate(toks, f"{path.name}: {' '.join(toks)}")


def test_guard_sees_invocations():
    assert sum(len(list(_invocations(p.read_text()))) for p in MARKDOWN) > 15


def test_guard_rejects_fake_flag():
    with pytest.raises(AssertionError):
        _validate(["skein", "node", "add", "x", "--nope"], "t")
    with pytest.raises(AssertionError):
        _validate(["skein", "bogus"], "t")
    with pytest.raises(AssertionError):
        _validate(["skein", "skills", "bogus"], "t")
