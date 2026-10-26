# Architecture

## Kernel vs adapters (Wave 1 / A1, 2026-10-24)

The system is split into a deterministic **kernel** that owns every
verdict and every state transition, and three **adapters** at narrow,
explicit boundaries. The kernel never imports a concrete client; it
depends on the interfaces in `src/adapters/base.py` and receives
implementations by injection (defaulted, so existing call sites are
unchanged).

```
        KERNEL (owns verdicts + state; deterministic; $0)
 ┌────────────────────────────────────────────────────────────┐
 │  DealManager — deal state machine                          │
 │  checklist.py — the verification checks (product IP)       │
 │  decision_engine.py — Tier 0 rules (authoritative)        │
 │  policy.py — operator thresholds as data (see below)       │
 └──────┬──────────────────┬───────────────────┬──────────────┘
        │ MoneyAdapter      │ EvidenceAdapter   │ sensor (shadow)
        │ (adapters/base)   │ (adapters/base)   │ forest behind its
 ┌──────▼─────────┐  ┌──────▼──────────────┐  │ 8 atomic questions
 │ PayPalMoney-   │  │ SimulatorEvidence-│  │ (laya_questions /
 │ Adapter        │  │ Adapter           │  │  laya_forest) —
 │ (adapters/     │  │ (adapters/        │  │ advisory only,
 │  paypal.py) →  │  │  simulator.py) →  │  │ never moves money
 │ paypal_client  │  │ evidence.py       │  └───────────────────
 │ (Orders v2)    │  │ (TrackingSim.)    │
 └────────────────┘  └───────────────────┘
```

- **Money adapter** (`MoneyAdapter`, `src/adapters/base.py`):
  `create_authorization / authorize_order / get_order /
  get_authorization / capture / void` — exactly the operations the
  kernel uses, returning the documented dict shapes. Implemented by
  `PayPalMoneyAdapter`, which delegates to `src/paypal_client.py`
  (Orders v2 authorize/capture/void; split settlement is a partial
  capture + void through the same interface). A different processor
  implements the same six methods; the kernel does not change.
- **Evidence adapter** (`EvidenceAdapter`): one method,
  `gather(deal, tracking, days_since_funding, messages=None)`,
  returning the evidence dict whose contract (every key, type, and
  meaning) is documented in `src/adapters/base.py`. Implemented
  today by `SimulatorEvidenceAdapter` over the scripted
  `TrackingSimulator` in `src/evidence.py`. The contract is written
  so a second adapter — e.g. a GitHub code-deliverable gatherer —
  can be implemented against it without touching the kernel.
- **Sensor boundary** (pre-existing, not refactored in A1): the Laya
  forest sits behind its 8 atomic questions (`src/laya_questions.py`)
  and is consumed by the decision engine only as a shadow sensor —
  its verdict is logged next to the real decision, disagreement
  routes to INVESTIGATE, and it can never execute a verdict.

Supporting pieces outside the kernel/adapter split: the audit log
(append-only), the dashboard + party pages (`src/app.py`), PayPal
webhooks, dispute-evidence submission, and notifications.

## Policy as data (Wave 1 / A2)

Every operator-judgment threshold lives in `src/policy.py` as a
frozen `Policy` dataclass: the default delivery window (14d),
authorization-expiry warn/escalate ages (20/25d of the 29d PayPal
lifetime), inconclusive cycles before escalation (3), the carrier
stall threshold (5d), the seller completion-rate threshold (0.9),
the Tier 2 minimum confidence (0.75), and the shadow-routing bounds
(≤2 unknowns, ≤1 fail). `DEFAULT_POLICY` reproduces the values that
were previously hardcoded, exactly; overrides come from constructor
injection (`DealManager(policy=...)`, `policy=` on `run_checklist` /
`adjudicate` / `expiry_status`) or `ESCROW_POLICY_*` environment
variables. Structural constants — state/verdict names, API
endpoints, the split invariant `0 < split < amount` — are not
policy and stay in code.

## Code-deliverable deals (Wave 2 / B, 2026-10-24)

A second domain — escrow for software delivery — was added with the
kernel unchanged (deal state machine, Tier 0 rules, and the money
path are byte-identical; the only kernel-adjacent edit is two
ADDITIVE policy keys, `code_release_threshold` /
`code_split_min_fraction`, with defaults no physical-goods code
reads). The extension lives in `src/code_delivery/`:

- `GitHubEvidenceAdapter` implements the Wave 1 EvidenceAdapter
  boundary over a local repo path (primary) or a public GitHub URL
  (read-only clone only), gathering README, tree, unfinished
  markers, test presence/runs, and commit activity, and mapping the
  grade onto the carrier-shaped tracking names so even
  `DealManager.monitor()` reaches the same verdicts.
