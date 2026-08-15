"""Integration tests for the triage router with fake upstreams.

Spins the real server on an ephemeral port with three fake upstreams that
stamp their tier into the response body, then verifies routing, direct
model override, streaming passthrough, session ladder, and the audit log.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inhouse_triage.server import TriageServer, Handler  # noqa: E402


class FakeUpstream(BaseHTTPRequestHandler):
    tag = "unknown"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            LAST_MODEL[self.tag] = json.loads(raw).get("model")
        except Exception:
            LAST_MODEL[self.tag] = None
        stream = "stream" in self.path
        body = json.dumps(
            {
                "id": f"chatcmpl-fake-{self.tag}",
                "object": "chat.completion",
                "model": self.tag,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"FAKE-{self.tag}-OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ).encode()
        if stream:
            sse = f"data: {json.dumps({'choices':[{'delta':{'content':f'FAKE-{self.tag}-OK'}}]})}\n\ndata: [DONE]\n\n"
            body = sse.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeL1(FakeUpstream):
    tag = "l1"


class FakeL2(FakeUpstream):
    tag = "l2"


class FakeL3(FakeUpstream):
    tag = "l3"


def _spawn_fake(cls, port_holder):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    port_holder.append(srv.server_address[1])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _chat(model="triage", content="hello", headers=None, stream=False):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % _PORT,
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": stream,
            }
        ).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return resp.status, resp.read().decode()


#: last model field each fake upstream received (set by FakeUpstream.do_POST)
LAST_MODEL: dict[str, str | None] = {}


_PORT = 0


def main():
    global _PORT
    fake_ports = {}
    servers = {}
    for name, cls in (("l1", FakeL1), ("l2", FakeL2), ("l3", FakeL3)):
        holder = []
        srv = _spawn_fake(cls, holder)
        fake_ports[name] = holder[0]
        servers[name] = srv

    cfg = {
        "listen": "127.0.0.1",
        "port": 0,
        "tiers": {
            "l1": {"name": "local-35b", "base_url": f"http://127.0.0.1:{fake_ports['l1']}/v1", "api_key": "local"},
            "l2": {"name": "deepseek-v4-flash", "base_url": f"http://127.0.0.1:{fake_ports['l2']}/v1", "api_key": "x"},
            "l3": {"name": "deepseek-v4-pro", "base_url": f"http://127.0.0.1:{fake_ports['l3']}/v1", "api_key": "x"},
        },
        "llm_tiebreaker": {"enabled": False},
        "audit_log": "",
        "session_ttl_s": 3600,
        "upstream_timeout_s": 30,
    }
    from inhouse_triage.config import Config

    tmp = tempfile.mktemp(suffix=".json")
    with open(tmp, "w") as fh:
        json.dump(cfg, fh)
    config = Config.load(tmp)
    audit_log_file = tempfile.mktemp(suffix=".log")
    from inhouse_triage.audit import AuditLog

    audit = AuditLog(audit_log_file)
    srv = TriageServer(("127.0.0.1", 0), Handler, config, audit)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _PORT = srv.server_address[1]
    time.sleep(0.3)

    failures = []
    total_checks = [0]

    def check(name, cond, detail=""):
        total_checks[0] += 1
        print(f"{'PASS' if cond else 'FAIL'} {name} {detail}")
        if not cond:
            failures.append(name)

    # 1. simple request -> L1
    status, body = _chat(content="What time is it?")
    check("simple->l1", status == 200 and "FAKE-l1-OK" in body, body)
    check("model-rewrite-l1", LAST_MODEL.get("l1") == "local-35b", str(LAST_MODEL))

    # 2. complex request -> L2
    status, body = _chat(
        content="My app crashes when main.py and utils.py run together, here is the traceback, fix the debug issue."
    )
    check("debug->l2", status == 200 and "FAKE-l2-OK" in body, body)
    check("model-rewrite-l2", LAST_MODEL.get("l2") == "deepseek-v4-flash", str(LAST_MODEL))

    # 3. formal reasoning -> L3
    status, body = _chat(content="Prove the correctness of the algorithm and derive the formal complexity bound.")
    check("formal->l3", status == 200 and "FAKE-l3-OK" in body, body)

    # 4. explicit model id -> direct route
    status, body = _chat(model="deepseek-v4-pro", content="hello")
    check("direct-model->l3", status == 200 and "FAKE-l3-OK" in body, body)
    check("model-unchanged-direct", LAST_MODEL.get("l3") == "deepseek-v4-pro", str(LAST_MODEL))

    # 5. override header forces tier
    status, body = _chat(content="What time is it?", headers={"X-Triage-Override": "l3"})
    check("override->l3", status == 200 and "FAKE-l3-OK" in body, body)

    # 6. session ladder: escalated session never downgrades
    status, body = _chat(content="Prove the theorem.", headers={"X-Session-Id": "sess-1"})
    check("session-first->l3", "FAKE-l3-OK" in body, body)
    status, body = _chat(content="What time is it?", headers={"X-Session-Id": "sess-1"})
    check("session-stays-l3", "FAKE-l3-OK" in body, body)

    # 7. streaming passthrough
    status, body = _chat(content="What time is it?", stream=True)
    check("stream->l1", status == 200 and "FAKE-l1-OK" in body, body[:80])

    # 8. models + health + stats endpoints
    m = urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/v1/models", timeout=10).read().decode()
    check("models-3", json.loads(m)["data"].__len__() == 3)
    h = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/health", timeout=10).read())
    check("health-ok", h["status"] == "ok")
    s = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/triage/stats", timeout=10).read())
    check("stats-l1", s["routed"]["l1"] >= 2, str(s["routed"]))

    # 9. audit log written
    entries = [json.loads(l) for l in open(audit_log_file) if l.strip()]
    check("audit-written", len(entries) >= 8, f"{len(entries)} entries")
    kinds = {e["kind"] for e in entries}
    check("audit-kinds", "routed" in kinds and "direct" in kinds and "override" in kinds)
    l3sess = [e for e in entries if e.get("session") == "sess-1"]
    check("audit-session-ladder", all(e["tier"] == "l3" for e in l3sess), str([e["tier"] for e in l3sess]))
    scored = [e for e in entries if e["kind"] == "routed"]
    check("audit-score-present", all("score" in e and "signals" in e for e in scored))

    # 10. upstream error passthrough (dead L2 fake -> use override to hit a closed port)
    dead_cfg = {
        "listen": "127.0.0.1", "port": 0,
        "tiers": {
            "l1": {"name": "x", "base_url": f"http://127.0.0.1:{fake_ports['l1']}/v1", "api_key": "local"},
            "l2": {"name": "y", "base_url": "http://127.0.0.1:1/v1", "api_key": "x"},
            "l3": {"name": "z", "base_url": f"http://127.0.0.1:{fake_ports['l3']}/v1", "api_key": "x"},
        },
        "llm_tiebreaker": {"enabled": False},
        "audit_log": "", "session_ttl_s": 3600, "upstream_timeout_s": 10,
    }
    tmp2 = tempfile.mktemp(suffix=".json")
    with open(tmp2, "w") as fh:
        json.dump(dead_cfg, fh)
    audit2 = AuditLog("")
    srv2 = TriageServer(("127.0.0.1", 0), Handler, Config.load(tmp2), audit2)
    t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
    t2.start()
    p2 = srv2.server_address[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{p2}/v1/chat/completions",
            data=json.dumps({"model": "y", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            check("dead-upstream-502", False, "expected 502")
        except urllib.error.HTTPError as e:
            check("dead-upstream-502", e.code == 502, str(e.code))
        st = audit2.stats()
        check("dead-upstream-counted", st["upstream_errors"] >= 1, str(st))
    finally:
        srv2.shutdown()

    srv.shutdown()
    for s in servers.values():
        s.shutdown()

    print(f"\n{total_checks[0] - len(failures)}/{total_checks[0]} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
