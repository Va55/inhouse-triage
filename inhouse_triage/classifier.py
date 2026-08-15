"""ITIL-style triage classifier for the Three-Tier Helpdesk@Home.

Maps the article's escalation criteria onto deterministic request signals.

  L1 (Service Desk)       — simple Q&A, routine coding, document tinkering,
                            home-automation chores. Default: first-contact
                            resolution, $0.00.
  L2 (Technical Support)  — 3+ domains in play, multi-file debugging,
                            architectural trade-offs, survived failures.
  L3 (Deep Technical)     — formal reasoning, research-grade synthesis,
                            long-horizon / 1M-context-scale problems.

Scoring: weighted signals -> raw score (0 = trivially simple, 3+ = hard).
Decision thresholds:

  raw <= 0.5            -> L1
  0.5 < raw < 2.2       -> LLM tiebreaker (if enabled) else L2
  raw >= 2.2            -> L3

Pure functions only — no I/O — so the classifier is fully unit-testable
and deterministic for a given input (audit-friendly).
"""

from __future__ import annotations

import re

# ── Signal definitions ──────────────────────────────────────────────────────

# Strong signals: formal reasoning / research-grade / long-horizon (L3-leaning)
_STRONG_RE = re.compile(
    r"\b(proof|prove|theorem|axiom|derive|derivation|deduction|correctness|"
    r"formal\s*(method|verification)|research|survey|literature|"
    r"state[\s-]of[\s-]the[\s-]art|exploit|vulnerability\s*analysis|"
    r"threat\s*model|reverse\s*engineer|fuzz|symbolic|solver|complexity\s*"
    r"(bound|analysis)|halting|decidab|formalization|paper(s)?)\b",
    re.IGNORECASE,
)

# Medium signals: debugging / architecture / multi-file (L2-leaning)
_DEBUG_RE = re.compile(
    r"\b(debug|traceback|stack\s*trace|segfault|crash(ed|es|ing)?|hangs?|"
    r"deadlock|race\s*condition|not\s*working|doesn'?t\s*work|broken|"
    r"error\s*log|log\s*file|exception|core\s*dump|blue\s*screen|kernel\s*"
    r"panic|timeout|oom|out\s*of\s*memory|memory\s*leak|corrupt)\b",
    re.IGNORECASE,
)
_ARCH_RE = re.compile(
    r"\b(architecture|architectural|design\s*decision|trade-?offs?|refactor|"
    r"migrat|scalab|concurr|parallel|distributed|performance\s*bottleneck|"
    r"latency|throughput|reliab|high[\s-]availab|monitoring|observability|"
    r"capacity\s*plann)\w*\b",
    re.IGNORECASE,
)
_FILEPATH_RE = re.compile(
    r"(?:^|[\s(/])([\w./-]+\.(?:py|js|ts|go|rs|c|cpp|h|hpp|java|kt|sh|bash|"
    r"yaml|yml|json|toml|ini|cfg|conf|md|sql|db|log|service|unit|conf))"
)
_MULTIFILE_RE = re.compile(
    r"\b(multi[\s-]?file|several\s*files|across\s+the\s+(repo|codebase)|"
    r"in\s+multiple\s+(files|modules)|all\s+(my|the)\s+.*\bfiles)\b",
    re.IGNORECASE,
)

# Impact / urgency (ITIL escalation driver) — escalates regardless of genre
_URGENT_RE = re.compile(
    r"\b(urgent|asap|immediately?|critical|production\s*(is|down)|outage|"
    r"data\s*loss|blocked|can'?t\s+work|down\s+right\s+now|everything\s*"
    r"(is|broken)|emergency|p0|sev\s*[123])\b",
    re.IGNORECASE,
)

# Domain vocabulary — count distinct domains for the "3+ domains" criterion
_DOMAINS = {
    "network": r"\b(network|routing|vlan|dns|dhcp|firewall|nat|vpn|subnet|"
               r"wifi|ethernet|ip\s*address|tcp|udp|port\s*forward)",
    "database": r"\b(database|sql|postgres|mysql|sqlite|redis|mongo|schema|"
                r"query|index|migration|backup|restore)",
    "security": r"\b(security|auth|password|certificate|tls|ssl|encrypt|"
                r"decrypt|key\s*pair|ssh|permission|sudo|sandbox)",
    "web": r"\b(web|http|https|api|rest|endpoint|nginx|cors|websocket|"
           r"html|css|javascript|frontend|backend)",
    "gpu": r"\b(gpu|cuda|cudnn|tensor|vram|nvidia|driver|llama|gguf|"
           r"inference|model\s*loading|token)",
    "kernel": r"\b(kernel|driver|module|device\s*node|dmesg|firmware|"
              r"pcie|usb|bluetooth)",
    "os": r"\b(linux|debian|freebsd|windows|systemd|boot|fstab|zfs|btrfs|"
          r"ext4|mount|partition|grub)",
    "containers": r"\b(docker|container|lxc|podman|k8s|kubernetes|image\b|"
                  r"compose|volume)",
    "audio": r"\b(audio|sound|alsa|pulse|pipewire|jack|mic|speaker|codec)",
    "automation": r"\b(automation|cron|script|scheduled|watchdog|daemon|"
                  r"systemd\s*unit|service\s*file)",
}
_DOMAIN_RES = {name: re.compile(pat, re.IGNORECASE) for name, pat in _DOMAINS.items()}

