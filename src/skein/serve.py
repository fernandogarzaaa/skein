"""Web canvas: live graph visualization + human editing over HTTP.

Stdlib only (no new dependencies). The API reuses the same edit rules
as the CLI (edits module), so a human editing a claimed node from the
browser appends human_interrupt exactly like `skein node edit` does.

Hardening (Phase 6): optional bearer-token auth for mutating requests
(--auth-token / SKEIN_AUTH_TOKEN; GETs stay open for dashboard viewing),
a 1 MB request body cap, and a best-effort per-IP rate limit. The server
binds 127.0.0.1 by default; binding 0.0.0.0 without a token is refused.
Failed auth attempts are recorded as `security` events (without ever
logging the presented credential - a mistyped real token must not land
in the log).

Observability (Phase 7): /timeline (filterable event timeline),
/node/<id> (full node detail), /api/metrics (JSON) + /metrics
(Prometheus text), /api/health (operator health check). Lifecycle
rejections are recorded as `rejected` events in claim.py and surface
in the timeline, `skein log --rejected`, and the status REJ column.
"""

from __future__ import annotations

import collections
import hmac
import html
import json
import os
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

from . import SKEIN_VERSION

# Request hardening limits.
_MAX_BODY_BYTES = 1_000_000  # 413 on excess
_RATE_LIMIT = 60  # requests ...
_RATE_WINDOW = 60.0  # ... per 60 seconds, per client IP (429 on excess)
_MAX_TRACKED_IPS = 10000  # bound on the in-memory rate-limit table

# Cap on rendered timeline rows: the page is operator tooling, not an
# archive browser; the full log stays available via /api/events.
_TIMELINE_ROW_LIMIT = 500


def _index_html() -> str:
    path = Path(__file__).resolve().parent / "static" / "app.html"
    return path.read_text(encoding="utf-8")


def _page(title: str, body: str) -> bytes:
    """Server-rendered page shell reusing the canvas header style."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{html.escape(title)} - Skein</title>
<style>
body {{ font-family: sans-serif; margin: 0; background: #f7f7f7; color: #111; }}
header {{ padding: 8px 12px; background: #222; color: #fff; display: flex; gap: 16px; align-items: center; }}
header a {{ color: #9cf; text-decoration: none; font-size: 14px; }}
table {{ border-collapse: collapse; margin: 12px; background: #fff; }}
th, td {{ border: 1px solid #ccc; padding: 4px 10px; text-align: left; font-size: 13px; vertical-align: top; }}
th {{ background: #eee; }}
.mono {{ font-family: monospace; font-size: 12px; }}
main {{ padding: 0 4px 24px; }}
h2 {{ margin: 16px 12px 4px; font-size: 16px; }}
form {{ margin: 12px; font-size: 13px; }}
.note {{ margin: 12px; font-size: 13px; color: #555; }}
</style></head>
<body>
<header><strong>Skein</strong>
<a href="/">canvas</a><a href="/timeline">timeline</a><a href="/api/graph">api</a>
<a href="/api/metrics">metrics</a><a href="/metrics">prometheus</a><a href="/api/health">health</a>
</header><main>{body}</main></body></html>""".encode("utf-8")


