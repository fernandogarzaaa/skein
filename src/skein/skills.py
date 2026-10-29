"""Agent-authored skills as scripts, with an evidence-gated trust lifecycle.

A skill is one executable script under `.skein/skills/<name>.<ext>` with
a short comment header (name, description, usage, triggers, self-test).
Two ideas are borrowed deliberately:

- From ADAM (adam-skills): a skill moves through a strict lifecycle,
  proposed -> tested -> promoted (or rejected / retired), every
  transition is recorded in the append-only event log, and nothing is
  trusted without test evidence. Trust is bound to the script's content
  hash: editing a promoted script silently demotes it to `modified` until
  it is re-tested and re-promoted, so a skill can never be changed and
  keep its old trust (ADAM's "evolve archives the prior procedure").
- From graphify: agents never read skill bodies by default. They get a
  one-line-per-skill index (name, stage, description, usage) in their
  prompt and run skills through `skein skills run <name>`; the full
  script is a reference loaded on demand (`skein skills show`). This is
  what keeps the token cost of "having skills" flat as the library grows.

State model: the script file is the procedure; the log is the trust
record. `skill` events are anchored at the reserved node id "skein-skill"
and carry {action, name, hash, ...}. No node state is derived from them
(apply_event ignores the type); the registry below reduces them.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import graph as g
from . import ids
from . import redact

SKILL_ANCHOR = "skein-skill"
SKILLS_DIRNAME = "skills"

STAGES = ("proposed", "tested", "promoted", "rejected", "retired")
# Derived-only pseudo stages: the file on disk disagrees with the log.
DISK_STAGES = ("modified", "missing")

# Which interpreter runs which extension. Anything else runs directly and
# must be executable (the add path sets the exec bit on POSIX).
_INTERPRETERS = {
    ".py": lambda path: [sys.executable, str(path)],
    ".sh": lambda path: ["sh", str(path)],
    ".bash": lambda path: ["bash", str(path)],
    ".ps1": lambda path: ["powershell", "-ExecutionPolicy", "Bypass",
                          "-File", str(path)],
}

_HEADER_KEYS = ("name", "description", "usage", "triggers", "self-test",
                "self_test", "language")
_HEADER_RE = re.compile(
    r"^\s*(?:#|//|--|;|::|REM)\s*(?:skein[- ]skill:\s*)?"
    r"(name|description|usage|triggers|self[-_]test|language)\s*[:=]\s*(.*?)\s*$",
    re.IGNORECASE)
_HEADER_SCAN_LINES = 40

DEFAULT_SELF_TEST_ARGV = ["--self-test"]
_CHARS_PER_TOKEN = 4  # the usual rough estimate (graphify's benchmark uses it)


class SkillError(Exception):
    pass


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def skill_config(repo_root: str | Path) -> Dict[str, Any]:
    cfg = g.load_config(repo_root)
    return {
        "promote_threshold": _as_float(cfg.get("skill_promote_threshold"), 1.0),
        "test_timeout": _as_float(cfg.get("skill_test_timeout_seconds"), 120.0),
        "run_timeout": _as_float(cfg.get("skill_run_timeout_seconds"), 600.0),
        "in_prompt": bool(cfg.get("skills_in_prompt", True)),
        "index_max_lines": int(_as_float(cfg.get("skills_index_max_lines"), 30)),
    }


def _as_float(value: Any, fallback: float) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return v if v >= 0 else fallback


# ---------------------------------------------------------------------------
# files + headers
# ---------------------------------------------------------------------------

def skills_dir(repo_root: str | Path) -> Path:
    return g.skein_dir(repo_root) / SKILLS_DIRNAME


def validate_name(name: str) -> str:
    try:
        return ids.validate_node_id(name)
    except ids.NodeIdError as e:
        raise SkillError(f"invalid skill name: {e}")


def skill_path(repo_root: str | Path, name: str) -> Optional[Path]:
    """The script file for `name`, whatever its extension, or None."""
    validate_name(name)
    d = skills_dir(repo_root)
    if not d.is_dir():
        return None
    matches = sorted(p for p in d.iterdir()
                     if p.is_file() and p.stem == name and not p.name.endswith(".tmp"))
    return matches[0] if matches else None


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_header(text: str) -> Dict[str, Any]:
    """Read the comment header from the top of a script.

    Keys: name, description, usage, triggers (comma-separated), self_test
    (argv string; "none" disables), language. Only the first
    _HEADER_SCAN_LINES lines are scanned, so a long script costs nothing
    extra to index.
    """
    out: Dict[str, Any] = {}
    for line in text.splitlines()[:_HEADER_SCAN_LINES]:
        m = _HEADER_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower().replace("-", "_")
        value = m.group(2).strip()
        if key == "triggers":
            out[key] = [t.strip().lower() for t in value.split(",") if t.strip()]
        else:
            out[key] = value
    return out


def self_test_argv(header: Dict[str, Any]) -> Optional[List[str]]:
    """argv appended to the skill command for its self-test, or None when
    the header says `self-test: none` (such a skill can never be promoted)."""
    raw = header.get("self_test")
    if raw is None:
        return list(DEFAULT_SELF_TEST_ARGV)
    raw = str(raw).strip()
    if raw.lower() in ("none", "no", "false", "-"):
        return None
    return raw.split() if raw else list(DEFAULT_SELF_TEST_ARGV)


def command_for(path: Path) -> List[str]:
    factory = _INTERPRETERS.get(path.suffix.lower())
    if factory is not None:
        return factory(path)
    return [str(path)]


TEMPLATE_PY = '''#!/usr/bin/env python3
# skein-skill
# name: {name}
# description: {description}
# usage: skein skills run {name} [-- ARGS]
# triggers: {triggers}
# self-test: --self-test
"""Skill body. Keep output short: whatever this prints is what the agent
reads. Exit non-zero on failure. `--self-test` must exercise the real
logic and exit 0 only when it works; it is what earns the skill trust."""
import sys


def main(argv):
    if argv[:1] == ["--self-test"]:
        # Replace with a real check of this skill's behaviour.
        print("self-test: ok")
        return 0
    print("TODO: implement {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

TEMPLATE_SH = '''#!/bin/sh
# skein-skill
# name: {name}
# description: {description}
# usage: skein skills run {name} [-- ARGS]
# triggers: {triggers}
# self-test: --self-test
set -e
if [ "$1" = "--self-test" ]; then
  # Replace with a real check of this skill's behaviour.
  echo "self-test: ok"
  exit 0
fi
echo "TODO: implement {name}"
exit 1
'''


def scaffold(repo_root: str | Path, name: str, description: str = "",
             lang: str = "py", triggers: Optional[List[str]] = None,
             overwrite: bool = False) -> Path:
    """Write a template skill file. Does not register it: the author edits
    the body, then `skein skills add <name>` records the proposal."""
    validate_name(name)
    lang = (lang or "py").lower().lstrip(".")
    if lang not in ("py", "sh"):
        raise SkillError("scaffold supports --lang py or sh; add any other "
                         "script with `skein skills add --file`")
    existing = skill_path(repo_root, name)
    if existing is not None and not overwrite:
        raise SkillError(f"skill '{name}' already exists at {existing} "
                         f"(use --overwrite)")
    d = skills_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.{lang}"
    tpl = TEMPLATE_PY if lang == "py" else TEMPLATE_SH
    path.write_text(tpl.format(
        name=name,
        description=(description or f"{name} (describe what this does in one line)"),
        triggers=", ".join(triggers or [name])), encoding="utf-8")
    _make_executable(path)
    return path


def _make_executable(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# log reduction -> registry
# ---------------------------------------------------------------------------

def _skill_events(repo_root: str | Path) -> List[Dict[str, Any]]:
    evs = [e for e in g.load_events(repo_root)
           if e.get("type") == "skill" and e.get("node_id") == SKILL_ANCHOR]
    evs.sort(key=lambda e: (e.get("timestamp", ""), e.get("seq", 0),
                            e.get("event_id", "")))
    return evs


def _empty_record(name: str) -> Dict[str, Any]:
    return {"name": name, "stage": None, "hash": None, "tests": [],
            "runs": 0, "run_failures": 0, "proposed_at": None,
            "promoted_at": None, "proposed_by": None, "reason": "",
            "archived_hashes": []}


def _reduce(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    recs: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        p = ev.get("payload") or {}
        name = p.get("name")
        action = p.get("action")
        if not isinstance(name, str) or not name:
            continue
        rec = recs.setdefault(name, _empty_record(name))
        h = p.get("hash")
        if action == "proposed":
            if rec["hash"] and rec["hash"] != h:
                rec["archived_hashes"].append(rec["hash"])
            rec.update(stage="proposed", hash=h, tests=[],
                       proposed_at=ev.get("timestamp"),
                       proposed_by=ev.get("actor"), promoted_at=None,
                       reason="")
        elif action == "tested":
            if rec["stage"] in (None, "retired", "rejected") or h != rec["hash"]:
                continue  # evidence for a different content; never counts
            rec["tests"].append({"passed": bool(p.get("passed")),
                                 "at": ev.get("timestamp"),
                                 "note": str(p.get("note") or "")[:200],
                                 "output_ref": p.get("output_ref")})
            if not p.get("passed") and rec["stage"] == "promoted":
                # a failing self-test on the trusted content revokes trust
                rec["stage"] = "tested"
                rec["promoted_at"] = None
            elif rec["stage"] != "promoted":
                rec["stage"] = "tested"
        elif action == "promoted":
            if rec["stage"] in ("tested", "promoted") and h == rec["hash"]:
                rec["stage"] = "promoted"
                rec["promoted_at"] = ev.get("timestamp")
        elif action == "rejected":
            if rec["stage"] not in (None, "retired"):
                rec["stage"] = "rejected"
                rec["reason"] = str(p.get("reason") or "")[:200]
        elif action == "retired":
            if rec["stage"] is not None:
                rec["stage"] = "retired"
                rec["reason"] = str(p.get("reason") or "")[:200]
        elif action == "run":
            rec["runs"] += 1
            if p.get("exit_code") not in (0, None):
                rec["run_failures"] += 1
    return recs


def fitness(rec: Dict[str, Any]) -> Optional[float]:
    tests = rec.get("tests") or []
    if not tests:
        return None
    return sum(1 for t in tests if t.get("passed")) / len(tests)


def registry(repo_root: str | Path) -> Dict[str, Dict[str, Any]]:
    """Log-derived trust state merged with what is on disk.

    Each record: name, stage (one of STAGES, or `modified` when the file
    differs from the recorded hash, or `missing` when it is gone),
    path, header, hash, tests, fitness, runs, run_failures, timestamps.
    Files on disk that were never registered show as stage None.
    """
    recs = _reduce(_skill_events(repo_root))
    d = skills_dir(repo_root)
    on_disk: Dict[str, Path] = {}
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file() and not p.name.endswith(".tmp") and p.stem not in on_disk:
                on_disk[p.stem] = p
    for name in set(recs) | set(on_disk):
        rec = recs.setdefault(name, _empty_record(name))
        path = on_disk.get(name)
        rec["path"] = str(path) if path else None
        rec["header"] = {}
        rec["disk_hash"] = None
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                rec["header"] = parse_header(text)
                rec["disk_hash"] = file_hash(path)
                rec["body_chars"] = len(text)
            except OSError:
                pass
        rec["log_stage"] = rec["stage"]
        if rec["stage"] in ("proposed", "tested", "promoted"):
            if path is None:
                rec["stage"] = "missing"
            elif rec["disk_hash"] != rec["hash"]:
                rec["stage"] = "modified"
        rec["fitness"] = fitness(rec)
        rec["description"] = (rec["header"].get("description")
                              or ("(no description)" if path else "(file missing)"))
        rec["usage"] = rec["header"].get("usage") or f"skein skills run {name}"
        rec["triggers"] = list(rec["header"].get("triggers") or [])
    return dict(sorted(recs.items()))


def get(repo_root: str | Path, name: str) -> Dict[str, Any]:
    validate_name(name)
    rec = registry(repo_root).get(name)
    if rec is None:
        raise SkillError(f"unknown skill '{name}'")
    return rec


# ---------------------------------------------------------------------------
# lifecycle mutations (each one is an event)
# ---------------------------------------------------------------------------

def _record(repo_root: str | Path, actor: str, payload: Dict[str, Any],
            commit: bool = True) -> Dict[str, Any]:
    return g.append_event(repo_root, actor, "skill", SKILL_ANCHOR, payload,
                          commit=commit)


def add(repo_root: str | Path, actor: str, name: str,
        source: Optional[str | Path] = None,
        overwrite: bool = False) -> Dict[str, Any]:
    """Register (propose) a skill from `source` (copied in) or from the
    file already under .skein/skills/. Validates the header and records
    a `proposed` event with the content hash. Re-adding changed content
    archives the previous hash and resets trust."""
    validate_name(name)
    d = skills_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    existing = skill_path(repo_root, name)
    if source is not None:
        src = Path(source)
        if not src.is_file():
            raise SkillError(f"source file not found: {src}")
        ext = src.suffix or ".sh"
        dest = d / f"{name}{ext}"
        if existing is not None and existing != dest and not overwrite:
            raise SkillError(f"skill '{name}' already exists as {existing.name} "
                             f"(use --overwrite to replace)")
        if existing is not None and existing != dest:
            existing.unlink()
        shutil.copyfile(src, dest)
        _make_executable(dest)
        path = dest
    else:
        if existing is None:
            raise SkillError(
                f"no file for skill '{name}' under {d}; pass --file, or "
                f"scaffold one with `skein skills new {name}`")
        path = existing
    text = path.read_text(encoding="utf-8", errors="replace")
    header = parse_header(text)
    if not header.get("description"):
        raise SkillError(
            f"{path.name} has no `# description:` header line; the index "
            f"shows exactly that one line to agents, so it is required")
    if header.get("name") and header["name"] != name:
        raise SkillError(
            f"{path.name} declares name '{header['name']}' but is being "
            f"added as '{name}'")
    if path.suffix.lower() not in _INTERPRETERS and os.name != "nt":
        if not os.access(str(path), os.X_OK):
            raise SkillError(f"{path.name} is not executable and has no "
                             f"known interpreter extension")
    h = file_hash(path)
    prev = _reduce(_skill_events(repo_root)).get(name)
    if prev and prev.get("hash") == h and prev.get("stage") in (
            "proposed", "tested", "promoted"):
        raise SkillError(f"skill '{name}' is already registered with this "
                         f"exact content (stage {prev['stage']})")
    _record(repo_root, actor, {
        "action": "proposed", "name": name, "hash": h,
        "file": path.name,
        "description": str(header.get("description"))[:200],
        "triggers": list(header.get("triggers") or [])[:20],
        "previous_hash": prev.get("hash") if prev else None,
    })
    return get(repo_root, name)


def _evidence_file(repo_root: str | Path, name: str, kind: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    from . import verify as v
    d = v.evidence_subdir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    return d / ids.evidence_name(f"skill-{name}", f"{ts}_{kind}")


def run(repo_root: str | Path, name: str, args: Optional[List[str]] = None,
        cwd: Optional[str | Path] = None, timeout: Optional[float] = None,
        record: bool = True, actor: str = "skill",
        allow_untrusted: bool = True) -> "rt.ExecutionResult":
    """Execute a skill through the bounded runtime. cwd defaults to the
    caller's working directory (an agent runs skills inside its
    worktree). Records a `skill` run event (commit=False) with exit code
    and duration so the registry can show run/failure counts."""
    from . import runtime as rt
    rec = get(repo_root, name)
    if rec.get("path") is None:
        raise SkillError(f"skill '{name}' has no script file")
    if rec["stage"] in ("retired", "rejected"):
        raise SkillError(f"skill '{name}' is {rec['stage']}"
                         + (f": {rec.get('reason')}" if rec.get("reason") else ""))
    if not allow_untrusted and rec["stage"] != "promoted":
        raise SkillError(f"skill '{name}' is '{rec['stage']}', not promoted; "
                         f"pass --untrusted to run it anyway")
    cfg = skill_config(repo_root)
    env = dict(os.environ)
    env["SKEIN_SKILL_NAME"] = name
    env["SKEIN_SKILL_STAGE"] = str(rec["stage"])
    env["SKEIN_REPO_ROOT"] = str(repo_root)
    t0 = time.monotonic()
    result = rt.execute(command_for(Path(rec["path"])) + list(args or []),
                        cwd=cwd or os.getcwd(),
                        timeout=timeout if timeout is not None else cfg["run_timeout"],
                        env=env)
    duration_ms = int((time.monotonic() - t0) * 1000)
    if record:
        try:
            _record(repo_root, actor, {
                "action": "run", "name": name, "hash": rec.get("disk_hash"),
                "exit_code": result.exit_code, "duration_ms": duration_ms,
                "timed_out": result.timed_out, "stage": rec["stage"],
            }, commit=False)
        except Exception:
            pass  # recording is observability; the run already happened
    return result


def test(repo_root: str | Path, name: str, actor: str = "skill",
         timeout: Optional[float] = None,
         cwd: Optional[str | Path] = None) -> Dict[str, Any]:
    """Run the skill's self-test and record the outcome as evidence.

    The outcome is bound to the current file hash. A skill whose header
    says `self-test: none` cannot produce evidence and so can never be
    promoted; the call explains that instead of faking a pass.
    """
    from . import runtime as rt
    rec = get(repo_root, name)
    if rec["stage"] in (None,):
        raise SkillError(f"skill '{name}' is on disk but not registered; "
                         f"run `skein skills add {name}` first")
    if rec["stage"] == "missing":
        raise SkillError(f"skill '{name}' file is missing")
    if rec["stage"] in ("retired", "rejected"):
        raise SkillError(f"skill '{name}' is {rec['stage']}")
    if rec["stage"] == "modified":
        raise SkillError(
            f"skill '{name}' changed on disk since it was registered; run "
            f"`skein skills add {name}` to register the new content, then test")
    argv = self_test_argv(rec["header"])
    if argv is None:
        raise SkillError(
            f"skill '{name}' declares `self-test: none`; without a self-test "
            f"it has no evidence and cannot be promoted")
    cfg = skill_config(repo_root)
    env = dict(os.environ)
    env["SKEIN_SKILL_NAME"] = name
    env["SKEIN_SKILL_SELF_TEST"] = "1"
    env["SKEIN_REPO_ROOT"] = str(repo_root)
    t0 = time.monotonic()
    result = rt.execute(command_for(Path(rec["path"])) + argv,
                        cwd=cwd or str(repo_root),
                        timeout=timeout if timeout is not None else cfg["test_timeout"],
                        env=env)
    duration_ms = int((time.monotonic() - t0) * 1000)
    passed = (not result.timed_out) and result.exit_code == 0
    out_file = _evidence_file(repo_root, name, "selftest")
    output = result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else "")
    verdict = ("TIMEOUT" if result.timed_out else f"exit={result.exit_code}")
    redact.write_redacted(
        out_file,
        f"$ {' '.join(command_for(Path(rec['path'])) + argv)}\n{verdict}\n"
        f"--- output (capped) ---\n{output}\n")
    note = (result.stdout.strip().splitlines() or [""])[-1][:200]
    _record(repo_root, actor, {
        "action": "tested", "name": name, "hash": rec["disk_hash"],
        "passed": passed, "exit_code": result.exit_code,
        "timed_out": result.timed_out, "duration_ms": duration_ms,
        "note": note, "output_ref": str(out_file),
    })
    rec = get(repo_root, name)
    rec["last_test"] = {"passed": passed, "exit_code": result.exit_code,
                        "timed_out": result.timed_out, "output_ref": str(out_file),
                        "duration_ms": duration_ms}
    return rec


def promote(repo_root: str | Path, name: str, actor: str = "operator") -> Dict[str, Any]:
    """Promote a tested skill. Requires test evidence on the current
    content and a pass rate at or above the configured threshold."""
    rec = get(repo_root, name)
    if rec["stage"] == "promoted":
        return rec
    if rec["stage"] == "modified":
        raise SkillError(f"skill '{name}' changed on disk since it was tested; "
                         f"`skein skills add {name}` then test again")
    if rec["stage"] != "tested":
        raise SkillError(f"cannot promote '{name}': stage is '{rec['stage']}', "
                         f"needs test evidence first (`skein skills test {name}`)")
    threshold = skill_config(repo_root)["promote_threshold"]
    fit = rec.get("fitness")
    if fit is None or fit < threshold:
        raise SkillError(
            f"cannot promote '{name}': pass rate {fit if fit is not None else 0:.2f} "
            f"is below the threshold {threshold:.2f} "
            f"({len(rec['tests'])} test(s) on this content)")
    _record(repo_root, actor, {"action": "promoted", "name": name,
                               "hash": rec["hash"], "fitness": fit,
                               "tests": len(rec["tests"])})
    return get(repo_root, name)


def retire(repo_root: str | Path, name: str, actor: str = "operator",
           reason: str = "", reject: bool = False) -> Dict[str, Any]:
    rec = get(repo_root, name)
    if rec["log_stage"] is None:
        raise SkillError(f"skill '{name}' was never registered")
    if rec["log_stage"] in ("retired", "rejected"):
        return rec
    _record(repo_root, actor, {"action": "rejected" if reject else "retired",
                               "name": name, "hash": rec["hash"],
                               "reason": reason[:200]})
    return get(repo_root, name)


# ---------------------------------------------------------------------------
# the token-cheap index
# ---------------------------------------------------------------------------

def _keywords(text: str) -> List[str]:
    from .infer import keywords
    return keywords(text)


def index_lines(repo_root: str | Path, node: Optional[Dict[str, Any]] = None,
                include_untrusted: bool = True,
                max_lines: Optional[int] = None) -> List[str]:
    """One line per skill: `name  stage  description  (usage)`.

    Promoted skills first; within a stage, skills whose triggers match
    the node's title/goal keywords come first (ADAM's registry answers
    "which promoted skills apply to this condition"). Modified, missing,
    retired and rejected skills are never listed: an agent should not be
    pointed at a script that does not match its trust record.
    """
    recs = registry(repo_root)
    keys = set(_keywords(f"{node.get('title', '')} {(node.get('intent') or {}).get('goal', '')}")) \
        if node else set()
    rows = []
    for rec in recs.values():
        stage = rec["stage"]
        if stage not in ("promoted", "tested", "proposed"):
            continue
        if not include_untrusted and stage != "promoted":
            continue
        hit = any(t in keys or any(t in k or k in t for k in keys)
                  for t in rec["triggers"]) if keys else False
        rank = (0 if stage == "promoted" else 1 if stage == "tested" else 2,
                0 if hit else 1, rec["name"])
        rows.append((rank, rec))
    rows.sort(key=lambda r: r[0])
    cap = max_lines if max_lines is not None else skill_config(repo_root)["index_max_lines"]
    lines = []
    for _, rec in rows[:max(0, cap)]:
        tag = rec["stage"] if rec["stage"] == "promoted" else f"{rec['stage']} (untrusted)"
        lines.append(f"{rec['name']}  [{tag}]  {rec['description'][:160]}  "
                     f"({rec['usage'][:80]})")
    if len(rows) > cap:
        lines.append(f"... {len(rows) - cap} more: skein skills list")
    return lines


def prompt_block(repo_root: str | Path, node: Optional[Dict[str, Any]] = None) -> str:
    """The block the supervisor puts in the backend prompt. Empty when
    there are no listable skills or skills_in_prompt is off."""
    cfg = skill_config(repo_root)
    if not cfg["in_prompt"]:
        return ""
    try:
        lines = index_lines(repo_root, node)
    except Exception:
        return ""
    if not lines:
        return ""
    return ("SKILLS (scripts you can run; run them with `skein skills run <name> "
            "[-- args]` instead of re-deriving the procedure. Bodies are not "
            "shown here: `skein skills show <name>` prints one if you must "
            "read it. Untrusted ones have no verified self-test yet. To save a "
            "reusable procedure for future agents: `skein skills new <name>`, "
            "edit the script, `skein skills add <name>`, `skein skills test "
            "<name>`):\n" + "\n".join(lines) + "\n")


def token_estimate(repo_root: str | Path) -> Dict[str, int]:
    """Rough token cost of the index vs. every skill body (chars / 4)."""
    recs = registry(repo_root)
    index_chars = sum(len(l) + 1 for l in index_lines(repo_root, max_lines=10 ** 6))
    body_chars = sum(int(r.get("body_chars") or 0) for r in recs.values()
                     if r["stage"] in ("promoted", "tested", "proposed"))
    return {"index_tokens": max(0, index_chars // _CHARS_PER_TOKEN),
            "body_tokens": max(0, body_chars // _CHARS_PER_TOKEN),
            "skills": sum(1 for r in recs.values()
                          if r["stage"] in ("promoted", "tested", "proposed"))}


# ---------------------------------------------------------------------------
# assistant registration (graphify-style lean skill file)
# ---------------------------------------------------------------------------

_PLATFORM_DIRS = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".codex") / "skills",
    "agents": Path(".agents") / "skills",
    "gemini": Path(".gemini") / "skills",
    "opencode": Path(".opencode") / "skills",
}

SKILL_MD = """---
name: skein
description: Use skein's shared skill scripts before re-deriving a procedure. Trigger on "/skein", "skein skills", or any task in a repository with a .skein/ directory: list skills with `skein skills index`, run one with `skein skills run <name>`, and save a reusable procedure with `skein skills new`.
---

