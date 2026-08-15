# MOTIVATION — why inhouse-triage exists

*Written August 15, 2026, the day the idea went from blog post to running code.*

---

## 1. The origin: an email with no numbers

On August 6, 2026, DeepSeek warned its users that a significant API price
increase was coming. No numbers, no date — just a notice to plan usage
accordingly. The natural homelab question: *which requests deserve the
metered specialist at all?*

The answer we reached was the oldest structure in enterprise IT: the
three-tier escalation ladder — Service Desk, Technical Support, Deep
Technical Support — mapped onto a locally running model and two API tiers:

| Tier | Role | Model | Cost per query |
|---|---|---|---|
| L1 | Service Desk | Qwen3.6-35B-A3B-UD-Q4_K_XL (local llama-server) | $0.00 |
| L2 | Technical Support | DeepSeek V4-Flash | ~$0.0003 |
| L3 | Deep Technical Support | DeepSeek V4-Pro | ~$0.026 |

At 1,000 queries/month with an honest 80/18/2 split that is $0.57/month —
and the $33.89 DeepSeek balance covers roughly five years of it (the
article's "decade" was optimistic by a factor of two; we fixed the math).

## 2. The first implementation: Hermes on the P40 box

We installed Hermes v0.20.1 on the P40 box and wired the
ladder exactly as the article's config block proposed:

- **L1 (default):** local 35B via `provider: custom`, `base_url: localhost:8080`
- **fallback_providers:** `deepseek-v4-flash` → `deepseek-v4-pro`
- **aliases `l2` / `l3`** for in-session escalation
- **`helpdesk-escalation` skill** carrying the ITIL policy (resolve first,
  escalate on documented criteria, hand over a summary)

That part was real. A smoke test proved L1 answers from the local model
(verified in llama-server's own timing logs), and `hermes fallback list`
showed the chain.

## 3. The deep audit: what "escalation" actually meant

We then audited the running system the way a support desk would be audited —
by reading the source of the fallback machinery and by injecting real
failures.

**What worked (proven by failure injection):**

- L1 down → `Fallback activated: dead-l1 → deepseek-v4-flash (deepseek)` →
  answered, ~21.5K-token context replayed at 100% cache hit. L2 leg works.
- L1 and L2 both down → `Fallback activated: dead-l2 → deepseek-v4-pro
  (deepseek)` → answered. L3 leg works.
- Manual escalation works: `/model l2` → "✓ Model switched:
  deepseek-v4-flash", full conversation context carried over, `/model
  default` returns to L1.

**What didn't work — the honest gap:**

The fallback chain is **availability failover, not complexity triage**.
Reading `agent/error_classifier.py` and `chat_completion_helpers.py`, the
triggers are: transport errors (connection/timeout), 5xx/503/529, 429 +
billing, 401 after credential rotation fails, model-not-found (404),
content-policy blocks. It fires on *the model being broken*, never on *the
ticket being hard*.

Three consequences, each a direct contradiction of the article's promises:

1. **Context overflow does NOT escalate.** When L1's context fills, Hermes
   compresses the conversation and retries the *same model* — the specialist
   with the 1M-token window is never consulted. The one case where the L2
   tier exists precisely to help is the one case that never reaches it.
2. **"Escalate after two failed tool-call attempts" is not automatic.**
   Tool failures are agent-internal; the transport layer never sees them.
3. **No hand-over summary.** Automatic failover replays the raw ticket; the
   ITIL "structured hand-over" exists only as a skill instruction and a
   manual `/model` switch.

Verdict of the audit: the availability ladder was real, the ITIL triage was
a narrative. The honest maximum of a stock agent framework is policy +
manual switching — and we said so.

## 4. The decision: build the triage the article promised

If the framework won't triage, the *infrastructure* will. So we built
`inhouse-triage`: an OpenAI-compatible router that sits in front of the
models and makes the escalation decision itself, per request, before any
model is touched.

Any client (Hermes, aichat, curl) points at `http://<host>:8095/v1`. The
router decides, then relays the request **verbatim** to the chosen upstream
— body untouched, streaming passed through as chunked SSE, upstream errors
passed through untouched so a client-side fallback chain still works as the
safety net underneath.

**Routing rules, in order:**

1. `X-Triage-Override: l1|l2|l3` header — forced tier (manual override,
   the `/model` equivalent for automation).
2. Explicit upstream model id in the request — direct route
   (`deepseek-v4-pro` goes straight to L3, no classification, no latency).
3. Anything else — classified:

   - **Heuristic signals** mapped 1:1 to the article's escalation criteria:
     formal/reasoning keywords (L3), debugging & stack traces (L2),
     architecture/trade-offs (L2), multi-file paths (L2), domain count
     (3+ domains => L2, straight from the article), impact/urgency words
     (ITIL's other escalation driver), request length, code blocks, and
     simple-request negatives that keep FCR high.
   - **Score bands** (raw): `<=0.5` L1, `0.5–2.2` middle band, `>=2.2` L3.
     The middle band consults an optional **LLM tiebreaker** — the local 35B
     itself, free, with a strict JSON contract — else defaults conservatively
     to L2 (pennies, not minutes).
   - **Session ladder** (`X-Session-Id`): ticket ownership, the ITIL rule
     that once a ticket is escalated it stays with the specialist. An
     escalated session never downgrades mid-ticket. (This is also our
     approximation of "survived two failed attempts": if the specialist had
     to take the ticket once, it keeps it.)

**The audit trail** is the point, not a side effect. Every request appends
one JSON line: timestamp, kind (routed/direct/override), tier, score,
signals, session, model, upstream status, latency, tokens. `GET
/triage/stats` for live counters, `GET /triage/log?tail=N` for the ledger.
Decisions are deterministic, replayable, and explainable — the same
property that makes a support desk auditable makes this one auditable.

## 5. Verification

- `tests/test_classifier.py` — 14/14: simple Q&A → L1, quick rename → L1,
  multi-file debug → L2, architectural trade-off → L2, 3 domains → L2,
  formal proof → L3, research synthesis → L3, urgent impact escalates,
  session ladder never downgrades (but allows upgrades), tiebreaker parsing.
- `tests/test_server.py` — 17/17 with fake upstreams: simple → L1,
  debug → L2, formal → L3, direct model-id routing, override header,
  session ladder end-to-end, streaming passthrough, models/health/stats
  endpoints, audit content (kinds, scores, session history), dead-upstream
  502 passthrough + error counting.

## 6. Honest limitations

- **"Two failed tool-call attempts" is still invisible to us.** Tool
  failures are agent-internal; the session ladder is the proxy, not the
  real thing.
- **The tiebreaker adds ~1–3 s** in the middle band (local, free — a
  support desk that thinks for two seconds is acceptable).
- **Session state is in-memory** — lost on restart. The audit JSONL is the
  durable record.
- **The 80% FCR figure is a target**, not yet a measurement. The audit log
  is what will eventually let us measure it.

## 7. Why it still matters the day after

On August 16, 2026, DeepSeek moves both models to peak/off-peak pricing —
every tier above today's flat rates (input up ~2x blended, cache reads up
~7.84x). The monthly ledger moves from $0.57 to roughly $1–2. The
architecture absorbs it, because the point was never the pennies: it's that
80% of queries never leave the network, and the decision about which 20%
leave is now made by a deterministic, auditable rule — not by a transport
error.

---

*P.S. The L2 and L3 models had a hand in the article that started this. The
triage router is entirely local, deterministic, and stdlib-only — because
the one thing a helpdesk must never do is ask the specialist to decide
whether it needs the specialist.*