def _event_summary(ev: Dict[str, Any]) -> str:
    """One-line human summary of an event for the timeline UI."""
    etype = ev.get("type")
    p = ev.get("payload") or {}
    if etype == "node_added":
        return f"added: {p.get('title') or ''}"[:160]
    if etype == "claimed":
        return f"claimed by {p.get('holder')}"
    if etype == "heartbeat":
        return f"heartbeat from {p.get('holder')}"
    if etype == "released":
        why = "lease expired" if p.get("expired") else (
            "force-released" if p.get("forced") else "released")
        return f"{why}: {(p.get('note') or '')[:140]}"
    if etype == "completed":
        return (p.get("handoff_note") or "completed")[:160]
    if etype == "failed":
        return f"{p.get('outcome') or 'failed'}: {(p.get('error') or '')[:140]}"
    if etype == "human_interrupt":
        return f"{p.get('action') or 'interrupt'}: {(p.get('reason') or '')[:140]}"
    if etype == "shipped":
        mc = str(p.get("merge_commit") or "")[:8]
        return f"shipped -> {p.get('target_branch')} as {mc}"
    if etype == "release":
        return f"release tag {p.get('tag')}"
    if etype == "security":
        return f"security: {p.get('kind')}"
    if etype == "rejected":
        return f"rejected {p.get('op')}: {(p.get('reason') or '')[:160]}"
    if etype == "plan_applied":
        ids = p.get("node_ids") or []
        return (f"plan applied ({p.get('source') or '?'}): "
                f"{len(ids)} node(s): {', '.join(ids[:6])}")[:160]
    return (json.dumps(p)[:160])


def _timeline_html(root: str, node_filter: str = "",
                   type_filter: str = "") -> bytes:
    from . import graph as g
    events = g.load_events(root)
    if node_filter:
        events = [e for e in events if e.get("node_id") == node_filter]
    if type_filter:
        events = [e for e in events if e.get("type") == type_filter]
    rows = []
    for e in reversed(events[-_TIMELINE_ROW_LIMIT:]):
        rows.append(
            "<tr><td class=\"mono\">" + html.escape(str(e.get("timestamp") or "")) +
            "</td><td class=\"mono\">" + html.escape(str(e.get("type") or "")) +
            "</td><td class=\"mono\">" + html.escape(str(e.get("node_id") or "")) +
            "</td><td>" + html.escape(str(e.get("actor") or "")) +
            "</td><td>" + html.escape(_event_summary(e)) + "</td></tr>")
    nf = html.escape(node_filter, quote=True)
    tf = html.escape(type_filter, quote=True)
    body = (f"<h2>Event timeline ({len(events)} events)</h2>"
            f"<form method=\"get\" action=\"/timeline\">"
            f"node <input name=\"node\" value=\"{nf}\" size=\"20\"> "
            f"type <input name=\"type\" value=\"{tf}\" size=\"16\"> "
            f"<button type=\"submit\">filter</button></form>"
            "<table><tr><th>timestamp</th><th>type</th><th>node</th>"
            "<th>actor</th><th>summary</th></tr>" +
            "".join(rows) + "</table>")
    return _page("Timeline", body)


