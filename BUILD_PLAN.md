# Build Plan — Oct 8 → Nov 12

Submission deadline: **Nov 12, 2026, 12:00pm PT** (2pm CT). Target internal
freeze Nov 10 for buffer.

## Week 1 — Foundation (Oct 8–14)
- [x] PayPal developer account + sandbox app (client ID/secret)
- [x] `paypal_client.py`: authorize, capture, void against sandbox — tested live
- [x] Repo scaffold, `.env`, requirements, this plan
- [x] Deal data model (`models.py`) finalized

## Week 2 — Escrow core (Oct 15–21) — light week, Gateway FurMeet Oct 16–18
- [x] Deal lifecycle state machine: PROPOSED → FUNDED → MONITORING → DECIDED
- [x] Checklist array v1 (`checklist.py`) with stub evidence
- [x] Rough dashboard: deal list + states

## Week 3 — AI layer (Oct 22–28)
- [x] Evidence gatherers: tracking sim (real carrier API still stretch)
- [x] Decision engine: tiered prompts, 3-way verdict, confidence scoring
- [x] Audit log: every decision with evidence + reasoning + cost
      (+ shadow_verdict / shadow_confidence / agreement per decision)

> **Laya's role — settled 2026-10-23 (Ian, Option A: shadow mode).**
> Tier 0 is authoritative. The Laya forest was measured as a judge and
> failed the adjudication checklist (2026-10-09), so it was demoted to a
> sensor: deterministic aggregation of tree answers replaces its
> meta-decider, it runs on every case Tier 0 doesn't settle, its verdict
> is written to the audit log next to the real decision, and
> disagreement routes to INVESTIGATE — it can escalate, never override.
> Grok (Tier 2) stays reserved for hard cases (still blocked on xAI
> credits — separate issue). Decision brief:
> `goals/paypal-hackathon-escrow-agent/files/laya-role-decision-brief-2026-10-23.md`

> **§4 pipeline fix — applied 2026-10-24 (Ian approved).** The complexity
> ladder found `evaluate_tree` re-parsing rendered state-block text to
> rebuild the `_facts` dict used for fact-certainty gating; renamed keys
> or decorated values silently missed, so perfectly-read cases landed on
> INVESTIGATE. `shadow()` now passes the `build_state()` facts dict
> through to the forest and gating reads it directly (text parse kept as
> a fallback for callers without a facts dict). Verified: tests 22/22;
> ladder re-run moved r6_renamed_keys / r6_verbose to their ground-truth
> RELEASE verdicts with accuracy unchanged, and left the genuine
> r0_never_ships model failure untouched. Full detail: ladder report §4
> in `goals/paypal-hackathon-escrow-agent/files/`.

## Week 4 — Demo polish (Oct 29–Nov 4) — core items done early, 2026-10-24
- [x] Tracking simulator: scripted scenarios (clean delivery / never ships /
      ambiguous) — each with its own demo deal ($500 camera / $300 keyboard /
      $1,200 laptop, matching DEMO_SCRIPT.md)
- [x] Dashboard polish: live feed (auto-refresh), cost-per-decision display,
      plus the shadow verdict + agreement columns the demo points at
- [x] Full end-to-end dry runs of all three demo scenarios (2026-10-24:
      clean → RELEASE day 3, never-ships → REFUND day 6, ambiguous →
      INVESTIGATE throughout; shadow fields populated on every tier-1
      decision; total AI spend $0.00)

## Week 5 — Submission (Nov 5–12)
- [ ] Demo video (< 3 min, YouTube, public)
- [ ] Devpost write-up: features, functionality, tools used
- [ ] Repo cleanup, open-source license, run instructions tested fresh
- [ ] Submit by Nov 10–11 — do not cut it close

## Submission checklist (from official rules)
- [ ] Working prototype (judges can run it or click hosted demo)
- [ ] Text description of features/functionality
- [ ] Demo video < 3 min on public YouTube, linked on submission
- [ ] Public repo (GitHub/GitLab/Bitbucket) with visible open-source license
- [ ] AI tools used + how they were used, documented
- [ ] Meaningful PayPal integration (sandbox) + meaningful AI — both central
