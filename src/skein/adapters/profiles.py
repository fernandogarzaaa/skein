"""Profile-driven backend engine (Stage 8).

One generic adapter engine, configured per backend by a data profile.
Adding a backend means adding a profile entry, not new orchestration
code. Each profile carries its own verified/unverified state, so the
support matrix always says exactly what has been proven - no
across-the-board claims.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROMPT_MODES = ("positional", "flag", "stdin")


@dataclass(frozen=True)
class AdapterResult:
    exit_code: int
    output: str


# A parser maps raw (stdout, stderr, exit_code) to an AdapterResult.
Parser = Callable[[str, str, int], AdapterResult]


def passthrough_parser(stdout: str, stderr: str, exit_code: int) -> AdapterResult:
    """Legacy byte-shape: stdout plus a stderr trailer. The default for
    backends whose output contract is plain text."""
    output = (stdout or "") + ("\n--- stderr ---\n" + stderr if stderr else "")
    return AdapterResult(exit_code, output)


def _harvest_text(obj: Any, keys: tuple, out: List[str]) -> None:
    """Best-effort text harvest from unknown stream-json shapes. Only used
    by parsers for backends whose exact event schema is unverified; the
    contract is pinned by mocked tests, not by a real binary."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v:
                out.append(v)
            else:
                _harvest_text(v, keys, out)
    elif isinstance(obj, list):
        for v in obj:
            _harvest_text(v, keys, out)


def make_stream_json_parser(*keys: str) -> Parser:
    """Build a parser for newline-delimited JSON event streams: extract
    text payloads line by line, fall back to raw output when a line is
    not JSON or carries no harvestable text (so evidence is never lost)."""
    keys = keys or ("text",)

    def parse(stdout: str, stderr: str, exit_code: int) -> AdapterResult:
        parts: List[str] = []
        raw_lines = (stdout or "").splitlines()
        if not raw_lines:
            return passthrough_parser(stdout, stderr, exit_code)
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                parts.append(line)
                continue
            found: List[str] = []
            _harvest_text(obj, keys, found)
            parts.append(" ".join(found) if found else line)
        return passthrough_parser("\n".join(parts), stderr, exit_code)

    parse.__name__ = "stream_json_parser(%s)" % (",".join(keys))
    return parse


# Assumed (unverified) stream-json text shapes. Each profile using these
# is marked verified=False until tested against the real binary.
gemini_stream_parser = make_stream_json_parser("text")
cursor_stream_parser = make_stream_json_parser("text", "content", "message", "result")


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


@dataclass(frozen=True)
class BackendProfile:
    """Data profile for one backend CLI. Field semantics:

    prompt_mode "flag": prompt immediately follows the headless flag
        group, e.g. `claude -p <prompt> ...`.
    prompt_mode "positional": prompt is the final argv element,
        e.g. `opencode run --model m <prompt>`.
    prompt_mode "stdin": prompt is piped via stdin, not argv.
    """

    name: str
    binary: str
    binary_env_var: Optional[str] = None
    prompt_mode: str = "positional"
    headless_flag: Any = field(default_factory=list)
    approval_bypass_flag: Any = None
    output_format_flag: Any = None
    config_flags: Dict[str, List[str]] = field(default_factory=dict)
    required_config: List[str] = field(default_factory=list)
    output_parser: Parser = passthrough_parser
    verified: bool = False
    source_note: str = ""

    def __post_init__(self) -> None:
        if self.prompt_mode not in PROMPT_MODES:
            raise ValueError(f"unknown prompt_mode: {self.prompt_mode}")
        object.__setattr__(self, "headless_flag", _as_list(self.headless_flag))
        object.__setattr__(self, "approval_bypass_flag", _as_list(self.approval_bypass_flag))
        object.__setattr__(self, "output_format_flag", _as_list(self.output_format_flag))
        object.__setattr__(self, "required_config", list(self.required_config or []))


CLAUDE_CODE_PROFILE = BackendProfile(
    name="claude_code",
    binary="claude",
    binary_env_var="SKEIN_CLAUDE_BIN",
    prompt_mode="flag",
    headless_flag=["-p"],
    approval_bypass_flag=["--dangerously-skip-permissions"],
    output_format_flag=["--output-format", "text"],
    output_parser=passthrough_parser,
    verified=True,
    source_note="Migrated from bespoke adapter; invocation validated against real claude CLI 2.1.248.",
)