def _node_html(root: str, node_id: str) -> Optional[bytes]:
    from . import graph as g
    node = g.load_graph(root).get(node_id)
    if node is None:
        return None
    claim = node.get("claim") or {}
    worktree = node.get("worktree") or {}
    result = node.get("result") or {}
    shipped = node.get("shipped") or {}
    attempts = node.get("attempts") or []
    evidence = node.get("evidence") or []
    diff_stats = result.get("diff_stats") or {}
    rows = [
        ("status", node.get("status")),
        ("title", node.get("title")),
        ("backend", node.get("backend")),
        ("depends_on", ", ".join(node.get("depends_on") or [])),
        ("claim holder", claim.get("holder") or "-"),
        ("attempt_id", (claim.get("attempt_id") or "")[:12] or "-"),
        ("max_retries", node.get("max_retries")),
        ("retry_backoff_seconds", node.get("retry_backoff_seconds")),
        ("attempts_used", node.get("attempts_used")),
        ("retry_at", node.get("retry_at") or "-"),
        ("rejected_count", node.get("rejected_count") or 0),
        ("worktree branch", worktree.get("branch") or "-"),
        ("worktree base_branch", worktree.get("base_branch") or "-"),
        ("worktree base_commit",
         str(worktree.get("base_commit") or "")[:12] or "-"),
        ("worktree path", worktree.get("path") or "-"),
        ("result commit", str(result.get("commit") or "")[:12] or "-"),
        ("result base", str(result.get("base_commit") or "")[:12] or "-"),
        ("changed files", ", ".join(result.get("changed_files") or []) or "-"),
        ("shipped", ", ".join(
            f"{b} as {str(v.get('merge_commit') or '')[:8]}"
            for b, v in shipped.items()) or "-"),
        ("handoff_note", (node.get("handoff_note") or "-")[:400]),
    ]
    info = "".join(
        f"<tr><th>{html.escape(k)}</th><td class=\"mono\">"
        f"{html.escape(str(v))}</td></tr>" for k, v in rows)
    att_rows = "".join(
        "<tr><td class=\"mono\">" + html.escape(str(a.get("attempt_id") or "")[:12]) +
        "</td><td>" + html.escape(str(a.get("holder") or "")) +
        "</td><td class=\"mono\">" + html.escape(str(a.get("started_at") or "")) +
        "</td><td class=\"mono\">" + html.escape(str(a.get("ended_at") or "")) +
        "</td><td>" + html.escape(str(a.get("outcome") or "")) +
        "</td><td>" + html.escape(str(a.get("error") or "")[:200]) + "</td></tr>"
        for a in attempts)
    ev_rows = "".join(
        "<tr><td class=\"mono\">" + html.escape(str(e.get("path") or e.get("kind") or "")) +
        "</td><td>" + html.escape(str(e.get("summary") or "")[:200]) + "</td></tr>"
        for e in evidence)
    diff_rows = "".join(
        f"<tr><td class=\"mono\">{html.escape(str(path))}</td>"
        f"<td class=\"mono\">+{d.get('added', 0)}/-{d.get('deleted', 0)}</td></tr>"
        for path, d in diff_stats.items())
    body = (f"<h2>Node {html.escape(node_id)}</h2>"
            "<table>" + info + "</table>"
            f"<h2>Attempt history ({len(attempts)})</h2>"
            "<table><tr><th>attempt</th><th>holder</th><th>started</th>"
            "<th>ended</th><th>outcome</th><th>error</th></tr>" +
            att_rows + "</table>"
            f"<h2>Evidence ({len(evidence)})</h2>"
            "<table><tr><th>path</th><th>summary</th></tr>" +
            ev_rows + "</table>"
            f"<h2>Diff stats ({len(diff_stats)} files)</h2>"
            "<table><tr><th>file</th><th>added/deleted</th></tr>" +
            diff_rows + "</table>")
    return _page(f"Node {node_id}", body)


def _metrics_dict(root: str, uptime_seconds: float) -> Dict[str, Any]:
    from . import graph as g
    from . import claim as c
    nodes = g.load_graph(root)
    events = g.load_events(root)
    now = c.now_utc()
    by_status: Dict[str, int] = {}
    attempts_by_outcome: Dict[str, int] = {}
    backends: Dict[str, Dict[str, int]] = {}
    retries_pending = 0
    shipped_nodes = 0
    for n in nodes.values():
        st = n.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        be = n.get("backend") or "unknown"
        bstat = backends.setdefault(be, {"done": 0, "failed": 0})
        for a in (n.get("attempts") or []):
            oc = a.get("outcome") or "unknown"
            attempts_by_outcome[oc] = attempts_by_outcome.get(oc, 0) + 1
            if oc == "done":
                bstat["done"] += 1
            elif oc in ("failed", "timeout"):
                bstat["failed"] += 1
        ra = c.parse_ts(n.get("retry_at"))
        if ra is not None and (ra - now).total_seconds() > 0:
            retries_pending += 1
        if n.get("shipped"):
            shipped_nodes += 1
    return {
        "nodes_by_status": by_status,
        "total_events": len(events),
        "rejected_events": sum(1 for e in events if e.get("type") == "rejected"),
        "attempts_by_outcome": attempts_by_outcome,
        "retries_pending": retries_pending,
        "shipped_nodes": shipped_nodes,
        "uptime_seconds": int(uptime_seconds),
        "backends": backends,
    }


