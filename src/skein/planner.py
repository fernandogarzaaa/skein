"""Planner: turn a goal description into a reviewable DAG draft.

Two brains, one draft schema:

- heuristic (default): deterministic parsing of markdown task lists.
  Numbered lists become sequential chains, bulleted lists under a
  heading become parallel nodes, headings order stages sequentially,
  and explicit "after X" / "depends on X" / "once X is done" hints
  become edges. No network, no LLM.
- llm (--llm): delegates decomposition to an external command named
  by SKEIN_PLANNER_CMD, which receives a JSON spec on stdin and must
  return the draft schema on stdout.

Planning is read-only: nothing touches the graph until the operator
runs `skein plan --apply`. Drafts are versioned JSON; node creation
goes through the validated edits.add_node path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import graph as g
from . import ids
from . import redact

DRAFT_VERSION = 1

# Reserved anchor for plan_applied events, mirroring the "skein-release"
# anchor used by shipping. Informational only: no node state derived.
PLAN_ANCHOR = "skein-plan"


class PlanError(ValueError):
    """The goal could not be turned into a valid DAG draft."""


# ---------------------------------------------------------------------------
# node id slugification
# ---------------------------------------------------------------------------

def slugify(title: str, taken: set, fallback: str) -> str:
    """Deterministic filesystem-safe node id derived from a title.

    Lowercases, maps non-alphanumerics to '-', collapses runs, strips
    edges, truncates to 60 chars, and disambiguates with -2, -3, ...
    against `taken`. Always returns an id that passes ids.validate.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-")
    if not slug:
        slug = fallback
    # ids require a leading alphanumeric; fallback is already safe.
    if not re.match(r"^[A-Za-z0-9]", slug):
        slug = fallback
    base, n = slug, 2
    while slug in taken:
        suffix = f"-{n}"
        slug = (base[: 60 - len(suffix)] + suffix).strip("-")
        n += 1
    try:
        ids.validate_node_id(slug)
    except ids.NodeIdError:
        slug = fallback
        n = 2
        while slug in taken:
            slug = f"{fallback}-{n}"
            n += 1
    taken.add(slug)
    return slug


# ---------------------------------------------------------------------------
# heuristic text parsing
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUMBERED_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_INLINE_NUM_RE = re.compile(r"(\d+)[.)]\s+")

_DEP_HINT_RES = [
    re.compile(r"\bafter\s+([^,.;:()]+?)(?:\s+first)?\s*$", re.IGNORECASE),
    re.compile(r"\bdepends?\s+on\s+([^,.;:()]+?)\s*$", re.IGNORECASE),
    re.compile(r"\bonce\s+([^,.;:()]+?)\s+(?:is|are)\s+done\b", re.IGNORECASE),
    re.compile(r"\bonce\s+([^,.;:()]+?)\s+(?:is|are)\s+complete[sd]?\b",
               re.IGNORECASE),
    re.compile(r"\bfollowing\s+([^,.;:()]+?)\s*$", re.IGNORECASE),
]


def _split_inline_numbered(line: str) -> List[str]:
    """Split '1. a 2. b 3. c' written on one line into ['1. a', ...]."""
    markers = list(_INLINE_NUM_RE.finditer(line))
    if len(markers) < 2:
        return [line]
    parts = []
    for i, m in enumerate(markers):
        start = m.start()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(line)
        parts.append(line[start:end].strip())
    return parts


def _normalize_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in (text or "").splitlines():
        for part in _split_inline_numbered(raw):
            lines.append(part)
    return lines


def _dep_hints(item_text: str) -> List[str]:
    hints: List[str] = []
    # also match hints wrapped in parentheses: "backfill (depends on X)"
    variants = [item_text, re.sub(r"[()]", " ", item_text)]
    for text in variants:
        for rx in _DEP_HINT_RES:
            for m in rx.finditer(text):
                frag = m.group(1).strip().strip("\"'")
                if frag and frag not in hints:
                    hints.append(frag)
    return hints


