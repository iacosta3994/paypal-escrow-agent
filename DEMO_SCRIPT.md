# Demo Script — 3 scenarios, < 3 minutes

Thesis: **AI you can trust with money, because it shows its work.**

## Scenario 1 — Clean release (the happy path)
- Deal: $500 used camera, buyer + seller, release on delivery confirmation.
- PayPal authorization locks $500 (show the hold on screen).
- Simulated tracking: label created → picked up → in transit → delivered,
  ZIP matches.
- Checklist: all pass. Tier 0/1 decision.
- **Verdict: RELEASE.** Show the reasoning + evidence + cost ($0.004).
- Capture executes; seller gets $500.

## Scenario 2 — Never ships (the refund)
- Deal: $300 mechanical keyboard. Authorization locks $300.
- Tracking: label created… then nothing. 7 days, no pickup scan.
- Checklist: pickup check FAILS, stall threshold breached.
- **Verdict: REFUND.** Show the reasoning ("no carrier pickup in 7 days").
- Void executes; buyer refunded.

## Scenario 3 — Ambiguous (the escalation)
- Deal: $1,200 laptop. Delivered scan present — but buyer's message says
  "box arrived dented, not sure it's the right model," and the delivery
  photo is inconclusive.
- Checklist: delivery PASS, condition UNKNOWN, buyer signal CONFLICT.
- **Verdict: INVESTIGATE.** AI refuses to guess — presents evidence to both
  parties with a recommendation and a human decision window.
- This is the trust moment: the AI knows what it doesn't know.

## Dashboard during all three
Live deal states, event feed, and **cost-per-decision** next to every
verdict. The unit economics are part of the pitch.
