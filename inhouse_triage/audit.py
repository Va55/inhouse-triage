"""Audit trail for inhouse-triage.

Every routed request appends one JSON line to the audit log — the durable,
auditable record of every triage decision (timestamp, tier, score, signals,
model, latency, tokens). Runtime stats are kept in memory for /triage/stats;
the JSONL file is the source of truth.
"""

from __future__ import annotations

import json
import os
import threading
import time


class AuditLog:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._stats = {
            "routed": {"l1": 0, "l2": 0, "l3": 0},
            "direct": {"l1": 0, "l2": 0, "l3": 0},
            "upstream_errors": 0,
            "client_errors": 0,
        }
        self._started = time.time()
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, entry: dict) -> None:
        entry = dict(entry)
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._stats_key(entry).add_one()
        if not self._path:
            return
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def _stats_key(self, entry: dict):
        kind = entry.get("kind", "routed")
        tier = entry.get("tier", "l1")
        if kind not in self._stats:
            kind = "routed"
        if tier not in self._stats[kind]:
            tier = "l1"
        return _CounterView(self._stats[kind], tier)

    def record_error(self, error_kind: str) -> None:
        key = error_kind if error_kind in self._stats else "upstream_errors"
        with self._lock:
            self._stats[key] += 1

    def stats(self) -> dict:
        with self._lock:
            out = dict(self._stats)
            out["uptime_s"] = round(time.time() - self._started, 1)
            return out

    def tail(self, n: int = 50) -> list[dict]:
        if not self._path or not os.path.exists(self._path):
            return []
        lines = []
        with open(self._path, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


class _CounterView:
    def __init__(self, bucket: dict, key: str):
        self._bucket = bucket
        self._key = key

    def add_one(self) -> None:
        self._bucket[self._key] = self._bucket.get(self._key, 0) + 1