# skein skills

Skills are executable scripts in `.skein/skills/`, shared through git,
with trust earned by self-test evidence (`proposed -> tested -> promoted`).
Do not read the scripts to decide what they do: the index line is the
contract.

1. `skein skills index` - one line per skill (name, stage, description,
   usage). Costs a few tokens per skill; read this instead of the bodies.
2. `skein skills run <name> [-- args]` - run one. Output is capped and
   the run is bounded. Prefer promoted skills; `(untrusted)` ones have
   no verified self-test yet.
3. `skein skills show <name>` - the full script, only if you must.
4. To save a procedure you expect to repeat:
   `skein skills new <name> --description "..."`, edit
   `.skein/skills/<name>.py` (keep the header lines; implement
   `--self-test` so it proves the skill works), then
   `skein skills add <name>` and `skein skills test <name>`.
   Promotion (`skein skills promote`) is the operator's call.
"""


def install(repo_root: str | Path, platform: str = "claude") -> Path:
    base = _PLATFORM_DIRS.get(platform.lower())
    if base is None:
        raise SkillError(f"unknown platform '{platform}' "
                         f"(choices: {', '.join(sorted(_PLATFORM_DIRS))})")
    dest = Path(repo_root) / base / "skein" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(SKILL_MD.encode("utf-8"))
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------------------
# rendering for the CLI
# ---------------------------------------------------------------------------

def render_table(repo_root: str | Path) -> str:
    recs = registry(repo_root)
    if not recs:
        return "(no skills; create one with `skein skills new <name>`)"
    header = ("NAME", "STAGE", "TESTS", "FIT", "RUNS", "FAILS", "DESCRIPTION")
    rows = []
    for rec in recs.values():
        stage = rec["stage"] if rec["stage"] is not None else "unregistered"
        fit = rec.get("fitness")
        rows.append((rec["name"], stage, str(len(rec["tests"])),
                     "-" if fit is None else f"{fit:.2f}", str(rec["runs"]),
                     str(rec["run_failures"]), rec["description"][:70]))
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt(r):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip()
    est = token_estimate(repo_root)
    lines = [fmt(header)] + [fmt(r) for r in rows]
    lines.append(f"index ~{est['index_tokens']} tokens for {est['skills']} skill(s); "
                 f"bodies ~{est['body_tokens']} tokens (never sent to agents)")
    return "\n".join(lines)
