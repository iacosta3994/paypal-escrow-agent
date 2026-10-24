"""Tiered adjudication engine.

Tier 0: deterministic — hard fails short-circuit to REFUND, decisive
        passes short-circuit to RELEASE, condition disputes force
        INVESTIGATE. No model call, $0. Tier 0 is authoritative.
Tier 1: Laya decision forest in SHADOW mode — small local networks
        evaluate the same deal from different angles and a deterministic
        aggregator combines their answers, but the forest no longer
        decides. It runs on every case Tier 0 doesn't settle; its
        verdict is written to the audit log next to the real decision
        (shadow_verdict / shadow_confidence / agreement). Disagreement
        or instability routes to INVESTIGATE — never an override.
        Demoted from judge to sensor 2026-10-23 (Ian, Option A) after
        failing the adjudication checklist in an authoritative role.
Tier 2: Grok (xAI) — deep reasoning, reserved for genuinely ambiguous,
        higher-value cases. Only spend here when ambiguity demands it.

Output is always a structured Decision: RELEASE / REFUND / INVESTIGATE.
"""
import json
import os

from . import config
from .models import CheckResult, CheckStatus, Deal, Decision, Verdict

# Hard-fail check names: any FAIL here short-circuits to REFUND, no model.
_HARD_FAILS = {"pickup_confirmed", "within_window", "amount_match"}

# If every decisive check passes, short-circuit to RELEASE.
_DECIDING_PASSES = {"delivered", "address_match", "amount_match", "no_dispute"}


def _tier0(deal: Deal, results: list[CheckResult]) -> Decision | None:
    by_name = {r.name: r for r in results}
    for name in _HARD_FAILS:
        r = by_name.get(name)
        if r and r.status == CheckStatus.FAIL:
            return Decision(
                deal_id=deal.id, verdict=Verdict.REFUND, confidence=0.95,
                reasoning=f"Hard fail on '{name}': {r.evidence}. "
                          "No judgment call needed.",
                key_evidence=[r.evidence], tier=0, cost_usd=0.0,
            )
    # Item arrived but condition is disputed: never auto-release,
    # never auto-refund — judgment is required.
    cond = by_name.get("condition_clear")
    if cond and cond.status == CheckStatus.FAIL:
        return Decision(
            deal_id=deal.id, verdict=Verdict.INVESTIGATE, confidence=0.0,
            reasoning=f"Delivery confirmed but condition disputed: "
                      f"{cond.evidence}. Releasing or refunding "
                      f"automatically would be guessing.",
            key_evidence=[cond.evidence,
                          by_name["delivered"].evidence], tier=0,
            cost_usd=0.0,
        )
    if all(by_name.get(n) and by_name[n].status == CheckStatus.PASS
           for n in _DECIDING_PASSES):
        ev = [by_name[n].evidence for n in _DECIDING_PASSES]
        return Decision(
            deal_id=deal.id, verdict=Verdict.RELEASE, confidence=0.97,
            reasoning="All decisive checks pass: delivered to the right "
                      "address, amount matches, no disputes.",
            key_evidence=ev, tier=0, cost_usd=0.0,
        )
    return None


def _tier1_shadow(deal: Deal, results: list[CheckResult]):
    """Run the Laya forest as a shadow sensor: local, $0.

    Returns a ShadowResult (advisory decision + tree votes), or None if
    the forest itself is unavailable. The shadow verdict is logged in
    the audit trail next to the real decision — it never IS the decision.
    """
    from .laya_forest import forest_shadow
    try:
        return forest_shadow(deal, results)
    except ImportError:
        return None


def _attach_shadow(decision: Decision, shadow) -> None:
    """Record the shadow outcome on the real decision for the audit log.

    agreement = fraction of forest trees whose vote backs the FINAL
    verdict (computed after the final verdict is fixed).
    """
    if shadow is None:
        return
    decision.shadow_verdict = shadow.decision.verdict.value
    decision.shadow_confidence = shadow.decision.confidence
    if shadow.votes:
        backers = sum(1 for v in shadow.votes.values()
                      if v == decision.verdict)
        decision.agreement = round(backers / len(shadow.votes), 4)


def _shadow_evidence(shadow) -> list[str]:
    if shadow is None:
        return ["shadow: Laya forest unavailable (not installed)"]
    return [f"shadow verdict: {shadow.decision.verdict.value} "
            f"(forest confidence {shadow.decision.confidence:.2f})"]


