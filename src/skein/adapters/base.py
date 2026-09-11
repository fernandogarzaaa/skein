"""Backend adapter interface (orchestration lives above this layer)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple


class BackendAdapter:
    name = "base"

    def build_prompt(self, node: Dict) -> str:
        intent = node.get("intent", {})
        return (
            f"Goal: {intent.get('goal', '')}\n"
            f"Context: {intent.get('context', '')}\n"
            f"Constraints: {intent.get('constraints', '')}\n"
            f"Completion check: {intent.get('completion', '')}\n"
        )

    def run(self, node: Dict, worktree_path: str | Path, timeout: int = 600
            ) -> Tuple[int, str]:
        raise NotImplementedError
