# Architecture

## Components

```
                    ┌──────────────┐
                    │  Dashboard   │  live deal states, feed, cost/decision
                    └──────┬───────┘
                           │
┌────────┐   ┌─────────────▼──────────────┐   ┌───────────────┐
│ Deal   │──▶│      Decision Engine       │◀──│  Evidence     │
│ Manager│   │  single adjudication prompt │   │  Gatherers    │
└───┬────┘   │  RELEASE / REFUND /        │   └───────┬───────┘
    │        │  INVESTIGATE               │           │
    │        └─────────────┬──────────────┘           │
    │                      │                        │
    ▼                      ▼                        ▼
┌─────────┐        ┌──────────────┐        ┌────────────────┐
│ PayPal  │        │  Audit Log   │        │ Tracking sim / │
│ sandbox │        │ (append-only)│        │ carrier APIs   │
│ auth /  │        └──────────────┘        └────────────────┘
│ capture │
│ void    │
└─────────┘
```

## Deal lifecycle

1. **PROPOSED** — buyer + seller agree: amount, item, conditions, window.
2. **FUNDED** — PayPal authorization locks the funds. Nobody has the money.
3. **MONITORING** — evidence gatherers poll conditions on a schedule.
4. **DECIDED** — decision engine returns a verdict.
5. **RELEASED** (capture → seller) | **REFUNDED** (void → buyer) |
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
| 1 — single prompt | minor unknowns, otherwise clear | ~$0.001–0.01 | small model, one call |
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