def _strip_hint_clauses(item_text: str) -> str:
    """Remove trailing dependency-hint clauses so titles stay clean."""
    text = item_text
    for rx in _DEP_HINT_RES:
        text = rx.sub("", text).strip()
    # parenthesized hint clauses: "backfill (depends on migrate db)"
    text = re.sub(
        r"\(\s*(?:after|depends?\s+on|once\s+.+?\s+(?:is|are)\s+"
        r"(?:done|complete[sd]?)|following)\b[^)]*\)",
        "", text, flags=re.IGNORECASE).strip()
    return re.sub(r"\s{2,}", " ", text).rstrip(",;").strip()


class _Block:
    def __init__(self, heading: str = "") -> None:
        self.heading = heading
        # (text, numbered)
        self.items: List[Tuple[str, bool]] = []


def _parse_blocks(text: str) -> Tuple[List[_Block], str]:
    """Split text into heading/item blocks; return (blocks, preamble)."""
    blocks: List[_Block] = []
    cur = _Block()
    preamble: List[str] = []
    seen_item = False
    for line in _normalize_lines(text):
        if not line.strip():
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            if cur.items or cur.heading:
                blocks.append(cur)
            cur = _Block(heading=hm.group(2).strip())
            continue
        nm = _NUMBERED_RE.match(line)
        if nm:
            cur.items.append((nm.group(2).strip(), True))
            seen_item = True
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            cur.items.append((bm.group(1).strip(), False))
            seen_item = True
            continue
        if not seen_item and not cur.heading and not cur.items:
            preamble.append(line.strip())
        # stray prose between items is ignored: it is not a task.
    if cur.items or cur.heading:
        blocks.append(cur)
    return blocks, " ".join(preamble).strip()


