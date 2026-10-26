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
from .policy import DEFAULT_POLICY, Policy

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
    # Accepted split settlement on an open dispute (dispute evidence,
    # Track 5): the buyer has the item (delivered PASS) and has agreed
    # in the dispute record to close it for a partial release. That is
    # a party-agreed resolution, not a judgment call — it outranks the
    # condition-dispute INVESTIGATE below (the damaged-but-kept case is
    # exactly condition FAIL + this offer), but never a hard fail
    # above: a seller who never shipped doesn't get a split.
    res = by_name.get("dispute_resolution")
    if res and res.status == CheckStatus.PASS:
        cents = res.detail.get("split_release_cents")
        delivered_r = by_name.get("delivered")
        if (isinstance(cents, int) and not isinstance(cents, bool)
                and 0 < cents < deal.terms.amount_cents
                and delivered_r
                and delivered_r.status == CheckStatus.PASS):
            return Decision(
                deal_id=deal.id, verdict=Verdict.SPLIT,
                split_release_cents=cents, confidence=0.9,
                reasoning=f"Dispute resolved by the buyer's accepted "
                          f"split settlement: release {cents} of "
                          f"{deal.terms.amount_cents} cents to the "
                          f"seller and void the remaining "
                          f"{deal.terms.amount_cents - cents} cents "
                          f"back to the buyer. {res.evidence}.",
                key_evidence=[res.evidence, delivered_r.evidence],
                tier=0, cost_usd=0.0,
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
    # Buyer-confirmation release: the buyer confirmed receipt and the
    # decisive checks pass. The address check is NOT required here —
    # the buyer is the authority on whether the item reached them.
    # Placement matters: hard fails above already short-circuited to
    # REFUND (amount_match FAIL included), and a condition FAIL above
    # already forced INVESTIGATE, so this rule can never override
    # either; the explicit condition check below keeps that true even
    # if the blocks above are ever reordered.
    if deal.buyer_confirmed_at is not None:
        needed = ("delivered", "amount_match", "no_dispute")
        cond = by_name.get("condition_clear")
        if (all(by_name.get(n) and by_name[n].status == CheckStatus.PASS
                for n in needed)
                and not (cond and cond.status == CheckStatus.FAIL)):
            ev = [by_name[n].evidence for n in needed]
            return Decision(
                deal_id=deal.id, verdict=Verdict.RELEASE, confidence=0.96,
                reasoning=f"Buyer confirmed receipt at "
                          f"{deal.buyer_confirmed_at} and the decisive "
                          f"checks pass (delivered, amount matches, no "
                          f"dispute): the buyer is the address "
                          f"authority, so the address check is not "
                          f"required to release.",
                key_evidence=ev, tier=0, cost_usd=0.0,
            )
    # Partial shipment: some items verifiably delivered, the rest
    # not — and every other decisive fact is known-good (amount
    # matches, no dispute, condition not failed, delivery address
    # verified; hard fails already returned above). Settle
    # proportionally instead of parking the whole amount: capture
    # the delivered fraction, void the rest.
    delivered = by_name.get("delivered")
    if (delivered and delivered.status == CheckStatus.UNKNOWN
            and "items_expected" in delivered.detail
            and "items_delivered" in delivered.detail):
        got = delivered.detail["items_delivered"]
        expected = delivered.detail["items_expected"]
        amount_r = by_name.get("amount_match")
        dispute_r = by_name.get("no_dispute")
        addr_r = by_name.get("address_match")
        cond = by_name.get("condition_clear")
        # Address guard (A5 corpus finding, 2026-10-24): a partial
        # split pays the seller for items said to be delivered —
        # the delivery address must be verified first. A wrong ZIP
        # (FAIL) or no recorded delivered ZIP (UNKNOWN) means the
        # fraction may have gone to the wrong place: fall through
        # to INVESTIGATE, never split. Sole exception: a buyer who
        # confirmed receipt is the address authority (the same
        # principle as the confirmation rule above).
        addr_ok = (addr_r is not None
                   and addr_r.status == CheckStatus.PASS) \
            or deal.buyer_confirmed_at is not None
        if (isinstance(got, int) and isinstance(expected, int)
                and 0 < got < expected
                and amount_r and amount_r.status == CheckStatus.PASS
                and dispute_r and dispute_r.status == CheckStatus.PASS
                and addr_ok
                and not (cond and cond.status == CheckStatus.FAIL)):
            split = (deal.terms.amount_cents * got) // expected
            if 0 < split < deal.terms.amount_cents:
                return Decision(
                    deal_id=deal.id, verdict=Verdict.SPLIT,
                    confidence=0.9,
                    reasoning=f"Partial shipment: {got} of {expected} "
                              f"items delivered — split settlement "
                              f"releases the delivered fraction "
                              f"({got}/{expected}) of "
                              f"{deal.terms.amount_cents} cents = "
                              f"{split} cents to the seller and voids "
                              f"the remaining "
                              f"{deal.terms.amount_cents - split} cents "
                              f"back to the buyer.",
                    key_evidence=[delivered.evidence,
                                  amount_r.evidence],
                    tier=0, cost_usd=0.0,
                    split_release_cents=split,
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
    if shadow.items:
        decision.shadow_items = [dict(item) for item in shadow.items]
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
- Confidence below {tier2_min_confidence} must be INVESTIGATE.
Respond with JSON only: {"verdict": "RELEASE|REFUND|INVESTIGATE",
"confidence": 0.0-1.0, "reasoning": "...", "key_evidence": [...],
"recommended_action": "..."}"""


def _tier2_grok(deal: Deal, results: list[CheckResult],
                policy: Policy = DEFAULT_POLICY) -> Decision:
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
        # (replace, not .format: the prompt contains literal JSON braces)
        messages=[{"role": "system", "content": _GROK_SYSTEM.replace(
                      "{tier2_min_confidence}",
                      str(policy.tier2_min_confidence))},
                  {"role": "user", "content": user}],
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    verdict = Verdict(parsed["verdict"].lower())
    if (parsed.get("confidence", 1.0) < policy.tier2_min_confidence
            and verdict != Verdict.INVESTIGATE):
        verdict = Verdict.INVESTIGATE
    return Decision(
        deal_id=deal.id, verdict=verdict,
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=parsed.get("reasoning", ""),
        key_evidence=parsed.get("key_evidence", []),
        tier=2, cost_usd=0.01,  # rough; replace with measured
    )


def adjudicate(deal: Deal, results: list[CheckResult],
               policy: Policy | None = None) -> Decision:
    """Tiered pipeline: Tier 0 decides; the forest shadows; Grok is
    reserved for hard cases. A forest RELEASE/REFUND is never returned.
    """
    policy = policy or DEFAULT_POLICY
    decided = _tier0(deal, results)
    if decided:
        return decided
    # Tier 0 didn't settle it: the forest runs as a shadow sensor on
    # every such case. Its verdict goes to the audit log only.
    shadow = _tier1_shadow(deal, results)
    unknowns = sum(1 for r in results if r.status == CheckStatus.UNKNOWN)
    fails = [r for r in results if r.status == CheckStatus.FAIL]
    evidence = [r.evidence for r in results if r.evidence]
    if (unknowns <= policy.shadow_max_unknowns
            and len(fails) <= policy.shadow_max_fails):
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
        d = _tier2_grok(deal, results, policy)
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
