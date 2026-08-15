# inhouse-triage

ITIL three-tier helpdesk router — an OpenAI-compatible triage proxy that
classifies every request and routes it to the right tier, automatically.

```
L1 (Service Desk)      Qwen3.6-35B-A3B-UD (local llama-server)   $0.00
L2 (Technical Support) deepseek-v4-flash                          ~$0.0003/ticket
L3 (Deep Support)      deepseek-v4-pro                            ~$0.026/ticket
```

Point any OpenAI-compatible client (Hermes, aichat, curl) at
`http://<host>:8095/v1` and the router does the ITIL triage for you.
Pure Python stdlib — zero dependencies.

## Why

Hermes' `fallback_providers` chain is availability failover (it fires on
connection errors / 5xx / 429 — not on task complexity). Real ITIL
escalation is a *triage decision*: the ticket is classified and routed by
complexity, impact, and urgency. This proxy implements that decision
deterministically, per request, with a full audit trail.

## Routing rules (in order)

1. `X-Triage-Override: l1|l2|l3` header — forced tier (manual override).
2. Explicit upstream model id in the request `model` field — direct route
   (e.g. `deepseek-v4-pro` goes straight to L3, no classification).
3. Anything else (`model: triage` or unknown) — classified:

   - Heuristic signals (weighted): formal/reasoning keywords, debugging &
     stack traces, architecture/trade-offs, multi-file paths, domain count
     (3+ => L2 per the article), impact/urgency words, request length,
     code blocks, simple-request negatives.
   - Score bands (raw): `<=0.5` L1, `0.5–2.2` LLM tiebreaker (if enabled)
     else L2, `>=2.2` L3.
   - Session ladder (ticket ownership): a session already escalated to
     L2/L3 never downgrades mid-ticket (`X-Session-Id` header).

Requests are relayed verbatim to the chosen upstream — body untouched
except the `model` field, which is rewritten to the upstream's model id
(DeepSeek validates it; llama-server ignores it). Streaming is passed
through as chunked SSE. Upstream errors pass through untouched so a
client-side fallback chain still works.

## Audit

Every request appends one JSON line to the audit log
(default `/var/log/inhouse-triage/triage.log`): timestamp, kind
(routed/direct/override), tier, score, signals, session, model, upstream
status, latency, tokens. Endpoints:

- `GET /health`            — status + configured tiers
- `GET /v1/models`         — upstream model list
- `GET /triage/stats`      — runtime counters
- `GET /triage/log?tail=N` — last N audit entries

## Development vs production

Convention for this project (llama-server as reference):

- **Development** lives in `$HOME` — clone the repo, run the tests, iterate
  (`git clone ... ~/inhouse-triage`).
- **Production** lives in `/opt` — `/opt/inhouse-triage` is a clone of the
  same repo, and the systemd unit references only `/opt` paths. `config.json`
  and `.env` are production-local and git-ignored (see `.gitignore`).
- **Shipping an update:** develop + test in `~/inhouse-triage` → push to
  your git remote → on the prod box: `cd /opt/inhouse-triage && git pull &&
  sudo systemctl restart inhouse-triage`.

## Install (deploy on the L1 box — /opt layout, llama-server as reference)

```bash
git clone https://github.com/Va55/inhouse-triage.git
sudo mv inhouse-triage /opt/inhouse-triage          # app lives in /opt, not $HOME
sudo chown -R $USER:$USER /opt/inhouse-triage
cp /opt/inhouse-triage/config.example.json /opt/inhouse-triage/config.json
# create /opt/inhouse-triage/.env (chmod 600, $USER:$USER) with: DEEPSEEK_API_KEY=...
sudo cp /opt/inhouse-triage/systemd/inhouse-triage.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now inhouse-triage
curl localhost:8095/health
```

All systemd-referenced paths live under /opt (ExecStart, WorkingDirectory,
EnvironmentFile) — mirroring the llama-server unit pattern. Audit log stays
in /var/log/inhouse-triage/triage.log.

Point Hermes at it:

```yaml
model:
  provider: custom
  default: triage              # any unknown name -> classified
  base_url: http://localhost:8095/v1
fallback_providers:            # stays as availability safety net
  - provider: deepseek
    model: deepseek-v4-flash
  - provider: deepseek
    model: deepseek-v4-pro
```

## Tests

```bash
python3 tests/test_classifier.py   # deterministic classifier cases
python3 tests/test_server.py       # router + fake upstreams + audit
```

## Known limitations

- "Two failed tool-call attempts" (article criterion) is agent-internal —
  the proxy cannot see tool failures; approximate via session ladder.
- Tiebreaker adds ~1–3 s latency in the middle band (local, free).
- Session state is in-memory (lost on restart); the audit JSONL is the
  durable record.