def _prometheus_text(m: Dict[str, Any]) -> bytes:
    lines = []
    lines.append("# HELP skein_nodes Nodes by status")
    lines.append("# TYPE skein_nodes gauge")
    for st in sorted(m["nodes_by_status"]):
        lines.append(f'skein_nodes{{status="{st}"}} {m["nodes_by_status"][st]}')
    lines.append("# HELP skein_events_total Total events in the log")
    lines.append("# TYPE skein_events_total counter")
    lines.append(f'skein_events_total {m["total_events"]}')
    lines.append("# HELP skein_rejected_events_total Lifecycle rejections recorded")
    lines.append("# TYPE skein_rejected_events_total counter")
    lines.append(f'skein_rejected_events_total {m["rejected_events"]}')
    lines.append("# HELP skein_attempts_total Attempts by outcome")
    lines.append("# TYPE skein_attempts_total counter")
    for oc in sorted(m["attempts_by_outcome"]):
        lines.append(f'skein_attempts_total{{outcome="{oc}"}} {m["attempts_by_outcome"][oc]}')
    lines.append("# HELP skein_retries_pending Nodes waiting out retry backoff")
    lines.append("# TYPE skein_retries_pending gauge")
    lines.append(f'skein_retries_pending {m["retries_pending"]}')
    lines.append("# HELP skein_shipped_nodes Nodes shipped to at least one branch")
    lines.append("# TYPE skein_shipped_nodes gauge")
    lines.append(f'skein_shipped_nodes {m["shipped_nodes"]}')
    lines.append("# HELP skein_uptime_seconds Server uptime in seconds")
    lines.append("# TYPE skein_uptime_seconds counter")
    lines.append(f'skein_uptime_seconds {m["uptime_seconds"]}')
    for be in sorted(m["backends"]):
        b = m["backends"][be]
        lines.append(f'skein_backend_done_total{{backend="{be}"}} {b["done"]}')
        lines.append(f'skein_backend_failed_total{{backend="{be}"}} {b["failed"]}')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _health_dict(root: str) -> Dict[str, Any]:
    from . import graph as g
    from . import ids
    git_ok = False
    try:
        r = subprocess.run(["git", "rev-parse", "--git-dir"],
                           cwd=str(root), capture_output=True, timeout=10)
        git_ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        git_ok = False
    skein_dir = g.skein_dir(root)
    log_writable = skein_dir.is_dir() and os.access(str(skein_dir), os.W_OK)
    wt_root = ids.worktree_path_for(root, "x").parent
    wt_ok = (not wt_root.exists()) or (
        wt_root.is_dir() and os.access(str(wt_root), os.W_OK))
    ok = git_ok and log_writable and wt_ok
    return {"ok": ok, "repo": str(root), "git": git_ok,
            "log_writable": log_writable,
            "worktree_root_writable": wt_ok, "version": SKEIN_VERSION}


