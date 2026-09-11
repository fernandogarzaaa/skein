"""Generic profile-driven adapter engine.

ProfileAdapter consumes a BackendProfile: argv assembly, binary
resolution, stdin handling, and output parsing are all data-driven.
No per-backend orchestration code lives here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import BackendAdapter
from .profiles import BackendProfile


class ProfileAdapter(BackendAdapter):
    def __init__(self, profile: BackendProfile,
                 binary: str | None = None,
                 backend_config: Optional[Dict[str, str]] = None,
                 extra_args: list | None = None):
        self.profile = profile
        self._binary = binary
        self.backend_config = dict(backend_config or {})
        self.extra_args = list(extra_args or [])

    @property
    def name(self) -> str:
        return self.profile.name

    def resolve_binary(self) -> str:
        if self._binary:
            return self._binary
        env_var = self.profile.binary_env_var
        if env_var and os.environ.get(env_var):
            return os.environ[env_var]
        return self.profile.binary

    def build_prompt(self, node: Dict) -> str:
        intent = node.get("intent", {})
        handoffs = intent.get("parent_handoffs", "")
        prompt = (
            f"You are working on task '{node.get('id')}: {node.get('title', '')}'.\n\n"
            f"GOAL (what done looks like):\n{intent.get('goal', '')}\n\n"
            f"CONTEXT (environment, frameworks, patterns to follow):\n{intent.get('context', '')}\n\n"
            f"CONSTRAINTS (what to avoid):\n{intent.get('constraints', '')}\n\n"
            f"COMPLETION CHECK (the supervisor will run this literally):\n{intent.get('completion', '')}\n"
        )
        if handoffs:
            prompt += f"\nHANDOFF NOTES FROM DEPENDENCIES:\n{handoffs}\n"
        prompt += ("\nMake the change in the current working directory. "
                   "Do not commit unless asked.")
        return prompt

    def config_argv(self, backend_config: Optional[Dict[str, str]] = None) -> List[str]:
        cfg = backend_config if backend_config is not None else self.backend_config
        for key in self.profile.required_config:
            if not (cfg or {}).get(key):
                raise ValueError(
                    f"backend '{self.profile.name}' requires backend_config['{key}'] "
                    f"(no default exists); pass --backend-config {key}=... on node add")
        argv: List[str] = []
        for key in sorted(self.profile.config_flags):
            if key in (cfg or {}):
                argv.extend(self.profile.config_flags[key])
                argv.append(str(cfg[key]))
        return argv

    def sample_argv(self, prompt: str,
                    backend_config: Optional[Dict[str, str]] = None) -> List[str]:
        """Assemble argv with an explicit prompt string (used for the
        <prompt> placeholder rendering and by build_command)."""
        p = self.profile
        argv = [self.resolve_binary()] + list(p.headless_flag)
        if p.prompt_mode == "flag":
            argv.append(prompt)
        argv = argv + list(p.approval_bypass_flag) + list(p.output_format_flag)
        argv = argv + self.config_argv(backend_config) + list(self.extra_args)
        if p.prompt_mode == "positional":
            argv.append(prompt)
        return argv

    def build_command(self, node: Dict) -> list:
        cfg = dict(self.backend_config)
        if isinstance(node.get("backend_config"), dict):
            merged = dict(node["backend_config"])
            merged.update(cfg)
            cfg = merged
        return self.sample_argv(self.build_prompt(node), cfg)

    def run(self, node: Dict, worktree_path: str | Path, timeout: int = 600
            ) -> Tuple[int, str]:
        cmd = self.build_command(node)
        stdin_text = self.build_prompt(node) if self.profile.prompt_mode == "stdin" else None
        try:
            r = subprocess.run(cmd, cwd=str(worktree_path), capture_output=True,
                               text=True, timeout=timeout,
                               input=stdin_text)
            result = self.profile.output_parser(r.stdout or "", r.stderr or "",
                                                r.returncode)
            return result.exit_code, result.output
        except FileNotFoundError as e:
            resolved = self.resolve_binary()
            return 127, f"{resolved} binary not found: {resolved}: {e}"
        except subprocess.TimeoutExpired:
            resolved = self.resolve_binary()
            return 124, f"{resolved} invocation timed out after {timeout}s"
