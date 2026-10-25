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

## The full escrow lifecycle

Adjudication is the middle of an escrow, not the whole of it. A deal here
runs the whole ring, driven by the two parties rather than an operator:

- **Terms before money.** A party proposes the deal (amount, item,
  delivery ZIP, window); the counterparty accepts or declines on their
  own page. Declined terms end the deal with no money moved; funding is
  impossible until both parties have accepted.
- **Buyer confirmation is a first-class release input.** The buyer can
  confirm receipt on their deal page; a confirmed receipt releases at
  Tier 0 alongside the checklist (the buyer is the address authority),
  and counts as delivery evidence when tracking has no delivery scan.
  Without confirmation, the evidence path stays authoritative.
- **Split settlement.** Not every deal is all-or-nothing: a partial
  shipment settles proportionally (capture the delivered fraction, void
  the rest), and a dispute the buyer agrees to close for a partial
  release settles at the agreed amount. Integer cents throughout;
  the partial-capture-then-void shape was proven against the live
  sandbox with PayPal read-backs for both legs.
- **Disputes carry evidence, not just a flag.** Parties attach evidence
  to an open dispute — seller tracking proof, buyer damage reports,
  settlement offers — on a visible timeline. Submitted evidence feeds
  the same checklist the carrier data does: a seller's pickup proof
  can rescue a deal headed for a stall refund; a buyer's damage
  report keeps a deal from releasing even after the dispute flag is
  resolved.
- **Party pages + notifications.** Each party gets their own deal page
  (status timeline, what happens next, only the actions that party may
  take — enforced server-side), distinct from the operator dashboard.
  Every state change writes a per-party notice to an append-only
  outbox, visible on the page.
- **PayPal webhooks, polling fallback.** A webhook listener applies
  `CHECKOUT.ORDER.APPROVED`, `PAYMENT.AUTHORIZATION.CREATED`,
  `PAYMENT.AUTHORIZATION.VOIDED`, and `PAYMENT.CAPTURE.COMPLETED`
  events idempotently (an approval completes the funding it was
  waiting on). The polling monitor remains the authoritative fallback
  if an event is missed. Signature verification is required in
  production and is deliberately not faked here — the sandbox listener
  is exercised with PayPal-shaped fixture payloads.

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
  deal_manager.py    The escrow state machine (two-party lifecycle)
  paypal_client.py   Sandbox client: authorize / capture / void
  checklist.py       The verification checklist array (the real IP)
  decision_engine.py Tiered adjudication (Tier 0 / shadow / Tier 2)
  laya_questions.py  The 8 atomic questions the forest answers
  laya_forest.py     Forest evaluation + shadow aggregation
  evidence.py        Evidence gatherers + scripted tracking simulator
  disputes.py        Party-submitted dispute evidence + timeline
  split_settlement.py Split execution: partial capture + void remainder
  webhooks.py        PayPal webhook listener (idempotent; polling fallback)
  notifications.py   Per-party notice outbox (append-only)
  auth_expiry.py     Authorization aging: flag at 20d, escalate at 25d
  simulator.py       Demo harness: 3 scripted deals end-to-end
  audit.py           Append-only audit log of every decision
  app.py             Operator dashboard + buyer/seller deal pages
tests/               204 tests: lifecycle, money path vs the live
                     sandbox, split settlement, webhooks, disputes,
                     party UI, golden corpus, hygiene + ladder gates
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

Week 4 of 5. The full lifecycle is built and tested end-to-end: two-party
terms, funding, buyer confirmation, monitoring, disputes with party
evidence, split settlement, webhooks, party pages and notifications —
204 tests passing, one documented skip (the buyer-login approval leg,
a PayPal platform constraint covered by a manual-mode test). All three
demo scenarios produce the correct verdict at $0 total AI spend.
Remaining: demo video and Devpost submission — see BUILD_PLAN.md.

## Limitations — stated plainly

**The money rail is a card hold, not production escrow.** What this
build does is a plain PayPal Orders v2 authorize/capture: the
buyer's card is authorized for the full amount and that authorization
is the "hold." PayPal fully honors an authorization for about 3
days; it expires at 29. The seller is paid by capturing the
authorization, the buyer is refunded by voiding it. That is a real
hold, but it is not how a production escrow platform settles:
production would capture the funds into the platform's account at
funding time and disburse with the Payouts API on release (refunding
the capture if the deal fails), which needs PayPal partner/escrow
arrangements and licensing this hackathon build doesn't have. The
adjudication core is deliberately rail-agnostic — checklist, tiers,
audit trail — so swapping the rail doesn't touch the judgment.
Authorization aging is surfaced, not silent: monitoring flags a
hold in the audit feed at 20 days and escalates it at 25, before
the 29-day expiry can quietly empty the escrow.

**Laya's blind spots are measured, not hypothetical.** On the
complexity ladder (22 cases, hostile inputs, three models per case):
the forest degrades with mixed-valence facts — when SHIPPED and
WITHIN_WINDOW are both NO it still draws YES answers in every
phrasing tried, so read on answers alone it can be fooled toward
release; raw numeric detail lines in its input (warehouse temps,
ticket counts) contaminate it at *any* size — presence, not
magnitude; and where a fact is genuinely UNKNOWN its forced guesses
lean YES roughly 9:3. Each blind spot has a named containment:
Tier 0 owns every hard fail (both-NO is a Tier 0 refund before the
forest is consulted), a hygiene-gate test pins the forest's input
to canonical atomic fact lines so numeric contamination can't creep
back in, fact-certainty gating means a guessed YES can never carry
a release vote, and the forest's verdict is shadow-logged only — it
can flag, it can never move money.

**The accuracy corpus is self-authored.** The fixtures, ladder
cases, and golden corpus (partial shipments, ZIP typos, carrier
exception strings, cent-level mismatches) are realistic but ours.
100% on our own cases is not a claim about the world; it's a claim
that the guardrails hold on the cases we could think of.

**Buyer approval cannot be automated.** PayPal offers no API to
approve an order as the buyer — approval is a human at a web page.
The end-to-end suite therefore funds its automated legs with
PayPal's sanctioned server-side card flow, and the buyer-approval
leg is covered by a manual-mode test (a human clicks approve; the
suite polls, completes, and verifies) rather than pretending the
step doesn't exist.

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Brian Acosta.
Third-party dependency licenses are inventoried in [NOTICE](NOTICE);
the project's own dependencies are permissive (Apache 2.0 / BSD / MIT —
laya, the Tier 1 sensor library, is itself Apache 2.0), with no copyleft
components.