REGISTRY: Dict[str, BackendProfile] = {}


def register(profile: BackendProfile) -> BackendProfile:
    REGISTRY[profile.name] = profile
    return profile


def get_profile(name: str, repo_root: Any = None) -> BackendProfile:
    try:
        return _merged_registry(repo_root)[name]
    except KeyError:
        raise ValueError(
            f"unknown backend '{name}' (known: {', '.join(sorted(_merged_registry(repo_root)))})")


def list_profiles(repo_root: Any = None) -> List[BackendProfile]:
    merged = _merged_registry(repo_root)
    return [merged[k] for k in sorted(merged)]


register(CLAUDE_CODE_PROFILE)


CODEX_PROFILE = BackendProfile(
    name="codex",
    binary="codex",
    binary_env_var="SKEIN_CODEX_BIN",
    prompt_mode="flag",
    headless_flag=["exec", "--full-auto"],
    output_parser=passthrough_parser,
    verified=False,
    source_note=("Invocation shape from OpenAI Codex docs (codex exec "
                  "non-interactive mode); not yet run against the real binary "
                  "in this repo. Upstream now prefers --sandbox workspace-write "
                  "over --full-auto (deprecated compat flag); --json event output "
                  "available via --adapter-args."),
)

register(CODEX_PROFILE)


GEMINI_CLI_PROFILE = BackendProfile(
    name="gemini_cli",
    binary="gemini",
    binary_env_var="SKEIN_GEMINI_BIN",
    prompt_mode="flag",
    headless_flag=["-p"],
    approval_bypass_flag=["--yolo"],
    output_format_flag=["--output-format", "stream-json"],
    output_parser=gemini_stream_parser,
    verified=False,
    source_note=("Invocation shape from the Stage 8 spec (gemini -p --yolo "
                  "--output-format stream-json); stream-json event schema "
                  "assumed, not yet run against the real binary in this repo."),
)

register(GEMINI_CLI_PROFILE)


OPENCODE_PROFILE = BackendProfile(
    name="opencode",
    binary="opencode",
    binary_env_var="SKEIN_OPENCODE_BIN",
    prompt_mode="positional",
    headless_flag=["run"],
    config_flags={"model": ["--model"]},
    required_config=["model"],
    output_parser=passthrough_parser,
    verified=False,
    source_note=("opencode run --model <provider/model> <prompt> per opencode "
                  "1.x CLI help (verified present locally); model has no "
                  "default so backend_config['model'] is required. Binary "
                  "present but zero provider credentials here - not yet run "
                  "end-to-end in this repo."),
)

register(OPENCODE_PROFILE)


CURSOR_AGENT_PROFILE = BackendProfile(
    name="cursor_agent",
    binary="cursor-agent",
    binary_env_var="SKEIN_CURSOR_BIN",
    prompt_mode="positional",
    headless_flag=["-p"],
    output_format_flag=["--output-format", "stream-json"],
    output_parser=cursor_stream_parser,
    verified=False,
    source_note=("Invocation shape from the Stage 8 spec (cursor-agent -p "
                  "--output-format stream-json); stream-json event schema "
                  "assumed, binary not installed here - not yet run end-to-end "
                  "in this repo."),
)

register(CURSOR_AGENT_PROFILE)


AIDER_PROFILE = BackendProfile(
    name="aider",
    binary="aider",
    binary_env_var="SKEIN_AIDER_BIN",
    prompt_mode="flag",
    headless_flag=["--message"],
    approval_bypass_flag=["--yes"],
    output_parser=passthrough_parser,
    verified=False,
    source_note=("Stage 8e proof: added with zero orchestration changes - "
                  "headless shape (aider --message + --yes) from aider's "
                  "documented non-interactive usage; not run against the "
                  "real binary in this repo."),
)

register(AIDER_PROFILE)


def sample_invocation(profile: BackendProfile) -> str:
    """Render the profile's argv shape with a <prompt> placeholder (and
    <key> placeholders for required config). Used by `skein backends
    list` and the README support matrix."""
    from .engine import ProfileAdapter
    node = {"id": "<node>", "title": "", "intent": {},
            "backend_config": {k: "<%s>" % k for k in profile.required_config}}
    argv = ProfileAdapter(profile).sample_argv("<prompt>", node.get("backend_config", {}))
    return " ".join(argv)


