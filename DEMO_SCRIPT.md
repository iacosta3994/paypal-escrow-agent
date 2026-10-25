# Demo Script — 3 scenarios, < 3 minutes

Thesis: **AI you can trust with money, because it shows its work.**

## Scenario 1 — Clean release (the happy path)
- Deal: $500 used camera, buyer + seller, release on delivery confirmation.
- Terms before money: the seller proposes the deal; the buyer accepts
  on their own deal page. Until both parties have accepted, funding
  doesn't exist — show the fund action refused/pending pre-acceptance.
- Funding pauses at AWAITING_APPROVAL: PayPal has no API to approve as
  the buyer, so the buyer approves on PayPal's own page — a human
  authorizes every hold; say so out loud, it's a trust beat, not a gap.
  (In the live system a PayPal webhook — CHECKOUT.ORDER.APPROVED —
  completes the funding the moment the approval lands.)
- PayPal authorization locks $500 (show the hold on screen). Each
  party's page shows the state change and a notice lands in the
  outbox — the parties watch the same lifecycle the operator does.
- Simulated tracking: label created → picked up → in transit → delivered,
  ZIP matches.
- Day 1, still in transit: the rules can't settle it yet — **INVESTIGATE**,
  keep monitoring. The shadow sensor's advisory verdict is logged next to
  the decision (watch the dashboard's shadow column).
- Day 3, delivered, ZIP matches: checklist all pass, Tier 0 decision.
- **Verdict: RELEASE.** Show the reasoning + evidence + cost ($0.00 —
  the deterministic tier is free).
- Capture executes; seller gets $500.
- Buyer-confirmation beat (say it in one line): had the tracking been
  silent, the buyer's own "confirm receipt" on their deal page would
  release this too — the buyer is the address authority. Confirmation
  is a first-class input, not a comment box.

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
- Resolution beat (the lifecycle closing): the buyer opens a dispute
  on their deal page and attaches their damage report as evidence;
  the seller's evidence appears on the same timeline. The buyer
  accepts a split — keep the laptop, release $900 of the $1,200 —
  and the system executes exactly that: capture $900, void $300,
  deal SPLIT_SETTLED, both parties notified. If the buyer accepts
  nothing, the INVESTIGATE above is where it stays — a human decides.
  (A partial shipment settles the same way, proportionally, without
  a dispute: capture the delivered fraction, void the rest.)
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
