"""inhouse-triage — OpenAI-compatible ITIL triage router.

Presents a single /v1/chat/completions endpoint. Every request is either
routed directly (explicit model id / X-Triage-Override header) or
classified (ITIL heuristics + optional local LLM tiebreaker) into
L1 (local llama-server) / L2 (deepseek-v4-flash) / L3 (deepseek-v4-pro),
then relayed verbatim to the chosen upstream. Streaming is passed through.

Pure stdlib — no dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import AuditLog
from .classifier import (
    L1,
    L2,
    L3,
    classify_request,
    parse_tiebreaker,
    tiebreaker_prompt,
)
from .config import Config

#: Middle band (raw score) where the LLM tiebreaker may decide
TIEBREAK_LOW, TIEBREAK_HIGH = 0.5, 2.2


class SessionStore:
    """In-memory per-session tier ladder (ticket ownership, no downgrade)."""

    def __init__(self, ttl_s: float = 3600):
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, str]] = {}

    def get(self, sid: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            hit = self._sessions.get(sid)
            if not hit:
                return None
            if now - hit[0] > self._ttl:
                del self._sessions[sid]
                return None
            return hit[1]

    def set(self, sid: str, tier: str) -> None:
        with self._lock:
            self._sessions[sid] = (time.monotonic(), tier)

    def size(self) -> int:
        with self._lock:
            return len(self._sessions)


class TriageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, cfg: Config, audit: AuditLog):
        super().__init__(addr, handler)
        self.cfg = cfg
        self.audit = audit
        self.sessions = SessionStore(cfg.get("session_ttl_s", 3600))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "inhouse-triage/0.1"

    # ── Helpers ────────────────────────────────────────────────────────────

    @property
    def cfg(self) -> Config:
        return self.server.cfg

    @property
    def audit(self) -> AuditLog:
        return self.server.audit

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default stderr noise
        return

    # ── GET endpoints ──────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send_json(
                200,
                {
                    "status": "ok",
                    "tiers": self.cfg.tier_names(),
                    "sessions": self.server.sessions.size(),
                },
            )
        if path == "/v1/models":
            models = [
                {
                    "id": t["name"],
                    "object": "model",
                    "owned_by": "inhouse-triage",
                    "tier": tier,
                }
                for tier, t in self.cfg._d["tiers"].items()
            ]
            return self._send_json(200, {"object": "list", "data": models})
        if path == "/triage/stats":
            return self._send_json(200, self.audit.stats())
        if path == "/triage/log":
            try:
                n = int(self.path.split("tail=", 1)[1].split("&", 1)[0])
            except (IndexError, ValueError):
                n = 50
            return self._send_json(200, {"entries": self.audit.tail(max(1, min(n, 1000)))})
        return self._send_text(404, "not found")

    # ── POST /v1/chat/completions ──────────────────────────────────────────

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            return self._send_text(404, "not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > self.cfg.get("max_body_bytes", 20 * 1024 * 1024):
            self.audit.record_error("client_errors")
            return self._send_text(400, "bad request: missing or oversized body")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.audit.record_error("client_errors")
            return self._send_text(400, "bad request: invalid JSON")

        t0 = time.monotonic()
        session_id = self.headers.get("X-Session-Id") or body.get("session_id") or "-"
        override = (self.headers.get("X-Triage-Override") or "").strip().lower()
        stream = bool(body.get("stream", False))

        # 1) explicit model id -> direct route
        model = body.get("model") or ""
        known = self.cfg.known_model_ids()
        if override in (L1, L2, L3):
            tier, kind = override, "override"
        elif model in known and model != self.cfg.get("triage_model_name"):
            tier, kind = known[model], "direct"
        else:
            # 2) classify
            user_text = _last_user_text(body)
            session_tier = (
                None if session_id == "-" else self.server.sessions.get(session_id)
            )
            tier, score, signals = classify_request(
                user_text, message_count=len(body.get("messages") or []),
                session_tier=session_tier,
            )
            tiebreak = None
            tb_cfg = self.cfg.get("llm_tiebreaker") or {}
            if (
                tb_cfg.get("enabled", True)
                and TIEBREAK_LOW <= score < TIEBREAK_HIGH
            ):
                tiebreak = self._llm_tiebreak(user_text, tb_cfg)
                if tiebreak in (L1, L2, L3):
                    tier = tiebreak
            kind = "routed"
            audit_extra = {
                "score": score,
                "signals": signals,
                "tiebreak": tiebreak,
            }
        if kind != "routed":
            audit_extra = {}

        tier_cfg = self.cfg.tier(tier)
        if not tier_cfg.get("base_url"):
            self.audit.record_error("client_errors")
            return self._send_text(502, f"tier {tier} not configured")

        # Upstreams validate the model field (DeepSeek rejects anything but
        # its own ids; llama-server ignores it). Rewrite it to the tier's
        # upstream name so the verbatim relay carries a valid model id.
        upstream_name = tier_cfg.get("name") or model
        if body.get("model") != upstream_name:
            body["model"] = upstream_name
            raw = json.dumps(body).encode()

        # DeepSeek v4-flash/pro are reasoning models: max_tokens caps
        # reasoning + final answer TOGETHER, so a small cap returns HTTP 200
        # with empty content (deep test 2026-08-15). Enforce a floor on the
        # paid tiers so the answer survives the thinking budget.
        if tier in (L2, L3):
            floor = self.cfg.get("min_max_tokens_l23", 2048)
            mt = body.get("max_tokens")
            if isinstance(mt, int) and mt < floor:
                body["max_tokens"] = floor
                raw = json.dumps(body).encode()
            # Per-tier reasoning toggle, mirroring llama-server's
            # `--reasoning off`. Empirically verified on this API: the
            # OpenAI-style `thinking: {type: disabled}` param kills the
            # reasoning phase (reasoning_tokens absent, direct answer).
            # Measured on the CAP question: 134s/6618 tok with reasoning vs
            # 30s/1545 tok without, no visible quality loss.
            if tier_cfg.get("reasoning_off"):
                tb = body.get("thinking")
                if not isinstance(tb, dict) or tb.get("type") != "disabled":
                    body["thinking"] = {"type": "disabled"}
                    raw = json.dumps(body).encode()

        # 3) commit the tier to the session ladder BEFORE relaying. The
        #    response leaves inside _relay, and audit.write() (open+flush+
        #    fsync) used to sit between response-send and sessions.set() — a
        #    fast client's next turn could be classified before the set()
        #    landed, silently downgrading an escalated session (deep test
        #    2026-08-15). Ticket ownership must not depend on upstream health:
        #    if the relay fails, a retry still deserves the same tier.
        if session_id != "-":
            self.server.sessions.set(session_id, tier)

        # 4) relay verbatim
        status, latency_ms, tokens = self._relay(raw, tier_cfg, stream)

        # 5) audit (fsync'd; runs after the ladder commit)
        self.audit.write(
            {
                "kind": kind,
                "tier": tier,
                "session": session_id,
                "model": tier_cfg.get("name", model),
                "status": status,
                "latency_ms": round(latency_ms),
                "in_tokens": tokens[0],
                "out_tokens": tokens[1],
                "override": override or None,
                **audit_extra,
            }
        )

    # ── Upstream plumbing ──────────────────────────────────────────────────

    def _relay(self, raw_body: bytes, tier_cfg: dict, stream: bool):
        url = tier_cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Authorization": f"Bearer {tier_cfg.get('api_key', '')}",
            "Accept-Encoding": "identity",
        }
        req = urllib.request.Request(
            url, data=raw_body, headers=headers, method="POST"
        )
        t0 = time.monotonic()
        try:
            resp = urllib.request.urlopen(
                req, timeout=self.cfg.get("upstream_timeout_s", 300)
            )
        except urllib.error.HTTPError as exc:
            # Pass upstream errors through untouched (Hermes' fallback chain
            # downstream reacts to these the same way it would without us).
            err_body = exc.read()
            self.send_response(exc.code)
            ctype = exc.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            try:
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.audit.record_error("upstream_errors")
            return exc.code, (time.monotonic() - t0) * 1000, (None, None)
        except Exception:
            self.audit.record_error("upstream_errors")
            self._send_text(502, "upstream unreachable")
            return 502, (time.monotonic() - t0) * 1000, (None, None)

        status = resp.status
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")

        tokens = (None, None)
        if stream:
            # chunked passthrough
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                resp.close()
        else:
            payload = resp.read()
            tokens = _usage_tokens(payload)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            resp.close()
        return status, (time.monotonic() - t0) * 1000, tokens

    def _llm_tiebreak(self, user_text: str, tb_cfg: dict) -> str | None:
        url = tb_cfg.get("base_url", "").rstrip("/") + "/chat/completions"
        if not url:
            return None
        payload = json.dumps(
            {
                "model": tb_cfg.get("model", "triage"),
                "messages": tiebreaker_prompt(user_text),
                "max_tokens": tb_cfg.get("max_tokens", 64),
                "temperature": 0.0,
                "stream": False,
            }
        ).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {tb_cfg.get('api_key', '')}",
                "Accept-Encoding": "identity",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=tb_cfg.get("timeout_s", 45)
            )
            data = json.loads(resp.read())
            return parse_tiebreaker(
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except Exception:
            return None

    # 405 for anything else
    def do_PUT(self):
        self._send_text(405, "method not allowed")

    def do_DELETE(self):
        self._send_text(405, "method not allowed")


def _last_user_text(body: dict) -> str:
    msgs = body.get("messages") or []
    for msg in reversed(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return " ".join(parts)
    return ""


def _usage_tokens(payload: bytes) -> tuple[int | None, int | None]:
    try:
        data = json.loads(payload)
        usage = data.get("usage") or {}
        return usage.get("prompt_tokens"), usage.get("completion_tokens")
    except Exception:
        return None, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="inhouse-triage router")
    ap.add_argument("--config", default=None, help="path to config.json")
    ap.add_argument("--port", type=int, default=None, help="override listen port")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    port = args.port or cfg.get("port", 8095)
    audit = AuditLog(cfg.get("audit_log", ""))
    server = TriageServer((cfg.get("listen", "0.0.0.0"), port), Handler, cfg, audit)
    print(
        f"inhouse-triage listening on {cfg.get('listen', '0.0.0.0')}:{port} "
        f"tiers={cfg.tier_names()} audit={cfg.get('audit_log') or '(none)'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
