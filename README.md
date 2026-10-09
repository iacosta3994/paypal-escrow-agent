# Escrow Agent — AI Referee for PayPal Escrow

An AI payment-operations agent that holds PayPal funds in escrow and releases
them only when it verifies the deal conditions were met. Built for the
[PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) (submissions
close Nov 12, 2026).

## The idea in one paragraph

PayPal's authorize-then-capture is a ready-made escrow primitive: authorize to
lock funds, capture to release, void to refund. This project puts an AI referee
in the middle. It runs a checklist of verifiable conditions (tracking
confirmed? delivered to the right address? within the window?), feeds the
results to a single adjudication prompt, and returns a three-way verdict:
**RELEASE**, **REFUND**, or **INVESTIGATE**. Cut-and-dry cases cost fractions of
a cent; only ambiguous cases get deep (expensive) reasoning. Every decision is
auditable — the AI shows its work.

## Project layout

```
src/
  config.py          Environment-based configuration
  models.py          Deal, CheckResult, Decision dataclasses
  paypal_client.py   Sandbox client: authorize / capture / void
  checklist.py       The verification checklist array (the real IP)
  decision_engine.py Single-prompt adjudication: RELEASE / REFUND / INVESTIGATE
  evidence.py        Evidence gatherers (tracking, messages, deliverables)
  simulator.py       Fake deals + simulated tracking events for the demo
  audit.py           Append-only audit log of every decision
  app.py             Dashboard (deal states, live feed, cost-per-decision)
tests/               Unit tests
ARCHITECTURE.md      Full design doc
BUILD_PLAN.md        Week-by-week plan through Nov 12
DEMO_SCRIPT.md       The 3-scenario cinematic demo
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in PayPal sandbox + LLM keys
python -m src.simulator  # run the demo harness
```

## Status

Week 1 scaffold. PayPal sandbox wiring and the decision engine are the first
milestones — see BUILD_PLAN.md.
