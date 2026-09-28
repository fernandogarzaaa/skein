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
"""

from __future__ import annotations

import collections
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# Request hardening limits.
_MAX_BODY_BYTES = 1_000_000  # 413 on excess
_RATE_LIMIT = 60  # requests ...
_RATE_WINDOW = 60.0  # ... per 60 seconds, per client IP (429 on excess)
_MAX_TRACKED_IPS = 10000  # bound on the in-memory rate-limit table


def _index_html() -> str:
    path = Path(__file__).resolve().parent / "static" / "app.html"
    return path.read_text(encoding="utf-8")


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
            from urllib.parse import parse_qs
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
