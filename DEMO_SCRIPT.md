# Demo Script — project escrow first, < 3 minutes

Thesis: **AI you can trust with money, because it shows its work.**

The headline use case is **project escrow**: a client pays for work —
a build, a deliverable, a milestone — and the money moves only when
the delivered work is verified against the requirements the client
froze before funding. Physical-goods escrow runs on the same kernel
and appears as a side example, not the pitch.

This script matches the accepted final cut
(demo-video-v2-fixed2-2026-10-29.mp4, 2:32) and doubles as the live
walkthrough script. Beats marked **[walkthrough]** are presenter
lines for the live demo and Q&A; they are not in the recorded cut.

## What the demo proves (the judge's map)

- **PayPal moves the money, for real.** Authorize / capture / void
  and partial capture on the sandbox rail; a split settlement
  ($150 captured, remainder voided) and a full $300 authorize →
  capture were proven live on 2026-10-25, read back on PayPal's
  side. Buyer approval is a human click on PayPal's own page —
  there is no API for it, and the script says so out loud.
- **AI is used where it is trusted, and only there.** A
  deterministic Tier 0 kernel owns every hard call; a local shadow
  sensor advises and can never move money; an AI layer reviews the
  documents on hand for genuinely ambiguous cases and escalates to
  a human if still uncertain.
- **The requirements list is the agreement and the audit.** One
  frozen, versioned list — the same list at funding and at
  grading. Every verdict cites it, item by item, with evidence.
- **Settlement follows measurement.** A delivery measuring 40% of
  its list settles its milestone at 40%. 90%+ releases in full;
  under 20% refunds. Nothing settles on a vibe.

## Demo links (share-link identity)

The dashboard is at `/`. Every party page is a share link —
`/deal/<id>?party=<buyer|seller>&k=<token>` — and holding the link
IS being that party (demo-grade identity; see README limitations).
The seeded deals use stable tokens so these links never churn:
buyer `demo-link-buyer`, seller `demo-link-seller`. Deals proposed
live in the UI get random tokens; the proposer lands on their own
link with the counterparty's link shown, ready to send. For a live
walkthrough: propose a project deal from `/propose`, send the
counterparty link to a second browser, accept there, fund, then
work the milestones from both sides.

## Hook (0:00–0:17, in the cut)

"In agentic commerce, the hardest question isn't what to buy. It's
when to let the money go." An escrow agent that shows its work:
every verdict with its evidence and its cost, on one dashboard.

## Beat A — Project escrow: graded against the agreement (0:17–0:53)

- A project is an agreement: the client and the builder freeze the
  **requirements list** before any money moves. That list is the
  contract and the audit checklist in one artifact. The deal is
  funded in milestone shares, each share tied to requirements on
  the list.
- Funding pauses at AWAITING_APPROVAL: PayPal has no API to
  approve as the buyer, so the client approves on PayPal's own
  page — a human authorizes every hold; say so out loud, it's a
  trust beat, not a gap. (A PayPal webhook — CHECKOUT.ORDER.APPROVED —
  completes the funding the moment the approval lands. For this
  demo, the approval beat stands on the proven 2026-10-25 sandbox
  record: order approved → authorization → capture/void, read back
  on PayPal's side.)
- The builder delivers a repo. The grader checks **every
  requirement** against what actually shipped — modules present,
  functions real (AST-verified, not stubs), the test suite, the
  docs — and returns each item with its evidence.
- This delivery measures **40.0%** of the list, so its milestone
  settles at forty percent: **SPLIT at the measured fraction** —
  partial capture, remainder voided. Ninety or better would
  release in full. Under twenty refunds. The client never pays
  for the 60% that didn't ship; the builder is never stiffed for
  the 40% that did.

## Beat B — Won't guess: the decisive unknown (0:53–1:15)

- A second delivery measures **70%** — but its decisive item was
  never verified, and that single unknown is the one that decides
  release versus split.
- **Verdict: INVESTIGATE.** An AI layer reviews the documents on
  hand for the case; if it is still uncertain, the case escalates
  to a human, evidence attached, while the money waits. No
  automatic release, no automatic refund on an unverified
  decisive item — the system refuses to guess with someone else's
  money.

## Beat C — Milestones settle independently (1:15–1:42)

- The milestone mechanics, shown on a $1,200 laptop deal:
  **delivery $900** + **condition verified $300**. Delivered scan
  present, ZIP matches — but the carrier reports the package
  visibly damaged, so condition is disputed.
- The delivery milestone verifies on its own checks and releases
  its $900 share. The condition milestone's checklist reads
  condition CONFLICT: **INVESTIGATE** from day 1.
- Resolution beat: the buyer disputes the condition milestone on
  their deal page and attaches their damage report; the seller's
  evidence appears under the same milestone. The parties accept a
  split of the $300 share ($150/$150); the system executes exactly
  that and the deal closes **SPLIT_SETTLED — $1,050 captured /
  $150 voided**, both parties notified. **The settled delivery
  share is never unwound by the open question.** If they accept
  nothing, INVESTIGATE is where it stays — a human decides.
  (Project deals settle the same way per milestone: a failed
  milestone refunds its share without touching the shares that
  already settled.)

## Beat D — Goods: the same kernel on parcels (1:42–2:04)

- **Camera — clean release.** $500 used camera. Terms before
  money: the seller proposes, the buyer accepts on their own deal
  page; until both have accepted, funding doesn't exist. PayPal
  authorization locks $500. Day 1, in transit: **INVESTIGATE**.
  Day 3, delivered, ZIP matches: Tier 0 — **RELEASE**, reasoning,
  evidence, and cost on screen ($0.00 — the deterministic tier is
  free). Capture executes; seller gets $500.
- **Keyboard — never ships, refund.** $300. Label created, then
  nothing. Day 6: the pickup check FAILS its stall threshold —
  **REFUND**, "no carrier pickup in 7 days." Void executes; the
  buyer is refunded.
- Transition line: "Different evidence. Same discipline."

## Close — the shadow tier (2:04–2:32, say it plainly)

"We also run a small local model on every case the rules can't
settle. We benchmarked it as a judge first — it failed our
adjudication checklist. So we demoted it to a sensor. Its verdict
is written into the audit log next to the real decision, and if
it ever disagrees, the case escalates to a human. It can flag.
It can never move money. That's the whole pitch: every layer of
this system is only trusted as far as it was measured."

Measured, not asserted: on a frozen 570-question hostile ladder
the sensor holds 0.947 pooled accuracy, wired in as a regression
gate — it has already caught one of our own changes regressing
it and sent it back.

## Dashboard throughout

Live deal states with their agreed completion dates, the event
feed, grade results, and **cost-per-decision** next to every
verdict — most verdicts read $0.00. The unit economics are part
of the pitch. One quiet line if asked about the rail: an
authorization-expiry watch flags an aging hold at 20 days and
escalates at 25, because PayPal expires authorizations at 29 —
an escrow must never discover an empty hold at day 30.
