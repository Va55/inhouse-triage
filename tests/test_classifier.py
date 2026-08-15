"""Unit tests for the ITIL triage classifier — deterministic cases."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inhouse_triage.classifier import (  # noqa: E402
    L1,
    L2,
    L3,
    classify_request,
    parse_tiebreaker,
    tiebreaker_prompt,
)


def test_simple_qna_is_l1():
    tier, score, _ = classify_request("What time is it in Sofia right now?")
    assert tier == L1
    assert score <= 0.30


def test_quick_rename_is_l1():
    tier, _, _ = classify_request("Rename the variable x to y in my script, quick fix please.")
    assert tier == L1


def test_short_translation_is_l1():
    tier, _, _ = classify_request("Translate this sentence to Bulgarian: hello world")
    assert tier == L1


def test_multifile_debug_is_l2():
    msg = (
        "My app crashes when I run main.py together with utils.py. "
        "Here is the traceback: File main.py line 42, in <module> ... "
        "File utils.py line 17, in parse_config ... KeyError: 'host'. "
        "The error log shows a segfault in the parser."
    )
    tier, score, signals = classify_request(msg)
    assert tier == L2
    assert any(s.startswith("multifile") for s in signals)
    assert "debug" in signals


def test_architectural_tradeoff_is_l2():
    msg = (
        "Should we use Docker or bare-metal services for the new monitoring stack? "
        "I need the trade-offs: scalability, reliability, latency, observability."
    )
    tier, _, signals = classify_request(msg)
    assert tier == L2
    assert "architecture" in signals


def test_three_domains_is_l2():
    msg = (
        "My API server can't reach the database over the network — "
        "nginx returns 502, postgres is up, firewall rules look fine."
    )
    tier, _, signals = classify_request(msg)
    assert tier == L2
    assert any(s.startswith("domains:") for s in signals)


def test_formal_proof_is_l3():
    msg = (
        "Prove that the deduplication algorithm is correct: derive the "
        "invariant and give a formal complexity bound for the merge step."
    )
    tier, score, _ = classify_request(msg)
    assert tier == L3
    assert score >= 0.75


def test_research_synthesis_is_l3():
    msg = (
        "Survey the recent literature on KV-cache compression for MoE models "
        "and compare the state-of-the-art methods with a research-grade analysis."
    )
    tier, _, _ = classify_request(msg)
    assert tier == L3


def test_urgent_impact_escalates():
    msg = "URGENT: production is down, data loss happening, everything is broken, fix it now."
    tier, _, signals = classify_request(msg)
    assert tier in (L2, L3)
    assert "urgent" in signals


def test_long_code_heavy_debug_is_l2():
    msg = (
        "Here is the code that fails:\n```python\nimport requests\n"
        "r = requests.get('http://localhost:9091/health')\n```\n"
        "It raises ConnectionError when the inhouse-api is starting. "
        "The log file shows a timeout. What is wrong and how do I fix it? "
    )
    tier, _, signals = classify_request(msg)
    assert tier == L2
    assert any(s.startswith("codeblocks") for s in signals)


def test_session_ladder_never_downgrades():
    tier, _, signals = classify_request(
        "What time is it?", session_tier=L3
    )
    assert tier == L3
    assert "session-ladder" in signals
    tier2, _, _ = classify_request("Rename x to y", session_tier=L2)
    assert tier2 == L2


def test_session_ladder_allows_l3_upgrade_from_l2():
    tier, _, _ = classify_request(
        "Prove the theorem about the halting problem", session_tier=L2
    )
    assert tier == L3


def test_tiebreaker_prompt_shape():
    msgs = tiebreaker_prompt("hello")
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_parse_tiebreaker():
    assert parse_tiebreaker('{"tier": "l2", "reason": "debugging"}') == "l2"
    assert parse_tiebreaker('{"tier":"l3"}') == "l3"
    assert parse_tiebreaker("tier: l1") == "l1"
    assert parse_tiebreaker("garbage") is None
    assert parse_tiebreaker("") is None


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{sum(1 for k in globals() if k.startswith('test_')) - failed}/{sum(1 for k in globals() if k.startswith('test_'))} passed")
    sys.exit(1 if failed else 0)
