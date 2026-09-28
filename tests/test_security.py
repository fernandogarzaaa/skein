"""Phase 6: secret redaction, backend env scrubbing, sandbox, serve hardening."""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

import pytest

from skein import graph as g
from skein import redact
from skein import runtime as rt

PY = sys.executable


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    monkeypatch.chdir(tmp_path)
    from skein.cli import main as skein_main
    assert skein_main(["init"]) == 0
    return tmp_path


def _wait_ready(srv, timeout=5.0):
    """Wait until the fixture server thread is accepting connections.

    Without this, the first request in a test can race the serve_forever
    thread and fail with connection-refused.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(
                ("127.0.0.1", srv.server_address[1]), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError("serve fixture never became ready")


@pytest.fixture
def server(repo):
    from skein.serve import make_server
    srv = make_server(str(repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _wait_ready(srv)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def authed_server(repo):
    from skein.serve import make_server
    srv = make_server(str(repo), port=0, auth_token="test-token-123")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _wait_ready(srv)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- redact_secrets unit tests -------------------------------------------


def test_redact_aws_key():
    out, n = redact.redact_with_count("key AKIAIOSFODNN7EXAMPLE here")
    assert n == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert out == "key [REDACTED] here"


def test_redact_github_tokens():
    for tok in ("ghp_" + "a" * 36, "gho_" + "b" * 36):
        out, n = redact.redact_with_count(f"token={tok}")
        assert n == 1, tok
        assert tok not in out


def test_redact_sk_variants():
    out, n = redact.redact_with_count(
        "a sk-ant-abc123XYZ_-99 and sk-abcdefghijklmnopqrst end")
    assert n == 2
    assert "sk-ant-" not in out and "sk-abcdefghijklmnopqrst" not in out


def test_redact_slack_tokens():
    for tok in ("xoxb-123456789012-abcdefghij", "xoxp-123456789012-abcdefghij"):
        out, n = redact.redact_with_count(tok)
        assert n == 1, tok
        assert tok not in out


def test_redact_private_key_block():
    key = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIBOgIBAAJBAKc123\n"
           "-----END RSA PRIVATE KEY-----")
    out, n = redact.redact_with_count(f"before\n{key}\nafter")
    assert n == 1
    assert "MIIBOgIBAAJBAKc123" not in out
    assert "PRIVATE KEY" not in out
    assert out.startswith("before\n") and out.endswith("\nafter")


def test_redact_assignments():
    out, n = redact.redact_with_count(
        "password=hunter2 api_key='abc123' PASSWORD = \"quoted; semi\"")
    assert n == 3
    assert out == ("password=[REDACTED] api_key=[REDACTED] "
                   "PASSWORD=[REDACTED]")


def test_redact_leaves_prose_untouched():
    prose = ("The password field is required. Keep your secret safe and "
             "never share it. Document the api_key parameter. A password "
             "alone is not a credential.")
    out, n = redact.redact_with_count(prose)
    assert n == 0
    assert out == prose


def test_redact_idempotent():
    s = ("AKIAIOSFODNN7EXAMPLE sk-ant-abc123XYZ_-99 password=hunter2 "
         "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")
    once = redact.redact_secrets(s)
    assert redact.redact_secrets(once) == once
    assert "AKIA" not in once and "hunter2" not in once


def test_redact_empty_and_non_string():
    assert redact.redact_secrets("") == ""
    assert redact.redact_with_count(None) == (None, 0)


def test_redact_payload_nested():
    payload = {"goal": "use sk-ant-abc123XYZ_-99",
               "nested": {"list": ["a", "password=hunter2"]},
               "count": 3, "flag": True, "nothing": None}
    out, n = redact.redact_payload(payload)
    assert n == 2
    assert out["goal"] == "use [REDACTED]"
    assert out["nested"]["list"][1] == "password=[REDACTED]"
    assert out["count"] == 3 and out["flag"] is True and out["nothing"] is None
    # input not mutated
    assert "sk-ant-" in payload["goal"]


# --- event-log payload writer boundary ------------------------------------


def test_append_event_redacts_payload_and_audits(repo):
    from skein.cli import main as skein_main
    secret = "sk-ant-" + "f" * 20
    assert skein_main(["node", "add", "s1", "--title", "T",
                       "--goal", f"deploy with {secret}",
                       "--completion", "true"]) == 0
    node = g.load_graph(str(repo))["s1"]
    assert secret not in node["intent"]["goal"]
    assert "[REDACTED]" in node["intent"]["goal"]
    raw = g.log_path(str(repo)).read_text(encoding="utf-8")
    assert secret not in raw
    sec = [e for e in g.load_events(str(repo)) if e["type"] == "security"]
    assert sec, "expected a security audit event"
    last = sec[-1]["payload"]
    assert last["kind"] == "redaction"
    assert last["event_type"] == "node_added"
    assert last["redacted_count"] >= 1
    assert secret not in json.dumps(sec)


def test_handoff_note_redacted_at_complete(repo):
    # complete_node path: the handoff note flows through the payload writer
    from skein.cli import main as skein_main
    assert skein_main(["node", "add", "s2", "--title", "T", "--goal", "g",
                       "--completion", "true"]) == 0
    assert skein_main(["claim", "s2", "--agent-id", "a1"]) == 0
    node = g.load_graph(str(repo))["s2"]
    claim = node["claim"]
    from skein import claim as c
    secret = "ghp_" + "c" * 36
    c.complete_node(str(repo), "s2", "a1", claim["claim_token"],
                    handoff_note=f"rotated {secret} ok", evidence=[],
                    worktree={}, result={})
    raw = g.log_path(str(repo)).read_text(encoding="utf-8")
    assert secret not in raw
    node = g.load_graph(str(repo))["s2"]
    assert secret not in node["handoff_note"]


# --- evidence file boundary -------------------------------------------------


def test_run_completion_redacts_evidence_files(tmp_path):
    from skein import verify as v
    tok = "ghp_" + "d" * 36
    ok, evidence, hits = v.run_completion(
        tmp_path, f"{PY} -c \"print('{tok}')\"", tmp_path / "ev", "n9")
    assert ok is True
    assert hits >= 2  # the $ cmd line and the printed output
    content = open(evidence[0]["output_ref"], encoding="utf-8").read()
    assert tok not in content
    assert "[REDACTED]" in content


def test_adapter_evidence_redacted(repo):
    from skein.supervisor import _write_adapter_evidence
    tok = "xoxb-" + "1" * 20
    entry, hits = _write_adapter_evidence(
        str(repo), type("A", (), {"name": "fake"})(), "n10", 0,
        f"leaked {tok} in output")
    assert hits == 1
    content = open(entry["output_ref"], encoding="utf-8").read()
    assert tok not in content


# --- backend environment scrubbing -------------------------------------------


def test_minimal_environ_allowlist(monkeypatch):
    monkeypatch.setenv("PHASE6_JUNK", "should-not-pass")
    monkeypatch.setenv("SKEIN_PHASE6_OK", "should-pass")
    env = rt.minimal_environ()
    assert "PHASE6_JUNK" not in env
    assert env.get("SKEIN_PHASE6_OK") == "should-pass"
    assert "PATH" in env


def test_execute_env_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("PHASE6_JUNK", "should-not-pass")
    r = rt.execute(
        [PY, "-c", "import os; print(os.environ.get('PHASE6_JUNK', 'ABSENT'))"],
        cwd=str(tmp_path), timeout=30, env=rt.minimal_environ())
    assert r.exit_code == 0
    assert "ABSENT" in r.stdout


# --- sandbox -----------------------------------------------------------------


def test_sandbox_runs_and_reports(tmp_path):
    import shutil
    r = rt.execute([PY, "-c", "print('sandbox-ok')"], cwd=str(tmp_path),
                   timeout=30, sandbox=True)
    assert r.exit_code == 0
    assert "sandbox-ok" in r.stdout
    if shutil.which("prlimit"):
        assert r.sandbox_note == ""
    else:
        assert "prlimit" in r.sandbox_note


def test_sandbox_missing_prlimit_warns_and_continues(tmp_path, monkeypatch):
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda name, *a, **k: None if name == "prlimit" else real_which(name, *a, **k)
    )
    r = rt.execute([PY, "-c", "print('still-runs')"], cwd=str(tmp_path),
                   timeout=30, sandbox=True)
    assert r.exit_code == 0
    assert "still-runs" in r.stdout
    assert "prlimit" in r.sandbox_note


def test_sandbox_missing_binary_still_127(tmp_path):
    r = rt.execute(["definitely-not-a-real-binary-xyz"], cwd=str(tmp_path),
                   timeout=30, sandbox=True)
    assert r.exit_code == 127
    assert "definitely-not-a-real-binary-xyz" in r.stderr


def test_sandbox_shell_command(tmp_path):
    r = rt.execute("echo shell-sandbox-ok", cwd=str(tmp_path),
                   timeout=30, shell=True, sandbox=True)
    assert r.exit_code == 0
    assert "shell-sandbox-ok" in r.stdout


def test_run_node_sandbox_fallback_audited(repo, monkeypatch):
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda name, *a, **k: None if name == "prlimit" else real_which(name, *a, **k)
    )
    from skein.cli import main as skein_main
    from skein.supervisor import run_node

    class FakeAdapter:
        name = "fake"
        def build_command(self, node):
            return [sys.executable, "-c", "pass"]
        def build_prompt(self, node):
            return "fake"

    assert skein_main(["node", "add", "sb1", "--title", "T", "--goal", "g",
                       "--completion", f"{PY} -c \"print('ok')\""]) == 0
    result = run_node(str(repo), "sb1", "agent-1", adapter=FakeAdapter(),
                      heartbeat_interval=0.2, poll_interval=0.05, sandbox=True)
    assert result["outcome"] == "done"
    sec = [e for e in g.load_events(str(repo))
           if e["type"] == "security"
           and e["payload"].get("kind") == "sandbox_fallback"]
    assert sec, "expected a sandbox_fallback security event"
    assert "prlimit" in sec[0]["payload"]["note"]


# --- serve hardening ------------------------------------------------------------


def test_mutations_require_token_when_configured(authed_server):
    code, _ = _call("POST", authed_server + "/api/nodes",
                    {"id": "a1", "title": "T", "goal": "g", "completion": "true"})
    assert code == 401
    code, _ = _call("POST", authed_server + "/api/nodes",
                    {"id": "a1", "title": "T", "goal": "g", "completion": "true"},
                    headers=_bearer("wrong-token"))
    assert code == 401
    # GETs stay open for dashboard viewing
    code, _ = _call("GET", authed_server + "/api/graph")
    assert code == 200
    code, data = _call("POST", authed_server + "/api/nodes",
                       {"id": "a1", "title": "T", "goal": "g", "completion": "true"},
                       headers=_bearer("test-token-123"))
    assert code == 201


def test_no_token_keeps_canvas_open(server):
    code, _ = _call("POST", server + "/api/nodes",
                    {"id": "open1", "title": "T", "goal": "g", "completion": "true"})
    assert code == 201


def test_auth_failure_audited_without_credential(authed_server, repo):
    code, _ = _call("POST", authed_server + "/api/nodes", {"id": "nope"})
    assert code == 401
    sec = [e for e in g.load_events(str(repo))
           if e["type"] == "security"
           and e["payload"].get("kind") == "serve_auth_failure"]
    assert sec, "expected a serve_auth_failure security event"
    assert "test-token-123" not in json.dumps(sec)  # real token never logged


def test_body_size_limit(authed_server):
    # Deterministic 413 check: send headers (with Expect: 100-continue)
    # over a raw socket and never upload the body. The server answers
    # 413 to the Content-Length header alone, so there is no
    # close-while-sending race (the old urllib 1 MB upload flaked with
    # BrokenPipeError when the server closed mid-send).
    import socket
    host, port = "127.0.0.1", int(authed_server.rsplit(":", 1)[1])
    body_len = 2 ** 20 + 100  # over the 1 MB cap
    head = (
        "POST /api/nodes HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {body_len}\r\n"
        "Authorization: Bearer test-token-123\r\n"
        "Expect: 100-continue\r\n"
        "\r\n"
    )
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(head.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
    status_line = resp.split(b"\r\n", 1)[0].decode()
    assert status_line.startswith("HTTP/1.0 413") or \
        status_line.startswith("HTTP/1.1 413"), status_line


def test_rate_limit_429(repo, monkeypatch):
    import skein.serve as serve_mod
    monkeypatch.setattr(serve_mod, "_RATE_LIMIT", 3)
    from skein.serve import make_server
    srv = make_server(str(repo), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _wait_ready(srv)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        codes = [_call("GET", base + "/api/graph")[0] for _ in range(4)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429
    finally:
        srv.shutdown()


def test_put_delete_gated(authed_server):
    assert _call("PUT", authed_server + "/api/nodes", {"id": "p1"})[0] == 401
    assert _call("DELETE", authed_server + "/api/nodes")[0] == 401
    code, _ = _call("PUT", authed_server + "/api/nodes", {"id": "p1"},
                    headers=_bearer("test-token-123"))
    assert code == 405


def test_bind_all_without_token_refused(repo):
    from skein.serve import serve_forever
    with pytest.raises(ValueError, match="auth token"):
        serve_forever(str(repo), host="0.0.0.0", port=0)


def test_serve_mutation_redacts_like_cli(authed_server, repo):
    code, _ = _call("POST", authed_server + "/api/nodes",
                    {"id": "sec1", "title": "T",
                     "goal": "deploy with password=hunter2",
                     "completion": "true"},
                    headers=_bearer("test-token-123"))
    assert code == 201
    node = g.load_graph(str(repo))["sec1"]
    assert "hunter2" not in node["intent"]["goal"]
    assert node["intent"]["goal"] == "deploy with password=[REDACTED]"


# --- CLI plumbing ---------------------------------------------------------------


def test_run_sandbox_flag_plumbing(repo, monkeypatch):
    import skein.supervisor as sup
    seen = {}

    def fake_run_node(root, node_id, holder, **kwargs):
        seen.update(kwargs)
        return {"outcome": "done"}

    monkeypatch.setattr(sup, "run_node", fake_run_node)
    from skein.cli import main as skein_main
    assert skein_main(["node", "add", "sb9", "--title", "T", "--goal", "g",
                       "--completion", "true"]) == 0
    assert skein_main(["run", "sb9", "--sandbox", "--agent-id", "a1"]) == 0
    assert seen.get("sandbox") is True


def test_run_config_default_sandbox(repo, monkeypatch):
    import skein.supervisor as sup
    seen = {}

    def fake_run_node(root, node_id, holder, **kwargs):
        seen.update(kwargs)
        return {"outcome": "done"}

    monkeypatch.setattr(sup, "run_node", fake_run_node)
    from skein.cli import main as skein_main
    cfg = g.load_config(str(repo))
    cfg["default_sandbox"] = True
    g.config_path(str(repo)).write_text(json.dumps(cfg), encoding="utf-8")
    assert skein_main(["node", "add", "sb10", "--title", "T", "--goal", "g",
                       "--completion", "true"]) == 0
    assert skein_main(["run", "sb10", "--agent-id", "a1"]) == 0
    assert seen.get("sandbox") is True


def test_log_shows_security_events(repo, capsys):
    from skein.cli import main as skein_main
    g.append_event(str(repo), "tester", "security", "skein-serve",
                   {"kind": "serve_auth_failure", "path": "/api/nodes",
                    "client": "1.2.3.4"})
    assert skein_main(["log"]) == 0
    out = capsys.readouterr().out
    assert "security" in out
    assert "serve_auth_failure" in out
