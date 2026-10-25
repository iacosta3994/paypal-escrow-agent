"""Part B — not-ready cases against the LIVE flow.

Each case funds a real sandbox deal (card-funded, money genuinely
held), runs the product monitor/settle cycle, and asserts both sides:
locally the deal stays MONITORING, and on PayPal's side the
authorization is still CREATED with zero captures. A "not ready"
verdict that quietly captured anyway would fail these tests.
"""
from src.deal_manager import DealManager
from src.evidence import TrackingSimulator
from src.models import DealState, Verdict

from .e2e_support import (CAMERA, LAPTOP, TEST_CARD_SOURCE, audit_log,
                          held_cleanup, make_terms, no_grok,
                          order_captures, pp_auth, requires_sandbox)

pytestmark = requires_sandbox


def _assert_still_held(deal):
    assert deal.state == DealState.MONITORING
    assert pp_auth(deal.paypal_auth_id)["status"] == "CREATED"
    assert order_captures(deal.paypal_order_id) == []


def test_in_transit_not_ready_holds(audit_log, no_grok, held_cleanup):
    """$1,200 laptop, day 3, in transit but not delivered: no release,
    across two monitoring cycles."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(LAPTOP))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)
    assert pp_auth(deal.paypal_auth_id)["amount"]["value"] == "1200.00"

    sim = TrackingSimulator("clean_delivery")
    # Day 1: the scenario's in-transit stage. Day 3: same in-transit
    # snapshot (the scenario's own day-3 stage is delivered — the case
    # under test is "shipped, still moving, not delivered").
    in_transit = sim.advance(1)
    for day, tracking in ((1, in_transit), (3, dict(in_transit))):
        decision, _ = mgr.monitor(deal, tracking, days_since_funding=day)
        assert decision.verdict == Verdict.INVESTIGATE
        mgr.settle(deal, decision)
        _assert_still_held(deal)


def test_window_open_no_tracking_holds(audit_log, no_grok,
                                       held_cleanup):
    """$500 camera, day 4 of a 14-day window, no tracking at all:
    evidence is UNKNOWN, so nothing may settle."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)

    decision, results = mgr.monitor(deal, {}, days_since_funding=4)
    assert decision.verdict == Verdict.INVESTIGATE
    by_name = {r.name: r.status.value for r in results}
    assert by_name["delivered"] == "unknown"
    assert by_name["within_window"] == "pass"  # window is NOT the block
    mgr.settle(deal, decision)
    _assert_still_held(deal)


def test_damaged_delivery_holds(audit_log, no_grok, held_cleanup):
    """$1,200 laptop delivered but carrier reports visible damage
    (the demo's ambiguous scenario): Tier 0 itself refuses to move
    money on a condition dispute."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(LAPTOP))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)

    sim = TrackingSimulator("ambiguous")
    decision, results = mgr.monitor(deal, sim.advance(3),
                                    days_since_funding=3)
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.tier == 0  # condition dispute is Tier 0's own call
    by_name = {r.name: r.status.value for r in results}
    assert by_name["delivered"] == "pass"
    assert by_name["condition_clear"] == "fail"
    mgr.settle(deal, decision)
    _assert_still_held(deal)
