"""Strict node-ID validation and filesystem-safe path resolution.

Node IDs flow into git branch names, worktree paths, evidence file
names, and API paths. They must never be trusted just because they
came from the CLI or the web UI.
"""

from __future__ import annotations

import re
from pathlib import Path

# Conservative schema: starts alphanumeric, then alnum/dot/underscore/
# dash, max 128 chars. Rejects "..", "/", "\\", absolute paths, control
# characters, and shell metacharacters by construction.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NodeIdError(ValueError):
    pass


def validate_node_id(node_id: str) -> str:
    """Return the node id if valid, else raise NodeIdError."""
    if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
        raise NodeIdError(
            f"invalid node id {node_id!r}: must match "
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
        )
    return node_id


def _ensure_within(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise NodeIdError(f"path escapes skein root: {path}")
    return resolved


def worktree_path_for(repo_root, node_id: str) -> Path:
    """Filesystem-safe worktree path, guaranteed under .skein/worktrees."""
    validate_node_id(node_id)
    root = Path(repo_root) / ".skein" / "worktrees"
    return _ensure_within(root, root / node_id)


def evidence_name(node_id: str, suffix: str) -> str:
    """Filesystem-safe evidence file name for a node."""
    validate_node_id(node_id)
    safe_suffix = re.sub(r"[^A-Za-z0-9._-]", "_", suffix)[:64]
    return f"{node_id}_{safe_suffix}.log"