CUSTOM_PARSERS: Dict[str, Parser] = {
    "passthrough": passthrough_parser,
    "stream_json": gemini_stream_parser,
    "stream_json_relaxed": cursor_stream_parser,
}


def profile_to_dict(profile: BackendProfile) -> Dict[str, Any]:
    return {
        "name": profile.name,
        "binary": profile.binary,
        "binary_env_var": profile.binary_env_var,
        "prompt_mode": profile.prompt_mode,
        "headless_flag": list(profile.headless_flag),
        "approval_bypass_flag": list(profile.approval_bypass_flag),
        "output_format_flag": list(profile.output_format_flag),
        "config_flags": {k: list(v) for k, v in profile.config_flags.items()},
        "required_config": list(profile.required_config),
        "parser": next((k for k, fn in CUSTOM_PARSERS.items()
                        if fn is profile.output_parser), "passthrough"),
        "verified": profile.verified,
        "source_note": profile.source_note,
    }


def profile_from_dict(data: Dict[str, Any], origin: str = "custom") -> BackendProfile:
    """Build a profile from a JSON mapping (see `skein backends add`).
    Raises ValueError on any schema violation. Custom profiles are
    always labeled with their origin; `verified` is accepted as given
    (your repo, your claim) but defaults to False."""
    if not isinstance(data, dict):
        raise ValueError(f"{origin}: profile must be a JSON object")
    name = data.get("name", "")
    binary = data.get("binary", "")
    if not name or not binary:
        raise ValueError(f"{origin}: profile needs 'name' and 'binary'")
    parser_name = data.get("parser", "passthrough")
    if parser_name not in CUSTOM_PARSERS:
        raise ValueError(f"{origin}: unknown parser '{parser_name}' "
                         f"(choices: {', '.join(sorted(CUSTOM_PARSERS))})")
    config_flags = data.get("config_flags", {})
    if not isinstance(config_flags, dict):
        raise ValueError(f"{origin}: 'config_flags' must be an object")
    return BackendProfile(
        name=name,
        binary=binary,
        binary_env_var=data.get("binary_env_var"),
        prompt_mode=data.get("prompt_mode", "positional"),
        headless_flag=_as_list(data.get("headless_flag", [])),
        approval_bypass_flag=_as_list(data.get("approval_bypass_flag")),
        output_format_flag=_as_list(data.get("output_format_flag")),
        config_flags={k: _as_list(v) for k, v in config_flags.items()},
        required_config=list(data.get("required_config", [])),
        output_parser=CUSTOM_PARSERS[parser_name],
        verified=bool(data.get("verified", False)),
        source_note=f"custom profile ({origin}); " + str(data.get("source_note", "")),
    )


def user_backends_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "skein" / "backends"


def repo_backends_dir(repo_root: Any) -> Path:
    return Path(repo_root) / ".skein" / "backends"


def load_custom_profiles(repo_root: Any = None) -> Dict[str, BackendProfile]:
    """Read user- and repo-level custom profiles. Bad files are skipped
    with a stderr warning, never a crash - a broken JSON file must not
    take down `backends list`."""
    found: Dict[str, BackendProfile] = {}
    candidates = [("user", user_backends_dir())]
    if repo_root is not None:
        candidates.append(("repo", repo_backends_dir(repo_root)))
    for scope, directory in candidates:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = profile_from_dict(data, origin=f"{scope}:{path.name}")
            except (ValueError, json.JSONDecodeError, OSError) as e:
                import sys
                print(f"warning: skipping {path}: {e}", file=sys.stderr)
                continue
            found[profile.name] = profile
    return found


def _merged_registry(repo_root: Any = None) -> Dict[str, BackendProfile]:
    merged = dict(REGISTRY)
    merged.update(load_custom_profiles(repo_root))
    return merged


def render_backends_table(repo_root: Any = None) -> str:
    """The support matrix, generated from the registry - the single
    source of truth also embedded in the README."""
    profiles = list_profiles(repo_root)
    rows = [(p.name, "yes" if p.verified else "no",
             sample_invocation(p), p.source_note) for p in profiles]
    header = ("NAME", "VERIFIED", "INVOCATION", "SOURCE")
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    def fmt(r):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip()
    return "\n".join([fmt(header)] + [fmt(r) for r in rows])
