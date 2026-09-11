"""Claude Code backend, expressed as a profile.

The invocation behavior lives in the CLAUDE_CODE_PROFILE data plus the
generic ProfileAdapter engine; this module preserves the historical
class name and constructor (including SKEIN_CLAUDE_BIN handling).
"""

from __future__ import annotations

from typing import Dict, Optional

from .engine import ProfileAdapter
from .profiles import CLAUDE_CODE_PROFILE, BackendProfile


class ClaudeCodeAdapter(ProfileAdapter):
    name = "claude_code"

    def __init__(self, binary: str | None = None, extra_args: list | None = None):
        super().__init__(CLAUDE_CODE_PROFILE, binary=binary, extra_args=extra_args)

    def build_prompt(self, node: Dict) -> str:
        return super().build_prompt(node)