class _Handler(BaseHTTPRequestHandler):
    server_version = "SkeinCanvas/1.0"

    def log_message(self, *args):
        pass

    @property
    def _root(self) -> str:
        return self.server.repo_root

    def _send(self, code: int, payload: Any) -> None:
        if isinstance(payload, bytes):
            raw, ctype = payload, "text/html; charset=utf-8"
        else:
            raw, ctype = json.dumps(payload).encode(), "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Dict[str, Any]:
        length = self._content_length()
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _check_rate_limit(self) -> bool:
        """Best-effort per-IP rate limit. Sends 429 and returns False
        when the client is over 60 requests/minute."""
        now = time.monotonic()
        ip = self.client_address[0]
        state = self.server.rate_state
        dq = state.get(ip)
        if dq is None:
            dq = state[ip] = collections.deque()
        cutoff = now - _RATE_WINDOW
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            self._send(429, {"error": "rate limit exceeded (60 requests/minute per IP)"})
            return False
        dq.append(now)
        if len(state) > _MAX_TRACKED_IPS:
            # Drop the longest-idle bucket; insertion order is oldest first.
            state.pop(next(iter(state)))
        return True

    def _require_auth(self) -> bool:
        """Bearer-token gate for mutating requests. When no token is
        configured the canvas is open (local dashboard use). Sends 401
        and returns False on missing/invalid credentials, and records a
        `security` event - without the presented credential, so a
        mistyped real token never lands in the log."""
        token = getattr(self.server, "auth_token", None)
        if not token:
            return True
        presented = self.headers.get("Authorization", "")
        if hmac.compare_digest(presented, f"Bearer {token}"):
            return True
        try:
            from . import graph as g
            g.append_event(self._root, "serve", "security", "skein-serve",
                           {"kind": "serve_auth_failure",
                            "path": urlparse(self.path).path,
                            "client": self.client_address[0]},
                           commit=False)
        except Exception:
            pass  # the 401 is the control; the audit is best-effort
        self._send(401, {"error": "missing or invalid Authorization Bearer token"})
        return False

    def _mutation_preamble(self) -> bool:
        """Shared gate for POST/PUT/DELETE: rate limit, body cap, auth."""
        if not self._check_rate_limit():
            return False
        if self._content_length() > _MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large (limit 1 MB)"})
            return False
        return self._require_auth()

    def do_GET(self):
        if not self._check_rate_limit():
            return
        from . import graph as g
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send(200, _index_html().encode("utf-8"))
        if parsed.path == "/api/graph":
            nodes = g.load_graph(self._root)
            return self._send(200, {"nodes": nodes,
                                    "event_count": len(g.load_events(self._root))})
        if parsed.path == "/api/events":
            try:
                since = int(parse_qs(parsed.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            events = g.load_events(self._root)
            return self._send(200, {"events": events[since:], "count": len(events)})
        if parsed.path == "/api/backends":
            from .adapters.profiles import list_profiles
            return self._send(200, {"backends": [
                {"name": p.name, "verified": p.verified,
                 "source_note": p.source_note}
                for p in list_profiles(self._root)]})
        if parsed.path == "/api/health":
            return self._send(200, _health_dict(self._root))
        if parsed.path == "/api/metrics":
            uptime = time.time() - getattr(self.server, "started_at",
                                           time.time())
            return self._send(200, _metrics_dict(self._root, uptime))
        if parsed.path == "/metrics":
            uptime = time.time() - getattr(self.server, "started_at",
                                           time.time())
            raw = _prometheus_text(_metrics_dict(self._root, uptime))
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if parsed.path == "/timeline":
            qs = parse_qs(parsed.query)
            return self._send(200, _timeline_html(
                self._root,
                node_filter=qs.get("node", [""])[0],
                type_filter=qs.get("type", [""])[0]))
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2 and parts[0] == "node":
            page = _node_html(self._root, parts[1])
            if page is None:
                return self._send(404, {"error": "unknown node"})
            return self._send(200, page)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._mutation_preamble():
            return
        from . import claim as c
        from . import graph as g
        from .edits import add_node, edit_node
        parsed = urlparse(self.path)
        body = self._body()
        actor = str(body.get("actor") or "web")
        parts = [p for p in parsed.path.split("/") if p]
        # POST /api/nodes
        if parts == ["api", "nodes"]:
            for req_field in ("id",):
                if not body.get(req_field):
                    return self._send(400, {"error": f"missing '{req_field}'"})
            try:
                node = add_node(
                    self._root, actor, str(body["id"]),
                    title=str(body.get("title", "")),
                    goal=str(body.get("goal", "")),
                    context=str(body.get("context", "")),
                    constraints=str(body.get("constraints", "")),
                    completion=str(body.get("completion", "")),
                    depends_on=body.get("depends_on", []),
                    blast_radius=body.get("blast_radius", []),
                    backend=str(body.get("backend", "claude_code")),
                    backend_config=body.get("backend_config", {}),
                    change_policy=str(body.get("change_policy", "warn")),
                    max_retries=body.get("max_retries"),
                    retry_backoff_seconds=body.get("retry_backoff_seconds"))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, {"node": node})
        # POST /api/nodes/<id>[/claim|/release]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "nodes":
            node_id = parts[2]
            if len(parts) == 4 and parts[3] == "claim":
                try:
                    node = c.claim_node(
                        self._root, node_id, str(body.get("holder") or actor),
                        ttl_seconds=body.get("ttl_seconds"), actor=actor)
                except c.ClaimError as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"node": node})
            if len(parts) == 4 and parts[3] == "release":
                token = None
                if not body.get("force", False):
                    try:
                        claim = c.current_claim(self._root, node_id)
                    except c.ClaimError:
                        claim = {}
                    if claim.get("holder") == actor:
                        token = claim.get("claim_token")
                try:
                    node = c.release_node(self._root, node_id, actor=actor,
                                          force=bool(body.get("force", False)),
                                          claim_token=token)
                except c.ClaimError as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"node": node})
            if len(parts) == 4 and parts[3] == "ship":
                # Same rules as `skein ship`: the shipping module owns the
                # merge, the event, and the error cases.
                from . import shipping as sh
                from . import worktree as wt
                try:
                    result = sh.ship_node(
                        self._root, node_id, target=body.get("to"),
                        ff_only=bool(body.get("ff_only", False)),
                        force=bool(body.get("force", False)), actor=actor)
                except (sh.ShipError, wt.BaseCommitUnavailable) as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"result": result})
            if len(parts) == 3:
                fields = {k: v for k, v in body.items()
                          if k in ("title", "intent", "depends_on", "blast_radius",
                                   "status", "backend", "backend_config",
                                   "change_policy", "max_retries",
                                   "retry_backoff_seconds") and k != "actor"}
                try:
                    outcome = edit_node(self._root, actor, node_id, fields,
                                        delete=bool(body.get("delete", False)))
                except ValueError as e:
                    code = 404 if str(e).startswith("unknown node") else 400
                    return self._send(code, {"error": str(e)})
                return self._send(200, {"outcome": outcome,
                                        "node": g.load_graph(self._root).get(node_id)})
        # POST /api/release  (same rules as `skein release`)
        if parts == ["api", "release"]:
            from . import shipping as sh
            try:
                result = sh.release_tag(
                    self._root, str(body.get("tag") or ""),
                    message=body.get("message"),
                    allow_unshipped=bool(body.get("allow_unshipped", False)),
                    actor=actor)
            except sh.ReleaseError as e:
                return self._send(409, {"error": str(e)})
            return self._send(200, {"result": result})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        # No PUT routes exist; the gates still apply so a future route
        # cannot accidentally skip auth or rate limiting.
        if not self._mutation_preamble():
            return
        return self._send(405, {"error": "method not allowed"})

    def do_DELETE(self):
        if not self._mutation_preamble():
            return
        return self._send(405, {"error": "method not allowed"})


def make_server(repo_root: str, host: str = "127.0.0.1",
                port: int = 0,
                auth_token: Optional[str] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.repo_root = str(repo_root)
    # When set, mutating requests (POST/PUT/DELETE) require
    # Authorization: Bearer <token>; GETs stay open.
    server.auth_token = auth_token
    server.rate_state = {}
    server.daemon_threads = True
    # For /api/metrics uptime: wall-clock start of this server instance.
    server.started_at = time.time()
    return server


def serve_forever(repo_root: str, host: str = "127.0.0.1",
                  port: int = 8765,
                  auth_token: Optional[str] = None) -> None:
    if host in ("0.0.0.0", "::") and not auth_token:
        raise ValueError(
            "refusing to bind 0.0.0.0 without an auth token: pass "
            "--auth-token or set SKEIN_AUTH_TOKEN (the canvas has no login "
            "page; an unauthenticated network bind would expose the "
            "mutating API to anyone who can reach it)")
    server = make_server(repo_root, host, port, auth_token=auth_token)
    addr = server.server_address
    print(f"skein canvas at http://{addr[0]}:{addr[1]}/ (repo: {repo_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
