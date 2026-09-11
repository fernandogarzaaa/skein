"""Skein CLI (v0.1)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import graph as g
from . import claim as c
from . import worktree as wt


def find_repo_root(start: str | Path | None = None) -> str:
    cur = Path(start or os.getcwd()).resolve()
    for p in [cur] + list(cur.parents):
        if (p / ".skein").exists():
            return str(p)
    # fall back: containing git repo
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, cwd=str(cur))
        if r.returncode == 0:
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    return str(cur)


def default_actor() -> str:
    return os.environ.get("SKEIN_ACTOR") or getpass.getuser()


def parse_backend_config(items) -> dict:
    """Parse repeatable --backend-config key=value flags into a map."""
    cfg: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"bad --backend-config {item!r}: expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"bad --backend-config {item!r}: expected key=value")
        cfg[key] = value
    return cfg


def resolve_backend(name: str, repo_root=None) -> str:
    from .adapters.profiles import get_profile
    try:
        return get_profile(name.strip(), repo_root).name
    except ValueError:
        from .adapters.profiles import list_profiles
        known = ", ".join(p.name for p in list_profiles(repo_root))
        print(f"unknown backend '{name}' (known: {known})",
              file=sys.stderr)
        raise SystemExit(1)


# ---------- command implementations ----------

def cmd_init(args) -> int:
    root = os.getcwd()
    # git repo (needed for worktrees + log commits)
    if not Path(root, ".git").exists():
        r = subprocess.run(["git", "init"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"git init failed: {r.stderr}", file=sys.stderr)
            return 1
    # git identity (needed for automated log commits)
    subprocess.run(["git", "config", "user.email", "skein@localhost"],
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "skein"], capture_output=True)
    sk = Path(root) / ".skein"
    sk.mkdir(exist_ok=True)
    (sk / "worktrees").mkdir(exist_ok=True)
    (sk / "evidence").mkdir(exist_ok=True)
    if not g.config_path(root).exists():
        ttl = int(getattr(args, "ttl", 1800))
        g.config_path(root).write_text(
            json.dumps({"default_ttl_seconds": ttl,
                        "heartbeat_interval_seconds": 60}, indent=2),
            encoding="utf-8")
    if not g.log_path(root).exists():
        g.log_path(root).write_text("", encoding="utf-8")
    g.rebuild_graph(root)
    # install a reminder hook (commits of .skein stay explicit via CLI)
    hooks = Path(root) / ".git" / "hooks"
    if hooks.exists():
        hook = hooks / "post-commit"
        if not hook.exists():
            hook.write_text("#!/bin/sh\n# skein: event log lives under .skein/ and is committed by the CLI on every append.\n",
                            encoding="utf-8")
    g.git_commit_log(root, "skein: init")
    print(f"initialized skein in {root}/.skein")
    return 0


def cmd_node_add(args) -> int:
    from .edits import add_node
    root = find_repo_root()
    depends = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
    blast = [b.strip() for b in (args.blast_radius or "").split(",") if b.strip()]
    try:
        backend_config = parse_backend_config(args.backend_config)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        add_node(root, default_actor(), args.id,
                 title=args.title or "", goal=args.goal or "",
                 context=args.context or "", constraints=args.constraints or "",
                 completion=args.completion or "", depends_on=depends,
                 blast_radius=blast, backend=args.backend or "claude_code",
                 backend_config=backend_config)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"added node {args.id}")
    return 0


def cmd_node_edit(args) -> int:
    from .edits import edit_node
    root = find_repo_root()
    nodes = g.load_graph(root)
    node = nodes.get(args.id)
    if node is None:
        print(f"unknown node '{args.id}'", file=sys.stderr)
        return 1
    fields: dict = {}
    intent: dict = {}
    if args.title is not None:
        fields["title"] = args.title
    if args.goal is not None:
        intent["goal"] = args.goal
    if args.context is not None:
        intent["context"] = args.context
    if args.constraints is not None:
        intent["constraints"] = args.constraints
    if args.completion is not None:
        intent["completion"] = args.completion
    if args.depends_on is not None:
        fields["depends_on"] = [d.strip() for d in args.depends_on.split(",") if d.strip()]
    if args.blast_radius is not None:
        fields["blast_radius"] = [b.strip() for b in args.blast_radius.split(",") if b.strip()]
    if args.status is not None:
        fields["status"] = args.status
    if args.backend is not None:
        fields["backend"] = args.backend
    if args.backend_config:
        try:
            extra = parse_backend_config(args.backend_config)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        merged = dict(node.get("backend_config") or {})
        merged.update(extra)
        fields["backend_config"] = merged
    if intent:
        fields["intent"] = intent
    try:
        outcome = edit_node(root, default_actor(), args.id, fields,
                            delete=args.delete)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if outcome == "interrupt_delete":
        print(f"node {args.id}: human_interrupt (delete) recorded; claim parked")
    elif outcome == "removed":
        print(f"removed node {args.id}")
    elif outcome == "interrupt":
        print(f"node {args.id}: human_interrupt recorded; agent claim parked as needs_human")
    else:
        print(f"edited node {args.id}")
    return 0


STATUS_ICON = {"unclaimed": "o", "claimed": "C", "in_progress": ">",
               "blocked": "#", "needs_human": "?", "done": "*", "failed": "X"}


def cmd_graph(args) -> int:
    root = find_repo_root()
    nodes = g.load_graph(root)
    if not nodes:
        print("(empty graph)")
        return 0
    # topological-ish render: roots first, then dependents (tree/table view)
    done_ids = set()
    remaining = dict(nodes)
    order = []
    while remaining:
        progress = False
        for nid, n in list(remaining.items()):
            if all(d not in remaining for d in n.get("depends_on", [])):
                order.append(nid)
                del remaining[nid]
                progress = True
        if not progress:  # cycle: drain alphabetically
            for nid in sorted(remaining):
                order.append(nid)
            break
    depth = {}
    for nid in order:
        n = nodes[nid]
        deps = n.get("depends_on", [])
        depth[nid] = (max([depth.get(d, 0) for d in deps]) + 1) if deps else 0
    print(f"{'ID':<22}{'ST':<4}{'STATUS':<12}TITLE")
    for nid in order:
        n = nodes[nid]
        icon = STATUS_ICON.get(n["status"], " ")
        indent = "  " * min(depth[nid], 5)
        deps = ",".join(n.get("depends_on", []))
        extra = f"  [depends: {deps}]" if deps else ""
        holder = (n.get("claim") or {}).get("holder")
        if holder and n["status"] in ("claimed", "in_progress"):
            extra += f"  [held by {holder}]"
        print(f"{indent}{nid:<22}{icon:<4}{n['status']:<12}{n.get('title', '')}{extra}")
    return 0


def cmd_claim(args) -> int:
    root = find_repo_root()
    holder = args.agent_id or default_actor()
    try:
        c.reap_expired(root)
        node = c.claim_node(root, args.id, holder,
                            ttl_seconds=args.ttl, actor=default_actor())
    except c.ClaimError as e:
        print(f"cannot claim '{args.id}': {e}", file=sys.stderr)
        return 1
    print(f"claimed {args.id} for {holder} (ttl={node['claim']['ttl_seconds']}s)")
    return 0


def cmd_run(args) -> int:
    from .supervisor import run_node
    root = find_repo_root()
    holder = args.agent_id or default_actor()
    c.reap_expired(root)
    from .adapters.engine import ProfileAdapter
    from .adapters.profiles import get_profile
    adapter_args = args.adapter_args or []
    adapter = None
    if args.backend:
        # Explicit override of the node's recorded backend for this run.
        backend_name = resolve_backend(args.backend, root)
        node = g.load_graph(root).get(args.id)
        if node is None:
            print(f"unknown node '{args.id}'", file=sys.stderr)
            return 1
        adapter = ProfileAdapter(get_profile(backend_name, root),
                                 backend_config=node.get("backend_config"),
                                 extra_args=adapter_args)
    try:
        result = run_node(root, args.id, holder, adapter=adapter,
                          verify_timeout=args.verify_timeout,
                          heartbeat_interval=args.heartbeat_interval,
                          adapter_timeout=args.adapter_timeout,
                          extra_args=adapter_args,
                          actor=default_actor())
    except (c.ClaimError, ValueError, RuntimeError) as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1
    outcome = result.get("outcome")
    print(f"run {args.id}: {outcome}")
    if outcome == "failed":
        return 2
    if outcome == "interrupted":
        return 3
    return 0


def cmd_release(args) -> int:
    root = find_repo_root()
    try:
        c.release_node(root, args.id, actor=default_actor(),
                       force=args.force, note="force-released by human" if args.force else "")
    except c.ClaimError as e:
        print(f"cannot release '{args.id}': {e}", file=sys.stderr)
        return 1
    print(f"released {args.id}" + (" (forced)" if args.force else ""))
    return 0


def cmd_status(args) -> int:
    root = find_repo_root()
    reaped = c.reap_expired(root)
    if reaped:
        print(f"reaper released expired leases: {', '.join(reaped)}")
    nodes = g.load_graph(root)
    if not nodes:
        print("(empty graph)")
        return 0
    now = datetime.now(timezone.utc)
    print(f"{'ID':<22}{'STATUS':<12}{'HOLDER':<16}LEASE")
    for nid in sorted(nodes):
        n = nodes[nid]
        claim = n.get("claim") or {}
        holder = claim.get("holder") or "-"
        lease = "-"
        if n["status"] in ("claimed", "in_progress") and claim.get("holder"):
            last = c.parse_ts(claim.get("last_heartbeat") or claim.get("claimed_at"))
            ttl = claim.get("ttl_seconds") or 0
            if last:
                remain = ttl - (now - last).total_seconds()
                lease = "EXPIRED" if remain <= 0 else f"{int(remain)}s left"
        print(f"{nid:<22}{n['status']:<12}{holder:<16}{lease}")
    return 0


def cmd_log(args) -> int:
    root = find_repo_root()
    events = g.load_events(root)
    if args.node:
        events = [e for e in events if e.get("node_id") == args.node]
    for e in events[-args.limit:]:
        print(f"{e.get('timestamp')}  {e.get('actor'):>12}  {e.get('type'):<15}  "
              f"{e.get('node_id')}  {json.dumps(e.get('payload', {}))[:200]}")
    return 0


def cmd_backends_list(args) -> int:
    from .adapters.profiles import render_backends_table
    root = find_repo_root()
    print(render_backends_table(root))
    return 0


def _custom_scope_dir(args, root: str) -> Path:
    from .adapters.profiles import repo_backends_dir, user_backends_dir
    scope = (args.scope or "").strip().lower()
    if scope not in ("repo", "user"):
        # Default: repo scope when inside a skein repo (shared with the
        # team via git), else user scope.
        scope = "repo" if Path(root, ".skein").is_dir() else "user"
    return repo_backends_dir(root) if scope == "repo" else user_backends_dir()


def cmd_backends_add(args) -> int:
    from .adapters.profiles import profile_from_dict
    root = find_repo_root()
    if not args.name.strip() or not args.binary.strip():
        print("--name and --binary are required", file=sys.stderr)
        return 1
    config_flags: dict = {}
    for item in args.config_flag or []:
        if "=" not in item:
            print(f"bad --config-flag {item!r}: expected key=--flag", file=sys.stderr)
            return 1
        key, flag = item.split("=", 1)
        if not key.strip() or not flag.strip():
            print(f"bad --config-flag {item!r}: expected key=--flag", file=sys.stderr)
            return 1
        config_flags.setdefault(key.strip(), []).append(flag.strip())
    data = {
        "name": args.name.strip(),
        "binary": args.binary.strip(),
        "prompt_mode": args.prompt_mode,
        "headless_flag": list(args.headless or []),
        "approval_bypass_flag": list(args.approval_bypass or []),
        "output_format_flag": list(args.output_format or []),
        "config_flags": config_flags,
        "required_config": list(args.required_config or []),
        "parser": args.parser,
        "verified": False,
        "source_note": args.source_note or "added via `skein backends add`; unverified",
    }
    try:
        profile_from_dict(data, origin="cli")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    directory = _custom_scope_dir(args, root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{data['name']}.json"
    if path.exists() and not args.overwrite:
        print(f"{path} exists (use --overwrite)", file=sys.stderr)
        return 1
    import json as _json
    path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    print(f"added backend '{data['name']}' -> {path}")
    return 0


def cmd_backends_remove(args) -> int:
    from .adapters.profiles import REGISTRY, repo_backends_dir, user_backends_dir
    root = find_repo_root()
    name = args.name.strip()
    if name in REGISTRY and args.scope in (None, "", "builtin"):
        print(f"'{name}' is a built-in profile and cannot be removed", file=sys.stderr)
        return 1
    scopes = [args.scope] if args.scope in ("repo", "user") else ["repo", "user"]
    for scope in scopes:
        directory = repo_backends_dir(root) if scope == "repo" else user_backends_dir()
        path = directory / f"{name}.json"
        if path.exists():
            path.unlink()
            print(f"removed backend '{name}' ({path})")
            return 0
    print(f"no custom backend '{name}' found", file=sys.stderr)
    return 1


def cmd_serve(args) -> int:
    from .serve import serve_forever
    serve_forever(find_repo_root(), host=args.host, port=args.port)
    return 0


def cmd_infer_blast(args) -> int:
    from .infer import suggest_blast_radius
    root = find_repo_root()
    try:
        result = suggest_blast_radius(root, args.id, top_n=args.top_n)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"node {args.id} keywords: {', '.join(result['keywords']) or '(none)'}")
    if result["suggestions"]:
        print("suggested globs (heuristic only - apply explicitly with node edit):")
        for s in result["suggestions"]:
            print(f"  {s['glob']}  ({s['hits']} hits: {', '.join(s['files'][:5])})")
    else:
        print("no suggestions: no keyword matched a tracked path")
    if result["inherited"]:
        print(f"inherited from dependencies: {', '.join(result['inherited'])}")
    if result["declared"]:
        print(f"currently declared: {', '.join(result['declared'])}")
    return 0


def cmd_sync(args) -> int:
    from .sync import sync_repo
    root = find_repo_root()
    try:
        result = sync_repo(root, push=not args.no_push, remote=args.remote)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"sync: {result['status']} (pulled={result['pulled']}, "
          f"pushed={result['pushed']}): {result['detail']}")
    return 0 if result["status"] == "ok" else 2


def cmd_reap(args) -> int:
    root = find_repo_root()
    released = c.reap_expired(root, actor=default_actor())
    print(f"released: {', '.join(released) if released else '(none)'}")
    return 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skein", description="Parallel task-graph orchestration for coding agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create .skein/, initial graph, git hooks")
    pi.add_argument("--ttl", type=int, default=1800)
    pi.set_defaults(func=cmd_init)

    pn = sub.add_parser("node", help="node operations")
    nsub = pn.add_subparsers(dest="node_cmd", required=True)
    pa = nsub.add_parser("add", help="add a node")
    pa.add_argument("id")
    pa.add_argument("--title", default="")
    pa.add_argument("--goal", default="")
    pa.add_argument("--context", default="")
    pa.add_argument("--constraints", default="")
    pa.add_argument("--completion", default="")
    pa.add_argument("--depends-on", default="")
    pa.add_argument("--blast-radius", default="")
    pa.add_argument("--backend", default="claude_code",
                    help="backend profile for this node (see: skein backends list)")
    pa.add_argument("--backend-config", action="append", default=[],
                    metavar="key=value",
                    help="backend-specific parameters (repeatable); e.g. model=openai/gpt-5 for opencode")
    pa.set_defaults(func=cmd_node_add)
    pe = nsub.add_parser("edit", help="edit a node (claimed nodes -> human_interrupt)")
    pe.add_argument("id")
    pe.add_argument("--title", default=None)
    pe.add_argument("--goal", default=None)
    pe.add_argument("--context", default=None)
    pe.add_argument("--constraints", default=None)
    pe.add_argument("--completion", default=None)
    pe.add_argument("--depends-on", default=None)
    pe.add_argument("--blast-radius", default=None)
    pe.add_argument("--status", default=None)
    pe.add_argument("--backend", default=None)
    pe.add_argument("--backend-config", action="append", default=[],
                    metavar="key=value")
    pe.add_argument("--delete", action="store_true")
    pe.set_defaults(func=cmd_node_edit)

    pg = sub.add_parser("graph", help="render current graph state to terminal")
    pg.set_defaults(func=cmd_graph)

    pc = sub.add_parser("claim", help="attempt to claim a node")
    pc.add_argument("id")
    pc.add_argument("--agent-id", default=None)
    pc.add_argument("--ttl", type=int, default=None)
    pc.set_defaults(func=cmd_claim)

    pr = sub.add_parser("run", help="claim + worktree + backend + verify + report")
    pr.add_argument("id")
    pr.add_argument("--agent-id", default=None)
    pr.add_argument("--backend", default=None,
                    help="override the node's recorded backend for this run")
    pr.add_argument("--verify-timeout", type=int, default=600)
    pr.add_argument("--heartbeat-interval", type=float, default=30.0)
    pr.add_argument("--adapter-timeout", type=float, default=1800.0,
                    help="ceiling for the backend process; on expiry it is killed and the node fails")
    pr.add_argument("--adapter-args", nargs=argparse.REMAINDER, default=[])
    pr.set_defaults(func=cmd_run)

    prl = sub.add_parser("release", help="release a node's claim")
    prl.add_argument("id")
    prl.add_argument("--force", action="store_true",
                     help="explicit force-release of another holder's claim (logged)")
    prl.set_defaults(func=cmd_release)

    ps = sub.add_parser("status", help="all nodes, claims, health of active leases")
    ps.set_defaults(func=cmd_status)

    pl = sub.add_parser("log", help="human-readable event history")
    pl.add_argument("--node", default=None)
    pl.add_argument("--limit", type=int, default=50)
    pl.set_defaults(func=cmd_log)

    prp = sub.add_parser("reap", help="release expired leases now")
    prp.set_defaults(func=cmd_reap)

    psy = sub.add_parser("sync", help="share the event log via git (fetch/merge/push)")
    psy.add_argument("--no-push", action="store_true")
    psy.add_argument("--remote", default="origin")
    psy.set_defaults(func=cmd_sync)

    psv = sub.add_parser("serve", help="live web canvas (graph view + human editing)")
    psv.add_argument("--host", default="127.0.0.1")
    psv.add_argument("--port", type=int, default=8765)
    psv.set_defaults(func=cmd_serve)

    pib = sub.add_parser("infer-blast",
                         help="suggest blast-radius globs for a node (heuristic, never applied)")
    pib.add_argument("id")
    pib.add_argument("--top-n", type=int, default=5)
    pib.set_defaults(func=cmd_infer_blast)

    pb = sub.add_parser("backends", help="backend profile registry")
    bsub = pb.add_subparsers(dest="backends_cmd", required=True)
    bl = bsub.add_parser("list", help="print the backend support matrix")
    bl.set_defaults(func=cmd_backends_list)

    ba = bsub.add_parser("add", help="register a custom backend (any CLI) without code changes")
    ba.add_argument("--name", required=True, help="profile name, e.g. mycli")
    ba.add_argument("--binary", required=True, help="executable resolved on PATH")
    ba.add_argument("--prompt-mode", default="positional",
                    choices=["positional", "flag", "stdin"],
                    help="positional: prompt is the final argv element; "
                         "flag: prompt follows the headless flags; "
                         "stdin: prompt is piped via stdin")
    ba.add_argument("--headless", action="append", default=[],
                    help="headless flag token (repeatable; use --headless=-p "
                         "form for dash-leading tokens)")
    ba.add_argument("--approval-bypass", action="append", default=[],
                    help="approval-bypass flag token (repeatable; --opt=value form)")
    ba.add_argument("--output-format", action="append", default=[],
                    help="output-format flag tokens (repeatable; --opt=value form)")
    ba.add_argument("--config-flag", action="append", default=[], metavar="key=--flag",
                    help="map a backend_config key to flag tokens (repeatable)")
    ba.add_argument("--required-config", action="append", default=[],
                    help="backend_config key with no default, e.g. model (repeatable)")
    ba.add_argument("--parser", default="passthrough",
                    choices=["passthrough", "stream_json", "stream_json_relaxed"])
    ba.add_argument("--source-note", default="")
    ba.add_argument("--scope", default="", choices=["", "repo", "user"],
                    help="default: repo scope inside a skein repo, else user scope")
    ba.add_argument("--overwrite", action="store_true")
    ba.set_defaults(func=cmd_backends_add)

    br = bsub.add_parser("remove", help="remove a custom backend profile")
    br.add_argument("name")
    br.add_argument("--scope", default="", choices=["", "repo", "user"])
    br.set_defaults(func=cmd_backends_remove)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
