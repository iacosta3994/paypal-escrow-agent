# Escrow Agent — AI Referee for PayPal Escrow

An AI payment-operations agent that holds PayPal funds in escrow and releases
them only when it verifies the deal conditions were met. Built for the
[PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) (submissions
close Nov 12, 2026).

**Thesis: AI you can trust with money, because it shows its work — and because
every layer of it is only trusted as far as it was measured.**

## How it decides

PayPal's authorize-then-capture is a ready-made escrow primitive: authorize to
lock funds, capture to release, void to refund. The referee in the middle is
tiered, and the tiers are ordered by how much each one has *earned* trust:

- **Tier 0 — deterministic rules (authoritative, $0).** A checklist of
  verifiable conditions (picked up? delivered? right ZIP? amount matches?
  condition disputed?). Hard fails short-circuit to REFUND, decisive passes
  to RELEASE, condition disputes to INVESTIGATE. Most cases end here.
- **Tier 1 — Laya decision forest, in SHADOW mode (advisory only).** Small
  local neural networks each answer one atomic question about one fact
  ("was the item shipped?", "is the delivery window still open?"); a
  deterministic aggregator combines the answers. We benchmarked this forest
  as the judge first — it failed our adjudication checklist — so it was
  demoted to a sensor: its verdict is written to the audit log next to the
  real decision (`shadow_verdict`, `shadow_confidence`, `agreement`), and
  disagreement routes the case to INVESTIGATE. It can flag. It can never
  move money.
- **Tier 2 — Grok (xAI), reserved.** Deep reasoning for genuinely ambiguous,
  higher-value cases Tier 0 can't settle. Spend happens here only when
  ambiguity demands it.

Every decision — verdict, tier, evidence, reasoning, cost, and the shadow
sensor's opinion — lands in an append-only audit log.

## AI tools used

- **Laya** (PyPI `laya`, checkpoint `convaiinnovations/laya/typed-decisions`)
  — the Tier 1 shadow forest. Runs locally, $0 per decision. Question design
  and failure analysis: `src/laya_questions.py`.
- **Grok via xAI** — Tier 2 escalation reasoning (reserved; not spent on
  routine cases).
- **Muse** — development pair-programming: architecture review, test
  design, and the measurement harnesses that decided Laya's role.

## Project layout

```
src/
  config.py          Environment-based configuration
  models.py          Deal, CheckResult, Decision dataclasses
  paypal_client.py   Sandbox client: authorize / capture / void
  checklist.py       The verification checklist array (the real IP)
  decision_engine.py Tiered adjudication (Tier 0 / shadow / Tier 2)
  laya_questions.py  The 8 atomic questions the forest answers
  laya_forest.py     Forest evaluation + shadow aggregation
  evidence.py        Evidence gatherers + scripted tracking simulator
  simulator.py       Demo harness: 3 scripted deals end-to-end
  audit.py           Append-only audit log of every decision
  app.py             Dashboard (live feed, cost + shadow per decision)
tests/               Unit tests (22)
ARCHITECTURE.md      Full design doc
BUILD_PLAN.md        Week-by-week plan through Nov 12
DEMO_SCRIPT.md       The 3-scenario demo, < 3 minutes
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install laya        # enables the Tier 1 shadow sensor (optional;
                        # without it Tier 0 still decides every case)
cp .env.example .env    # PayPal sandbox keys (only needed for live
                        # authorize/capture/void, not the simulator)

python -m src.simulator clean_delivery   # happy path  -> RELEASE
python -m src.simulator never_ships      # stall       -> REFUND
python -m src.simulator ambiguous        # damaged     -> INVESTIGATE
python -m src.app                        # dashboard on :5000
python -m pytest tests/                  # or run the test files directly
```

## Status

Week 4 of 5. Escrow core, tiered decision engine, shadow sensor, simulator,
and dashboard are built and tested end-to-end (all three scenarios produce
the correct verdict at $0 total AI spend). Remaining: demo video and Devpost
submission — see BUILD_PLAN.md.
