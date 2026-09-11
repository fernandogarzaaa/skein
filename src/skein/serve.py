"""Web canvas: live graph visualization + human editing over HTTP.

Stdlib only (no new dependencies). The API reuses the same edit rules
as the CLI (edits module), so a human editing a claimed node from the
browser appends human_interrupt exactly like `skein node edit` does.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


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
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self):
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
                    backend_config=body.get("backend_config", {}))
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
                try:
                    node = c.release_node(self._root, node_id, actor=actor,
                                          force=bool(body.get("force", False)))
                except c.ClaimError as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"node": node})
            if len(parts) == 3:
                fields = {k: v for k, v in body.items()
                          if k in ("title", "intent", "depends_on", "blast_radius",
                                   "status", "backend", "backend_config") and k != "actor"}
                try:
                    outcome = edit_node(self._root, actor, node_id, fields,
                                        delete=bool(body.get("delete", False)))
                except ValueError as e:
                    code = 404 if str(e).startswith("unknown node") else 400
                    return self._send(code, {"error": str(e)})
                return self._send(200, {"outcome": outcome,
                                        "node": g.load_graph(self._root).get(node_id)})
        return self._send(404, {"error": "not found"})


def make_server(repo_root: str, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.repo_root = str(repo_root)
    server.daemon_threads = True
    return server


def serve_forever(repo_root: str, host: str = "127.0.0.1",
                  port: int = 8765) -> None:
    server = make_server(repo_root, host, port)
    addr = server.server_address
    print(f"skein canvas at http://{addr[0]}:{addr[1]}/ (repo: {repo_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
