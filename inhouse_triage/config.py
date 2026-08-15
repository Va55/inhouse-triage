"""Configuration for inhouse-triage.

Config file: JSON (default ./config.json, override with --config or
INHOUSE_TRIAGE_CONFIG). All fields optional — defaults assume the
Three-Tier Helpdesk@Home deployment.
"""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 8095,
    "tiers": {
        "l1": {
            "name": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
            "base_url": "http://localhost:8080/v1",
            "api_key": "local",
        },
        "l2": {
            "name": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "l3": {
            "name": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
    },
    "llm_tiebreaker": {
        "enabled": True,
        "base_url": "http://localhost:8080/v1",
        "model": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        "api_key": "local",
        "max_tokens": 64,
        "timeout_s": 45,
    },
    "audit_log": "/var/log/inhouse-triage/triage.log",
    "session_ttl_s": 3600,
    "max_body_bytes": 20 * 1024 * 1024,
    "upstream_timeout_s": 300,
    "triage_model_name": "triage",
}


class Config:
    def __init__(self, data: dict):
        self._d = data

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("INHOUSE_TRIAGE_CONFIG") or "config.json"
        data = json.loads(json.dumps(DEFAULTS))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
            _deep_merge(data, user)
        return cls(data)

    def get(self, key: str, default=None):
        return self._d.get(key, default)

    def tier(self, name: str) -> dict:
        t = self._d["tiers"].get(name) or {}
        cfg = dict(t)
        key = cfg.pop("api_key", None)
        key_env = cfg.pop("api_key_env", None)
        if key_env:
            cfg["api_key"] = os.environ.get(key_env, "")
        elif key:
            cfg["api_key"] = key
        return cfg

    def tier_names(self) -> dict[str, str]:
        return {name: t.get("name", name) for name, t in self._d["tiers"].items()}

    def known_model_ids(self) -> dict[str, str]:
        """Map upstream model ids (and the L1 file path alias) -> tier."""
        out: dict[str, str] = {}
        for tier, cfg in self._d["tiers"].items():
            out[cfg.get("name", "")] = tier
        l1 = self._d["tiers"].get("l1", {})
        name = l1.get("name", "")
        if name.startswith("/"):
            out[name.rsplit("/", 1)[-1]] = "l1"  # bare filename alias
        return out


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