_GROK_SYSTEM = """You are an escrow referee. Given deal terms and a
checklist of verification results, return a JSON verdict. Rules:
- RELEASE only if delivery/condition evidence is solid.
- REFUND if the seller failed to perform (no shipment, window blown).
- INVESTIGATE if evidence conflicts or is too thin to decide — never guess.
- Confidence below 0.75 must be INVESTIGATE.
Respond with JSON only: {"verdict": "RELEASE|REFUND|INVESTIGATE",
"confidence": 0.0-1.0, "reasoning": "...", "key_evidence": [...],
"recommended_action": "..."}"""


def _tier2_grok(deal: Deal, results: list[CheckResult]) -> Decision:
    """Grok via xAI's OpenAI-compatible API. Only for hard cases."""
    import openai

    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        raise ImportError("XAI_API_KEY not set")
    client = openai.OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    checklist_text = "\n".join(
        f"- {r.name}: {r.status.value} (weight {r.weight}) — {r.evidence}"
        for r in results
    )
    user = (f"Deal {deal.id}: {deal.terms.item_description}, "
            f"{deal.terms.amount_cents/100:.2f} {deal.terms.currency}. "
            f"Conditions: {deal.terms.release_conditions}.\n"
            f"Checklist:\n{checklist_text}")
    resp = client.chat.completions.create(
        model=config.LLM_TIER2_MODEL, max_tokens=500,
        messages=[{"role": "system", "content": _GROK_SYSTEM},
                  {"role": "user", "content": user}],
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    verdict = Verdict(parsed["verdict"].lower())
    if parsed.get("confidence", 1.0) < 0.75 and verdict != Verdict.INVESTIGATE:
        verdict = Verdict.INVESTIGATE
    return Decision(
        deal_id=deal.id, verdict=verdict,
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=parsed.get("reasoning", ""),
        key_evidence=parsed.get("key_evidence", []),
        tier=2, cost_usd=0.01,  # rough; replace with measured
    )


def adjudicate(deal: Deal, results: list[CheckResult]) -> Decision:
    """Tiered pipeline: Tier 0 decides; the forest shadows; Grok is
    reserved for hard cases. A forest RELEASE/REFUND is never returned.
    """
    decided = _tier0(deal, results)
    if decided:
        return decided
    # Tier 0 didn't settle it: the forest runs as a shadow sensor on
    # every such case. Its verdict goes to the audit log only.
    shadow = _tier1_shadow(deal, results)
    unknowns = sum(1 for r in results if r.status == CheckStatus.UNKNOWN)
    fails = [r for r in results if r.status == CheckStatus.FAIL]
    evidence = [r.evidence for r in results if r.evidence]
    if unknowns <= 2 and len(fails) <= 1:
        # Formerly the forest's home turf. In shadow mode the case
        # escalates instead of letting the forest move money.
        d = Decision(
            deal_id=deal.id, verdict=Verdict.INVESTIGATE, confidence=0.0,
            reasoning="Tier 0 could not settle this, and the Laya forest "
                      "is shadow-only (demoted from judge to sensor "
                      "2026-10-23): escalated for human review rather "
                      "than letting the forest decide.",
            key_evidence=evidence + _shadow_evidence(shadow),
            tier=1, cost_usd=0.0,
        )
        _attach_shadow(d, shadow)
        return d
    # Hard case: Grok is reserved for these. If it decides, the shadow
    # is recorded next to its verdict; disagreement -> INVESTIGATE,
    # never a forest override.
    try:
        d = _tier2_grok(deal, results)
    except Exception:
        d = Decision(
            deal_id=deal.id, verdict=Verdict.INVESTIGATE, confidence=0.0,
            reasoning="Hard case and Grok unavailable; the shadow-only "
                      "forest cannot decide it either. Escalated for "
                      "human review rather than guessing.",
            key_evidence=evidence + _shadow_evidence(shadow),
            tier=1, cost_usd=0.0,
        )
        _attach_shadow(d, shadow)
        return d
    if shadow is not None and shadow.decision.verdict != d.verdict:
        d.reasoning += (
            f" (Shadow forest disagreed — forest said "
            f"{shadow.decision.verdict.value}; escalated to INVESTIGATE "
            f"rather than letting either model override.)"
        )
        d.verdict = Verdict.INVESTIGATE
    _attach_shadow(d, shadow)
    return d