- `grading.py` grades a structured requirements list (file /
  module / symbol / tests-pass / README-section / manual-judgment)
  deterministically, with integer-explicit fraction math
  (satisfied × 10000 // total, in basis points). Manual-judgment
  items grade UNKNOWN unless a human/policy verdict is supplied;
  a decisive UNKNOWN forces INVESTIGATE. No sensor is consulted.
- `checklist.py` is a checklist pack in the kernel's existing
  CheckResult vocabulary (hard-fail → REFUND, decisive passes →
  RELEASE, partial-shipment detail → SPLIT at the measured
  fraction, condition-fail → INVESTIGATE), decided by the existing
  `decision_engine.adjudicate()` and settled by the existing
  `DealManager.settle()`.

## Pull-request grading + the /grade workbench (2026-10-25)

The requirements-list pattern generalized to a third deliverable
shape: a GitHub pull request, graded against the list from the
issue/agreement it claims to close. This domain is audit-only —
`src/pr_delivery/` has no checklist pack and settles nothing; it
produces the same `GradeResult` audit record a code deal produces,
with the same integer basis-point fraction and decisive-unknown
rule, so grades are comparable across domains.

- `gather.py` collects read-only GitHub evidence: PR metadata,
  the paginated changed-file list, and the head commit's check
  runs — each recorded with its run id and start time. The network
  sits behind an injectable fetcher (tests run on recorded
  payloads); a token comes from the argument or `GITHUB_TOKEN`
  and is never stored on the evidence object.
- `requirements.py` defines the PR kinds: `files_include` /
  `files_exclude` (a path or glob the diff must / must not touch),
  `check_passes` (a named check on the head commit), 
  `checklist_item` (an item in the PR body's own task list,
  checked vs unchecked), and `manual_judgment`.
- `grading.py` grades deterministically. One subtlety lives here:
  GitHub returns EVERY run of a check, including superseded
  reruns, so `check_passes` judges only the LATEST run per check
  name (highest run id — ids are monotonic). A check that is
  missing, or whose latest run is still running, grades UNKNOWN.

The front end is the dashboard's `/grade` page (`src/app.py`):
state a requirements list as JSON, point it at a local repo /
deliverable folder or a GitHub PR, and read back the item-by-item
audit with its evidence, the completion fraction, and the policy
band that fraction falls in (RELEASE / SPLIT / REFUND /
INVESTIGATE). Advisory only — the page moves no money.

## Agreements, share links, milestones (2026-10-26)

The web layer (`src/app.py`, one shared Atlas Glass shell) gained
the full user flow from the 2026-10-26 flow spec. A proposal
carries the agreement itself — `DealTerms.requirements`, an
additive list of `{text, check}` items whose `check` names a
checklist item or is `None` for manual judgment — and the
completion date is derived from the delivery window, never stored
twice. Proposed deals issue per-party share links
(`Deal.share_tokens`); the token matching the party gates both the
party page and every POST, through the same allow-list pattern as
`allowed_actions`. Identity-by-link is demo-grade by design (see
README limitations).

A deal may be structured as milestones (`src/models.py`
`Milestone`, `src/milestones.py`): shares that must sum to the deal
total, each with its own checklist item (or manual judgment), due
date, and state. Milestone deals skip whole-deal adjudication:
monitoring evaluates per milestone with the same Tier 0 rules,
the per-item shadow view groups checklist items under their
milestone, and `DealManager.settle_milestone` settles each share
through the existing capture/void mechanics. A failed milestone
refunds its share without unwinding settled ones; the deal closes
when every milestone has settled, at the aggregate outcome.
Disputes on milestone deals are milestone-scoped (evidence records
tagged with the milestone id); the deal-level dispute surface
belongs to lump deals.

## Deal lifecycle

1. **PROPOSED** — buyer + seller agree: amount, item, conditions, window.
2. **AWAITING_APPROVAL** — order created; the buyer must approve on
   PayPal (no API exists for that step) before funds are held.
3. **FUNDED** — PayPal authorization locks the funds. Nobody has the money.
4. **MONITORING** — evidence adapters feed the checklist on each pass.
5. **DECIDED** — decision engine returns a verdict.
6. **RELEASED** (capture → seller) | **REFUNDED** (void → buyer) |
   **SPLIT_SETTLED** (partial capture + void of the remainder) |
   **ESCALATED** (humans decide, AI presents the evidence).

## The checklist array

The checklist is the product's IP: which checks exist, their weights, and the
escalation thresholds. Each check returns `pass / fail / unknown` + evidence.

Shipment deals:
- tracking number exists and is valid
- carrier confirms pickup scan
- in-transit scans progressing (no stall > N days)
- delivered scan present
- delivered ZIP matches buyer
- no delivery exceptions
- within agreed window
- authorized amount == deal amount
- no dispute filed by either party
- seller history: prior completion rate above threshold

## Decision tiers (cost control)

| Tier | When | Cost | Action |
|------|------|------|--------|
| 0 — deterministic | all checks pass or a hard fail (e.g. no tracking after 14 days) | $0 | verdict without LLM |
| 1 — shadow sensor | Tier 0 didn't settle; the Laya forest evaluates in shadow mode and its advisory verdict is logged, never executed | $0 (local) | INVESTIGATE + shadow verdict in audit |
| 2 — deep reasoning | conflicting evidence, high value, fuzzy inputs | ~$0.10–0.50 | full reasoning model, may multi-step |

Rule: never spend Tier 2 money on a Tier 0 case. The dashboard shows
cost-per-decision next to every verdict — the unit economics are part of the
demo.

## Adjudication prompt (shape)

Input: deal terms + checklist results (status + evidence each).
Output (structured JSON):
```json
{
  "verdict": "RELEASE | REFUND | INVESTIGATE",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary for the audit trail",
  "key_evidence": ["tracking #... delivered 2026-11-02", "..."],
  "recommended_action": "optional human follow-up"
}
```

## Trust model

- Every decision logs evidence + reasoning (audit trail).
- Either party can override an AI verdict before execution (configurable window).
- Low confidence never auto-executes — it escalates.
- The AI doesn't need to be perfect; it needs to be auditable.
