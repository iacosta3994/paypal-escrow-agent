"""Single-prompt adjudication engine with cost tiers.

Tier 0: deterministic — all decisive checks pass, or a hard fail
        (e.g. no pickup scan after the stall threshold). No LLM call.
Tier 1: one cheap-model prompt for mostly-clear cases.
Tier 2: full reasoning model for conflicting/ambiguous evidence.

Output is always a structured Decision: RELEASE / REFUND / INVESTIGATE.
"""
import json

from . import config
from .models import CheckResult, CheckStatus, Deal, Decision, Verdict

# Hard-fail check names: any FAIL here short-circuits to REFUND, no LLM.
_HARD_FAILS = {"pickup_confirmed", "within_window", "amount_match"}

# If every weighted-decisive check passes, short-circuit to RELEASE.
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


_ADJUDICATION_SYSTEM = """You are an escrow referee. Given deal terms and a
checklist of verification results, return a JSON verdict. Rules:
- RELEASE only if delivery/condition evidence is solid.
- REFUND if the seller failed to perform (no shipment, window blown).
- INVESTIGATE if evidence conflicts or is too thin to decide — never guess.
- Confidence below 0.75 must be INVESTIGATE.
Respond with JSON only: {"verdict": "RELEASE|REFUND|INVESTIGATE",
"confidence": 0.0-1.0, "reasoning": "...", "key_evidence": [...],
"recommended_action": "..."}"""


def _llm_adjudicate(deal: Deal, results: list[CheckResult],
                    tier: int) -> Decision:
    provider = config.LLM_TIER1_PROVIDER if tier == 1 else config.LLM_TIER2_PROVIDER
    model = config.LLM_TIER1_MODEL if tier == 1 else config.LLM_TIER2_MODEL

    checklist_text = "\n".join(
        f"- {r.name}: {r.status.value} (weight {r.weight}) — {r.evidence}"
        for r in results
    )
    user = (f"Deal {deal.id}: {deal.terms.item_description}, "
            f"{deal.terms.amount_cents/100:.2f} {deal.terms.currency}. "
            f"Conditions: {deal.terms.release_conditions}. "
            f"Buyer {deal.terms.buyer_email}, seller {deal.terms.seller_email}.\n"
            f"Checklist:\n{checklist_text}")

    # Cost model: rough per-1k pricing; replace with measured on first runs.
    # tier1 ~$0.15/1M in, tier2 ~$3/1M in — a 2k-token call lands ~$0.0003 / $0.006.
    cost = 0.0003 if tier == 1 else 0.006

    if provider == "anthropic":
        import anthropic
        import os
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=model, max_tokens=500,
            system=_ADJUDICATION_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        raw = msg.content[0].text
    else:
        import openai
        import os
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=model, max_tokens=500,
            messages=[{"role": "system", "content": _ADJUDICATION_SYSTEM},
                      {"role": "user", "content": user}],
        )
        raw = resp.choices[0].message.content

    parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    verdict = Verdict(parsed["verdict"].lower())
    if parsed.get("confidence", 1.0) < 0.75 and verdict != Verdict.INVESTIGATE:
        verdict = Verdict.INVESTIGATE
    return Decision(
        deal_id=deal.id, verdict=verdict,
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=parsed.get("reasoning", ""),
        key_evidence=parsed.get("key_evidence", []),
        tier=tier, cost_usd=cost,
    )


def adjudicate(deal: Deal, results: list[CheckResult]) -> Decision:
    """Run the tiered pipeline: deterministic first, LLM only if needed."""
    decided = _tier0(deal, results)
    if decided:
        return decided
    unknowns = sum(1 for r in results if r.status == CheckStatus.UNKNOWN)
    fails = [r for r in results if r.status == CheckStatus.FAIL]
    # Mostly clear -> cheap model; conflicting/thin -> deep reasoning.
    tier = 1 if (unknowns <= 2 and len(fails) <= 1) else 2
    try:
        return _llm_adjudicate(deal, results, tier)
    except ImportError:
        # LLM provider not installed/configured: escalate, never guess.
        return Decision(
            deal_id=deal.id, verdict=Verdict.INVESTIGATE, confidence=0.0,
            reasoning="LLM provider unavailable — escalated for human review "
                      "rather than guessing.",
            key_evidence=[r.evidence for r in results if r.evidence],
            tier=tier, cost_usd=0.0,
        )
