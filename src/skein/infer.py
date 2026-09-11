"""Blast-radius inference: heuristic glob suggestions, never auto-applied.

Suggests file globs a node is likely to touch by matching keywords from
its title/goal/context against tracked repo paths, compressed to
directory globs. This is a heuristic starting point for a human (or an
agent proposing via node edit) - it does not prove anything about what
a node will touch, and suggestions are only ever printed, never
written to the graph by this module.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

STOPWORDS = frozenset("""
a an the and or of to in on for with without from by as at is are was were be
been it its this that these those they them he she we you your our their his her
make add new use using used create implement build fix update change remove refactor
file files code test tests app project system feature support should will can
""".split())


def keywords(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if len(w) >= 3 and w not in STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def tracked_files(repo_root: Any) -> List[str]:
    try:
        r = subprocess.run(["git", "ls-files"], cwd=str(repo_root),
                           capture_output=True, text=True)
        if r.returncode == 0:
            files = [l for l in r.stdout.splitlines() if l.strip()]
            if files:
                return files
    except FileNotFoundError:
        pass
    out = []
    root = Path(repo_root)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel.startswith(".git/") or rel.startswith(".skein/"):
                continue
            out.append(rel)
    return out


def _compress(matches: Dict[str, int]) -> List[Tuple[str, int, List[str]]]:
    """Compress matched files to ranked (glob, hits, files) suggestions,
    clustered by parent directory: 3+ matches compress to a dir glob,
    smaller clusters stay exact paths (more precise)."""
    by_parent: Dict[str, List[str]] = {}
    for path in matches:
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        by_parent.setdefault(parent, []).append(path)
    suggestions = []
    for parent, paths in by_parent.items():
        hits = sum(matches[p] for p in paths)
        if parent != "." and len(paths) >= 3:
            exts = {Path(p).suffix for p in paths}
            if len(exts) == 1 and next(iter(exts)):
                suggestions.append((f"{parent}/**/*{next(iter(exts))}", hits,
                                    sorted(paths)))
            else:
                suggestions.append((f"{parent}/**", hits, sorted(paths)))
        else:
            for path in sorted(paths, key=lambda p: (-matches[p], p)):
                suggestions.append((path, matches[path], [path]))
    suggestions.sort(key=lambda s: (-s[1], s[0]))
    return suggestions


def suggest_blast_radius(repo_root: Any, node_id: str,
                         top_n: int = 5) -> Dict[str, Any]:
    from . import graph as g
    nodes = g.load_graph(repo_root)
    node = nodes.get(node_id)
    if node is None:
        raise ValueError(f"unknown node '{node_id}'")
    intent = node.get("intent", {})
    keys = keywords(f"{node.get('title', '')} {intent.get('goal', '')} "
                    f"{intent.get('context', '')}")
    matches: Dict[str, int] = {}
    for path in tracked_files(repo_root):
        lowered = path.lower()
        hits = sum(1 for k in keys if k in lowered)
        if hits:
            matches[path] = hits
    suggestions = [{"glob": glob, "hits": hits, "files": files}
                   for glob, hits, files in _compress(matches)[:top_n]]
    inherited = sorted({b for d in node.get("depends_on", [])
                        for b in (nodes.get(d, {}).get("blast_radius", []))})
    return {"node": node_id, "keywords": keys, "suggestions": suggestions,
            "inherited": inherited,
            "declared": list(node.get("blast_radius", []))}
