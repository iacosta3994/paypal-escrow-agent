# Demo Script — 3 scenarios, < 3 minutes

Thesis: **AI you can trust with money, because it shows its work.**

## Scenario 1 — Clean release (the happy path)
- Deal: $500 used camera, buyer + seller, release on delivery confirmation.
- PayPal authorization locks $500 (show the hold on screen).
- Simulated tracking: label created → picked up → in transit → delivered,
  ZIP matches.
- Day 1, still in transit: the rules can't settle it yet — **INVESTIGATE**,
  keep monitoring. The shadow sensor's advisory verdict is logged next to
  the decision (watch the dashboard's shadow column).
- Day 3, delivered, ZIP matches: checklist all pass, Tier 0 decision.
- **Verdict: RELEASE.** Show the reasoning + evidence + cost ($0.00 —
  the deterministic tier is free).
- Capture executes; seller gets $500.

## Scenario 2 — Never ships (the refund)
- Deal: $300 mechanical keyboard. Authorization locks $300.
- Tracking: label created… then nothing. Days pass, no pickup scan.
- Days 1–3: **INVESTIGATE** (shadow sensor agrees, also logged) while the
  stall clock runs.
- Day 6: pickup check FAILS, stall threshold breached, Tier 0 hard fail.
- **Verdict: REFUND.** Show the reasoning ("no carrier pickup in 7 days").
- Void executes; buyer refunded.

## Scenario 3 — Ambiguous (the escalation)
- Deal: $1,200 laptop. Delivered scan present, ZIP matches — but the
  carrier reports the package visibly damaged, so condition is disputed.
- Checklist: delivery PASS, condition CONFLICT.
- **Verdict: INVESTIGATE** — from day 1 and every day after. The system
  refuses to guess: no automatic release, no automatic refund on a
  condition dispute. It escalates for a human with the evidence attached.
- This is the trust moment: the AI knows what it doesn't know.
- **The shadow tier (say it plainly):** "We also run a small local model
  on every case the rules can't settle. We benchmarked it as a judge
  first — it failed our adjudication checklist. So we demoted it to a
  sensor. Its verdict is written into the audit log next to the real
  decision, and if it ever disagrees, the case escalates to a human. It
  can flag. It can never move money. That's the whole pitch: every layer
  of this system is only trusted as far as it was measured."

## Dashboard during all three
Live deal states, event feed, and **cost-per-decision** next to every
verdict. The unit economics are part of the pitch.