def _find_cycle(node_ids: List[str],
                deps: Dict[str, List[str]]) -> Optional[List[str]]:
    """Return a cycle path (ids) or None. DFS over the draft edge set."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in node_ids}

    def visit(nid: str, stack: List[str]) -> Optional[List[str]]:
        color[nid] = GRAY
        for dep in deps.get(nid, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                return stack + [dep]
            if color[dep] == WHITE:
                hit = visit(dep, stack + [dep])
                if hit:
                    return hit
        color[nid] = BLACK
        return None

    for nid in node_ids:
        if color[nid] == WHITE:
            hit = visit(nid, [nid])
            if hit:
                return hit
    return None


def _resolve_hint(fragment: str,
                  candidates: List[Tuple[str, str]]) -> Optional[str]:
    """Map a hint fragment to a node id by title substring match.

    `candidates` is [(node_id, title)]. Longest title match wins, so
    "implement login" beats "login" when both appear.
    """
    frag = fragment.lower()
    best: Optional[str] = None
    best_len = -1
    for nid, title in candidates:
        t = title.lower()
        if frag in t or t in frag:
            if len(t) > best_len:
                best, best_len = nid, len(t)
    return best


def parse_heuristic(text: str, goal: str = "") -> Dict[str, Any]:
    """Deterministically parse a goal/task list into a draft DAG dict.

    Raises PlanError on dependency cycles.
    """
    goal = (goal or "").strip()
    blocks, preamble = _parse_blocks(text)
    if not goal:
        goal = preamble[:200] or "planned tasks"
    else:
        # the goal arg often carries the task list inline
        # ("Build auth: 1. design schema 2. ..."): keep only the part
        # before the first list marker as the goal proper. Require two
        # markers so "Phase 1. Design" style goals are left alone.
        markers = list(_INLINE_NUM_RE.finditer(goal))
        if len(markers) >= 2 and markers[0].start() > 0:
            short = goal[:markers[0].start()].rstrip(" :,-").strip()
            if short:
                goal = short
        if len(goal) > 200:
            goal = goal[:200].rstrip() + "..."

    # Fallback: prose-only goal with no list items -> single node.
    if not blocks:
        single = (goal or "task").strip()
        taken: set = set()
        node = {
            "id": slugify(single[:120], taken, "task-1"),
            "title": single[:120],
            "goal": goal,
            "completion": (
                f"Complete this task: {single}\n\n"
                f"It is part of the larger goal: {goal}\n\n"
                "When finished, write a handoff note summarizing what "
                "changed and how to verify it."),
            "depends_on": [],
        }
        return _finalize_draft(
            goal=goal, source="heuristic", brain="heuristic",
            raw_nodes=[node],
            warnings=[],
        )

    raw_nodes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    # per-block node index lists, in order
    block_node_idxs: List[List[int]] = []
    for block in blocks:
        idxs: List[int] = []
        items = block.items
        # a block chains when any item is numbered: all-numbered lists
        # chain, mixed lists chain in listed order (documented), and
        # all-bullet lists stay parallel.
        chain = any(n for _, n in items)
        prev_in_block: Optional[int] = None
        for pos, (text_item, is_numbered) in enumerate(items):
            title = _strip_hint_clauses(text_item)
            node = {
                "title": title[:120] or f"task {len(raw_nodes) + 1}",
                "task": text_item,
                "depends_on": [],
                "hints": _dep_hints(text_item),
            }
            if chain and prev_in_block is not None:
                node["depends_on"].append(prev_in_block)  # placeholder
            idxs.append(len(raw_nodes))
            raw_nodes.append(node)
            prev_in_block = len(raw_nodes) - 1
        block_node_idxs.append(idxs)

    # assign stable ids first so hints and edges can reference them
    taken: set = set()
    for i, node in enumerate(raw_nodes):
        node["id"] = slugify(node["title"], taken, f"task-{i + 1}")

    # resolve intra-block placeholder edges to ids
    id_by_idx = {i: n["id"] for i, n in enumerate(raw_nodes)}
    for node in raw_nodes:
        node["depends_on"] = [id_by_idx[i] for i in node["depends_on"]]

    # stage ordering: every node in block k depends on all of block k-1
    for prev_idxs, idxs in zip(block_node_idxs, block_node_idxs[1:]):
        prev_ids = [id_by_idx[i] for i in prev_idxs]
        for i in idxs:
            nid = id_by_idx[i]
            node = raw_nodes[i]
            for pid in prev_ids:
                if pid != nid and pid not in node["depends_on"]:
                    node["depends_on"].append(pid)

    # explicit hint edges
    candidates = [(n["id"], n["title"]) for n in raw_nodes]
    for node in raw_nodes:
        for frag in node.pop("hints"):
            target = _resolve_hint(
                frag, [(nid, t) for nid, t in candidates if nid != node["id"]])
            if target and target not in node["depends_on"]:
                node["depends_on"].append(target)
            elif not target:
                warnings.append(
                    f"node '{node['id']}': hint '{frag}' matched no task; "
                    "edge skipped")

    cycle = _find_cycle([n["id"] for n in raw_nodes],
                        {n["id"]: n["depends_on"] for n in raw_nodes})
    if cycle:
        raise PlanError(
            "dependency cycle detected in plan: " + " -> ".join(cycle))

    for node in raw_nodes:
        node["goal"] = f"Plan: {goal}\n\nTask: {node['task']}"
        node["completion"] = (
            f"Complete this task: {node['task']}\n\n"
            f"It is part of the larger goal: {goal}\n\n"
            "When finished, write a handoff note summarizing what changed "
            "and how to verify it.")
        del node["task"]
    return _finalize_draft(goal=goal, source="heuristic", brain="heuristic",
                           raw_nodes=raw_nodes, warnings=warnings)


def _finalize_draft(goal: str, source: str, brain: str,
                    raw_nodes: List[Dict[str, Any]],
                    warnings: List[str]) -> Dict[str, Any]:
    nodes = []
    for n in raw_nodes:
        nodes.append({
            "id": n["id"],
            "title": n["title"],
            "goal": n.get("goal", ""),
            "completion": n.get("completion", ""),
            "depends_on": list(n.get("depends_on", [])),
            "backend": n.get("backend", "claude_code"),
        })
    return {
        "version": DRAFT_VERSION,
        "goal": goal,
        "source": source,
        "brain": brain,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warnings": list(warnings),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# LLM brain
# ---------------------------------------------------------------------------

def _tracked_files(repo_root: Any, limit: int = 200) -> List[str]:
    try:
        r = subprocess.run(["git", "ls-files"], cwd=str(repo_root),
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            files = [l for l in r.stdout.splitlines() if l.strip()]
            return files[:limit]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def parse_llm(goal: str, text: str, repo_root: Any) -> Dict[str, Any]:
    """Ask the external SKEIN_PLANNER_CMD for a draft DAG.

    Never falls back to the heuristic: a missing/failing/misbehaving
    command is a hard error so the operator always knows which brain
    produced the plan.
    """
    cmd = os.environ.get("SKEIN_PLANNER_CMD", "").strip()
    if not cmd:
        raise PlanError(
            "SKEIN_PLANNER_CMD is not set: --llm needs an external planner "
            "command to pipe the goal spec to")
    nodes = g.load_graph(repo_root)
    spec = {
        "goal": goal,
        "source_text": text,
        "existing_nodes": [
            {"id": nid, "title": n.get("title", ""),
             "status": n.get("status", "")}
            for nid, n in sorted(nodes.items())
        ],
        "files": _tracked_files(repo_root),
        "draft_schema_version": DRAFT_VERSION,
    }
    try:
        r = subprocess.run([cmd], input=json.dumps(spec),
                           capture_output=True, text=True, timeout=120,
                           cwd=str(repo_root))
    except FileNotFoundError:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} not found or not executable")
    except subprocess.TimeoutExpired:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} timed out after 120s")
    if r.returncode != 0:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} exited {r.returncode}: "
            f"{r.stderr.strip()[:500]}")
    try:
        draft = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} returned invalid JSON: {e}")
    _validate_llm_draft(draft, cmd, nodes)
    draft["brain"] = cmd
    if not draft.get("source"):
        draft["source"] = "llm"
    draft.setdefault("warnings", [])
    draft.setdefault("created_at",
                     datetime.now(timezone.utc).isoformat())
    return draft


def _validate_llm_draft(draft: Any, cmd: str,
                        existing: Dict[str, Dict[str, Any]]) -> None:
    if not isinstance(draft, dict):
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} must return a JSON object")
    if draft.get("version") != DRAFT_VERSION:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} returned draft version "
            f"{draft.get('version')!r}; this skein reads version "
            f"{DRAFT_VERSION}")
    # the external command is untrusted input: every field the draft
    # path touches must have the expected type, otherwise a stray
    # string/int turns into per-character warnings or an uncaught
    # TypeError instead of a clean PlanError.
    for field in ("goal", "source", "brain"):
        if field in draft and not isinstance(draft[field], str):
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r}: draft field {field!r} must "
                f"be a string")
    warnings = draft.get("warnings", [])
    if (not isinstance(warnings, list)
            or any(not isinstance(w, str) for w in warnings)):
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r}: draft 'warnings' must be a "
            "list of strings")
    nodes = draft.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} returned no nodes")
    draft_ids = set()
    for n in nodes:
        if not isinstance(n, dict):
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r} returned a malformed node")
        if not isinstance(n.get("id"), str):
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r} returned a node with a "
                "non-string id")
        for field in ("title", "goal", "completion", "backend"):
            if field in n and not isinstance(n[field], str):
                raise PlanError(
                    f"SKEIN_PLANNER_CMD {cmd!r}: node '{n.get('id')}' "
                    f"field {field!r} must be a string")
        deps = n.get("depends_on", []) or []
        if not isinstance(deps, list) or any(
                not isinstance(d, str) for d in deps):
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r}: node '{n.get('id')}' "
                "'depends_on' must be a list of strings")
        try:
            ids.validate_node_id(n.get("id", ""))
        except ids.NodeIdError as e:
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r} returned invalid node id: {e}")
        if n["id"] in draft_ids:
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r} returned duplicate node id "
                f"'{n['id']}'")
        if n["id"] in existing:
            raise PlanError(
                f"SKEIN_PLANNER_CMD {cmd!r} reused existing node id "
                f"'{n['id']}'")
        draft_ids.add(n["id"])
        for dep in n.get("depends_on", []) or []:
            if dep not in draft_ids and dep not in existing:
                # dep may be a later draft node; checked fully below
                pass
    all_ids = draft_ids | set(existing)
    for n in nodes:
        for dep in n.get("depends_on", []) or []:
            if dep == n["id"]:
                raise PlanError(
                    f"SKEIN_PLANNER_CMD {cmd!r}: node '{n['id']}' depends "
                    "on itself")
            if dep not in all_ids:
                raise PlanError(
                    f"SKEIN_PLANNER_CMD {cmd!r}: node '{n['id']}' depends "
                    f"on unknown '{dep}'")
    cycle = _find_cycle([n["id"] for n in nodes],
                        {n["id"]: list(n.get("depends_on", []) or [])
                         for n in nodes})
    if cycle:
        raise PlanError(
            f"SKEIN_PLANNER_CMD {cmd!r} returned a dependency cycle: "
            + " -> ".join(cycle))


# ---------------------------------------------------------------------------
# git-log source
# ---------------------------------------------------------------------------

def plan_from_git_log(repo_root: Any, max_commits: int = 20) -> Dict[str, Any]:
    """Build a sequential draft from recent commit subjects."""
    try:
        r = subprocess.run(
            ["git", "log", f"-n{max_commits}", "--format=%s"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise PlanError("git not available for --from-git-log")
    if r.returncode != 0:
        raise PlanError(f"git log failed: {r.stderr.strip()[:200]}")
    subjects = [s.strip() for s in r.stdout.splitlines() if s.strip()]
    if not subjects:
        raise PlanError("git log is empty: nothing to plan from")
    subjects.reverse()  # oldest first -> sequential chain
    taken: set = set()
    raw_nodes: List[Dict[str, Any]] = []
    prev_id: Optional[str] = None
    for i, subject in enumerate(subjects):
        nid = slugify(subject, taken, f"commit-{i + 1}")
        node: Dict[str, Any] = {
            "id": nid,
            "title": subject[:120],
            "goal": f"Recreate the change from commit: {subject}",
            "completion": (
                f"Implement the change described by the commit subject: "
                f"{subject}\n\nWhen finished, write a handoff note "
                "summarizing what changed and how to verify it."),
            "depends_on": [prev_id] if prev_id else [],
        }
        raw_nodes.append(node)
        prev_id = nid
    draft = _finalize_draft(goal=f"replay last {len(subjects)} commits",
                            source="git-log", brain="git-log",
                            raw_nodes=raw_nodes, warnings=[])
    return draft


# ---------------------------------------------------------------------------
# draft persistence (redacted at the write boundary)
# ---------------------------------------------------------------------------

def _drafts_dir(repo_root: Any) -> Path:
    d = Path(repo_root) / ".skein"
    d.mkdir(parents=True, exist_ok=True)
    return d


def draft_filename() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"plan-{ts}.json"


def save_draft(repo_root: Any, draft: Dict[str, Any]) -> Path:
    """Write a draft file. Secret-shaped content is redacted before it is
    stored, so goal text can never smuggle a key into .skein/."""
    path = _drafts_dir(repo_root) / draft_filename()
    raw = json.dumps(draft, indent=2, sort_keys=True)
    redacted = redact.redact_secrets(raw)
    path.write_text(redacted + "\n", encoding="utf-8")
    return path


def list_drafts(repo_root: Any) -> List[Path]:
    return sorted(_drafts_dir(repo_root).glob("plan-*.json"))


def latest_draft(repo_root: Any) -> Optional[Path]:
    drafts = list_drafts(repo_root)
    return drafts[-1] if drafts else None


def load_draft(path: Any) -> Dict[str, Any]:
    """Load and validate a draft file. Unknown schema versions are refused."""
    p = Path(path)
    try:
        draft = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"cannot read draft '{p}': {e}")
    if not isinstance(draft, dict) or draft.get("version") != DRAFT_VERSION:
        raise PlanError(
            f"draft '{p}' has unsupported version "
            f"{draft.get('version') if isinstance(draft, dict) else '?'}; "
            f"this skein reads version {DRAFT_VERSION}")
    if not isinstance(draft.get("nodes"), list):
        raise PlanError(f"draft '{p}' has no node list")
    return draft


def draft_hash(draft: Dict[str, Any]) -> str:
    """Semantic identity of a draft: created_at is excluded so the same
    logical draft re-saved later hashes identically."""
    content = {k: v for k, v in draft.items() if k != "created_at"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_applied_hashes(repo_root: Any) -> List[str]:
    """Hashes of drafts already applied, scanned from the event log."""
    out: List[str] = []
    for ev in g.load_events(repo_root):
        if ev.get("type") == "plan_applied":
            h = (ev.get("payload") or {}).get("draft_hash")
            if h:
                out.append(h)
    return out


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _topo_order(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order draft nodes so dependencies come first (stable, Kahn's)."""
    by_id = {n["id"]: n for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    dependents: Dict[str, List[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        for dep in n.get("depends_on", []) or []:
            if dep in by_id and dep != n["id"]:
                indeg[n["id"]] += 1
                dependents[dep].append(n["id"])
    ready = sorted([nid for nid, d in indeg.items() if d == 0])
    order: List[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for nxt in sorted(dependents[nid]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(order) != len(nodes):
        rest = sorted(set(by_id) - set(order))
        raise PlanError(
            "cannot order plan: dependency cycle involving "
            + ", ".join(rest))
    return [by_id[nid] for nid in order]


def apply_draft(repo_root: Any, actor: str, draft: Dict[str, Any],
                draft_name: str, force: bool = False) -> Tuple[List[str],
                                                              List[str]]:
    """Create draft nodes via the validated add_node path.

    Returns (created, skipped). Refuses when this exact draft was
    already applied (unless force). With force, node ids that already
    exist are skipped instead of failing, so a partially applied or
    partially deleted plan can be resumed; without force an id
    collision fails fast via add_node's own validation.
    Records one plan_applied event anchored at the reserved "skein-plan"
    id with the draft hash, so a second apply is caught even if nodes
    were deleted afterwards.
    """
    from .edits import add_node

    h = draft_hash(draft)
    if not force and h in plan_applied_hashes(repo_root):
        raise PlanError(
            f"draft '{draft_name}' was already applied "
            f"(hash {h[:12]}); use --force to apply it again")
    created: List[str] = []
    skipped: List[str] = []
    for node in _topo_order(draft["nodes"]):
        if force and node["id"] in g.load_graph(repo_root):
            skipped.append(node["id"])
            continue
        add_node(
            repo_root, actor, node["id"],
            title=node.get("title", "") or node["id"],
            goal=node.get("goal", ""),
            completion=node.get("completion", ""),
            depends_on=list(node.get("depends_on", []) or []),
            backend=node.get("backend") or "claude_code",
        )
        created.append(node["id"])
    # add_node already validated every edge (existence, cycles); the
    # anchor event is informational, like shipping's "release".
    g.append_event(repo_root, actor, "plan_applied", PLAN_ANCHOR, {
        "draft_hash": h,
        "draft_file": draft_name,
        "node_ids": created,
        "skipped_ids": skipped,
        "source": draft.get("source", ""),
        "brain": draft.get("brain", ""),
    })
    return created, skipped


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_draft(draft: Dict[str, Any], path: Optional[str] = None) -> str:
    lines = []
    header = (f"draft: {path} " if path else "draft: (not saved) ")
    header += (f"({draft.get('source', '?')}, brain={draft.get('brain', '?')}, "
               f"{len(draft.get('nodes', []))} nodes)")
    lines.append(header)
    if draft.get("goal"):
        lines.append(f"goal: {draft['goal']}")
    for w in draft.get("warnings", []) or []:
        lines.append(f"warning: {w}")
    for n in draft.get("nodes", []):
        deps = ", ".join(n.get("depends_on", []) or []) or "(none)"
        lines.append(f"  [{n['id']}] {n.get('title', '')}")
        lines.append(f"    depends_on: {deps}")
        completion = (n.get("completion", "") or "").splitlines()
        if completion:
            first = completion[0][:100]
            more = "..." if len(completion) > 1 or len(completion[0]) > 100 else ""
            lines.append(f"    completion: {first}{more}")
    lines.append("")
    lines.append("review the draft, then: skein plan --apply"
                 + (f" {path}" if path else ""))
    return "\n".join(lines)