# Simple-request signals (negative weight — FCR-friendly)
_SIMPLE_RE = re.compile(
    r"\b(quick|simple|simply|just\s+(rename|change|fix\s*the\s*typo)|"
    r"typo|short\s*answer|briefly|in\s*one\s*sentence|translate|what\s*is|"
    r"who\s*is|when\s*is|spell|format|convert)\b",
    re.IGNORECASE,
)

_CODE_FENCE_RE = re.compile(r"```")

# ── Public API ──────────────────────────────────────────────────────────────

#: Tier ids
L1 = "l1"
L2 = "l2"
L3 = "l3"


def classify_request(
    user_text: str,
    *,
    message_count: int = 1,
    session_tier: str | None = None,
) -> tuple[str, float, list[str]]:
    """Classify a request into a tier.

    Returns (tier, score, hit_signals). ``session_tier`` implements ticket
    ownership: an already-escalated session never downgrades mid-ticket.
    """
    text = user_text or ""
    hits: list[str] = []
    raw = 0.0

    # Strong signals (L3-leaning) — accumulate per distinct keyword, capped
    strong_matches = _STRONG_RE.findall(text)
    if strong_matches:
        raw += min(1.4 * len(set(strong_matches)), 2.8)
        hits.append(f"strong:{len(set(strong_matches))}")
    # Debugging / errors
    if _DEBUG_RE.search(text):
        raw += 1.0
        hits.append("debug")
    # Architecture / design trade-offs
    if _ARCH_RE.search(text):
        raw += 1.0
        hits.append("architecture")
    # Multi-file
    file_paths = set(_FILEPATH_RE.findall(text))
    if len(file_paths) >= 2 or _MULTIFILE_RE.search(text):
        raw += 1.0
        hits.append(f"multifile:{len(file_paths)}")
    elif len(file_paths) == 1:
        raw += 0.3
        hits.append("singlefile")
    # Domains (3+ => L2 per the article)
    domains = [name for name, rx in _DOMAIN_RES.items() if rx.search(text)]
    if len(domains) >= 3:
        raw += 1.0
        hits.append(f"domains:{len(domains)}")
    elif len(domains) == 2:
        raw += 0.4
        hits.append(f"domains:{len(domains)}")
    # Impact / urgency
    if _URGENT_RE.search(text):
        raw += 1.2
        hits.append("urgent")
    # Length (long-horizon proxy)
    words = len(text.split())
    if words > 800:
        raw += 1.0
        hits.append(f"long:{words}")
    elif words > 300:
        raw += 0.5
        hits.append(f"long:{words}")
    # Code blocks
    fences = len(_CODE_FENCE_RE.findall(text)) // 2
    if fences:
        raw += min(0.3 * fences, 1.0)
        hits.append(f"codeblocks:{fences}")
    # Simple-request negatives
    if _SIMPLE_RE.search(text):
        raw -= 0.6
        hits.append("simple")

    raw = max(0.0, raw)
    score = round(raw, 3)

    tier = _decide(score)
    if session_tier in (L2, L3):
        # Ticket ownership: never downgrade mid-ticket.
        if tier != L3 and session_tier == L3:
            tier = L3
            hits.append("session-ladder")
        elif session_tier == L2 and tier == L1:
            tier = L2
            hits.append("session-ladder")
    return tier, score, hits


def _decide(raw: float) -> str:
    if raw <= 0.5:
        return L1
    if raw < 2.2:
        # Middle band — caller may consult the LLM tiebreaker; this is the
        # conservative default (pennies, not seconds).
        return L2
    return L3


# ── LLM tiebreaker (optional, local, free) ──────────────────────────────────

_TIEBREAK_SYSTEM = (
    "You are the triage agent of an ITIL-style three-tier helpdesk. "
    "Classify the user's request into exactly one tier:\n"
    "l1 = simple question, routine task, quick fix, no debugging needed\n"
    "l2 = multi-domain, debugging, architectural decision, several files\n"
    "l3 = formal reasoning, proof, research-grade synthesis, long-horizon plan\n"
    'Reply with only a JSON object: {"tier": "l1|l2|l3", "reason": "<short>"}'
)


def tiebreaker_prompt(user_text: str) -> list[dict]:
    """Build the chat payload for the local LLM tiebreaker."""
    return [
        {"role": "system", "content": _TIEBREAK_SYSTEM},
        {"role": "user", "content": user_text[:6000]},
    ]


def parse_tiebreaker(text: str) -> str | None:
    """Extract l1/l2/l3 from the tiebreaker's reply. None on failure."""
    if not text:
        return None
    m = re.search(r'"(tier)"\s*:\s*"?(l[123])"?', text)
    if m:
        return m.group(2).lower()
    m = re.search(r"\b(l[123])\b", text)
    return m.group(1).lower() if m else None
