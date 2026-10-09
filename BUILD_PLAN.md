# Build Plan — Oct 8 → Nov 12

Submission deadline: **Nov 12, 2026, 12:00pm PT** (2pm CT). Target internal
freeze Nov 10 for buffer.

## Week 1 — Foundation (Oct 8–14)
- [ ] PayPal developer account + sandbox app (client ID/secret)
- [ ] `paypal_client.py`: authorize, capture, void against sandbox — tested live
- [ ] Repo scaffold, `.env`, requirements, this plan
- [ ] Deal data model (`models.py`) finalized

## Week 2 — Escrow core (Oct 15–21) — light week, Gateway FurMeet Oct 16–18
- [ ] Deal lifecycle state machine: PROPOSED → FUNDED → MONITORING → DECIDED
- [ ] Checklist array v1 (`checklist.py`) with stub evidence
- [ ] Rough dashboard: deal list + states

## Week 3 — AI layer (Oct 22–28)
- [ ] Evidence gatherers: tracking sim + (stretch) real carrier API
- [ ] Decision engine: tiered prompts, 3-way verdict, confidence scoring
- [ ] Audit log: every decision with evidence + reasoning + cost

## Week 4 — Demo polish (Oct 29–Nov 4)
- [ ] Tracking simulator: scripted scenarios (clean delivery / never ships /
      ambiguous photo)
- [ ] Dashboard polish: live feed, cost-per-decision display
- [ ] Full end-to-end dry runs of all three demo scenarios

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
