"""Permanent guard: the both-NO mixed-valence blind spot (S3).

The complexity ladder's residual checkpoint limitation: when SHIPPED
and WITHIN_WINDOW are both NO, every question phrasing tried still
draws YES answers from the model — read on answers alone, the forest
can be fooled all the way to RELEASE on a deal where the seller
never shipped and the window is blown. This test pins the three
layers that make that harmless in production:

1. Tier 0 owns the outcome. Both-NO means pickup_confirmed or
   within_window FAIL — both Tier 0 hard fails — so adjudicate()
   settles REFUND at tier 0 and the forest is never even consulted
   (the decision carries no shadow fields at all).
2. Fact-certainty gating. With the §4 facts pass-through, even the
   stuck-YES answers cannot carry a RELEASE vote: the votes rest on
   facts the gating knows are NO.
3. End-to-end: a never-ships deal past its window refunds through
   DealManager, with a tier-0 audit row and no shadow opinion.
"""
from src import laya_forest
from src.deal_manager import DealManager
from src.decision_engine import adjudicate
from src.evidence import TrackingSimulator
from src.models import (CheckResult, CheckStatus, Deal, DealState,
                        DealTerms, Verdict)

from .e2e_support import audit_log, audit_rows_for, make_terms, KEYBOARD


def _both_no_results():
    """Checklist results for: never shipped + window blown."""
    return [
        CheckResult("tracking_exists", CheckStatus.PASS),
        CheckResult("pickup_confirmed", CheckStatus.FAIL),
        CheckResult("in_transit", CheckStatus.FAIL),
        CheckResult("delivered", CheckStatus.UNKNOWN),
        CheckResult("address_match", CheckStatus.UNKNOWN),
        CheckResult("within_window", CheckStatus.FAIL),
        CheckResult("amount_match", CheckStatus.PASS),
        CheckResult("no_dispute", CheckStatus.PASS),
        CheckResult("seller_history", CheckStatus.PASS),
        CheckResult("condition_clear", CheckStatus.UNKNOWN),
    ]


def _deal() -> Deal:
    return Deal(id="both-no", terms=DealTerms(
        amount_cents=30000, currency="USD",
        item_description="Mechanical keyboard",
        buyer_email="b@x", seller_email="s@x", delivery_zip="47172"))


# The documented misread: answers claim shipped + in-window anyway.
_STUCK_YES = [
    {"tree": "fulfillment", "shipped": "yes", "delivered": "yes",
     "address_match": "yes", "condition_ok": "yes",
     "within_window": "yes"},
    {"tree": "condition", "condition_ok": "yes", "complaint": "no",
     "delivered": "yes"},
    {"tree": "risk", "shipped": "yes", "within_window": "yes",
     "amount_match": "yes", "seller_history_good": "yes",
     "complaint": "no", "address_match": "yes"},
]

_BOTH_NO_FACTS = {
    "fulfillment": {"SHIPPED": "no", "DELIVERED": "unknown",
                    "ADDRESS_MATCH": "unknown", "CONDITION_OK": "unknown",
                    "WITHIN_WINDOW": "no"},
    "condition": {"CONDITION_OK": "unknown", "BUYER_COMPLAINT": "no",
                  "DELIVERED": "unknown"},
    "risk": {"SHIPPED": "no", "WITHIN_WINDOW": "no",
             "AMOUNT_MATCH": "yes", "SELLER_HISTORY_GOOD": "yes",
             "BUYER_COMPLAINT": "no", "ADDRESS_MATCH": "unknown"},
}


def test_forest_can_be_fooled_on_answers_alone__documented():
    """The blind spot is real: voting on the stuck-YES answers alone
    (no fact gating), the forest reaches RELEASE on both-NO facts.
    This is WHY it is a shadow sensor and why layers 1-2 exist."""
    agg = laya_forest.aggregate([dict(t) for t in _STUCK_YES])
    assert agg["verdict"] == Verdict.RELEASE


def test_gating_refuses_the_stuck_yes_answers():
    """With ground-truth facts attached (the production §4 path), the
    same stuck-YES answers cannot carry a single RELEASE vote."""
    trees = []
    for t in _STUCK_YES:
        t = dict(t)
        t["_facts"] = _BOTH_NO_FACTS[t["tree"]]
        trees.append(t)
    agg = laya_forest.aggregate(trees)
    assert agg["verdict"] == Verdict.INVESTIGATE
    assert Verdict.RELEASE not in agg["votes"].values()


def test_tier0_owns_both_no_and_forest_is_never_consulted():
    decision = adjudicate(_deal(), _both_no_results())
    assert decision.verdict == Verdict.REFUND
    assert decision.tier == 0
    # Not "logged and ignored" — never asked. No shadow fields exist
    # on a Tier-0-settled decision.
    assert decision.shadow_verdict is None
    assert decision.shadow_confidence is None
    assert decision.agreement is None


def test_both_no_end_to_end_refunds(audit_log):
    mgr = DealManager()  # simulated funding; the verdict path is real
    deal = mgr.propose(make_terms(KEYBOARD))
    mgr.fund(deal)
    tracking = TrackingSimulator("never_ships").advance(6)
    mgr.run_cycle(deal, tracking, days_since_funding=15)
    assert deal.state == DealState.REFUNDED
    rows = audit_rows_for(deal.id)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "refund"
    assert rows[0]["tier"] == 0
    assert rows[0]["shadow_verdict"] is None
