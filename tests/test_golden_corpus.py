"""Golden corpus: messy, realistic deals with expected verdicts
(improvement plan S1, 2026-10-24).

The clean demo scenarios are the happy path. These are the cases
that actually break escrow systems — partial shipments, typos,
carrier exception strings, cent-level amount mismatches, goods that
arrive late or damaged. Each runs the REAL decision path (checklist
+ tiered engine through DealManager; the amount cases feed the
evidence dict directly because through-product amounts come from
PayPal's authorization read-back, which the live suite already
proves match). The expected verdict is what the system SHOULD do,
not just what it happens to do: never release on ambiguity, never
refund on ambiguity either — investigate; refund only on hard
seller failure (window blown, amount wrong).
"""
import pytest

from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.decision_engine import adjudicate
from src.models import DealState, Verdict

from .e2e_support import (CAMERA, KEYBOARD, LAPTOP, audit_log,  # noqa: F401
                          make_terms, no_grok)  # noqa: F401

pytestmark = pytest.mark.usefixtures("audit_log", "no_grok")

DELIVERED = {"number": "1ZGOLD001", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z",
             "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
             "delivered_zip": "47172"}


def _run(amount_item, tracking, day=3, messages=None):
    mgr = DealManager()  # simulated funding; adjudication is real
    deal = mgr.propose(make_terms(amount_item))
    mgr.fund(deal)
    decision, results = mgr.monitor(deal, tracking, day, messages)
    mgr.settle(deal, decision)
    return deal, decision, {r.name: r for r in results}


# --- fulfillment messiness ---

def test_partial_shipment_splits():
    """1 of 2 items delivered, every other decisive fact known-good:
    SPLIT settlement (L2, 2026-10-24) — capture the delivered half,
    void the rest — instead of parking the whole amount at
    INVESTIGATE as this case originally expected. The delivered
    check still reports UNKNOWN/partial; Tier 0 turns the fraction
    into exact integer cents (floor)."""
    t = {**DELIVERED, "items_expected": 2, "items_delivered": 1}
    deal, decision, results = _run(CAMERA, t)
    assert results["delivered"].status.value == "unknown"
    assert "partial" in results["delivered"].evidence
    assert decision.verdict == Verdict.SPLIT
    assert decision.split_release_cents == CAMERA[0] // 2
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.split_released_cents == CAMERA[0] // 2
    assert deal.split_voided_cents == CAMERA[0] - CAMERA[0] // 2


def test_full_multi_item_delivery_releases():
    """Control for the partial case: 2 of 2 items -> clean RELEASE."""
    t = {**DELIVERED, "items_expected": 2, "items_delivered": 2}
    deal, decision, _ = _run(CAMERA, t)
    assert decision.verdict == Verdict.RELEASE
    assert deal.state == DealState.RELEASED


def test_address_typo_never_releases():
    """Delivered, but to ZIP 47127 — the agreed ZIP 47172 with two
    digits transposed. Address is a deciding check: no release."""
    t = {**DELIVERED, "delivered_zip": "47127"}
    deal, decision, results = _run(CAMERA, t)
    assert results["address_match"].status.value == "fail"
    assert decision.verdict == Verdict.INVESTIGATE
    assert deal.state == DealState.MONITORING


def test_late_but_delivered_refunds():
    """Delivered on day 15 of a 14-day window: the goods arrived but
    the seller blew the deal's terms — window is a Tier 0 hard fail.
    (Also the forest's known weak configuration; Tier 0 owns it.)"""
    deal, decision, results = _run(CAMERA, DELIVERED, day=15)
    assert results["within_window"].status.value == "fail"
    assert decision.verdict == Verdict.REFUND
    assert decision.tier == 0
    assert deal.state == DealState.REFUNDED


# --- carrier exception strings ---

def test_carrier_exception_weather_delay_investigates():
    t = {"number": "1ZGOLD002", "carrier": "FedEx",
         "pickup_scan": "2026-11-02T09:00:00Z", "in_transit": True,
         "exception": "delayed — weather"}
    deal, decision, _ = _run(LAPTOP, t, day=4)
    assert decision.verdict == Verdict.INVESTIGATE
    assert deal.state == DealState.MONITORING


def test_carrier_exception_held_at_facility_investigates():
    t = {"number": "1ZGOLD003", "carrier": "UPS",
         "pickup_scan": "2026-11-02T09:00:00Z",
         "exception": "held at facility"}
    deal, decision, _ = _run(KEYBOARD, t, day=6)
    assert decision.verdict == Verdict.INVESTIGATE
    assert deal.state == DealState.MONITORING


# --- delivered but condition disputed ---

def test_damaged_by_carrier_investigates():
    t = {**DELIVERED, "exception": "package visibly damaged"}
    deal, decision, _ = _run(LAPTOP, t)
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.tier == 0  # Tier 0 condition-dispute gate
    assert deal.state == DealState.MONITORING


def test_damaged_by_buyer_message_investigates():
    deal, decision, results = _run(
        LAPTOP, DELIVERED,
        messages=["arrived today — screen is cracked, item damaged"])
    assert results["condition_clear"].status.value == "fail"
    assert decision.verdict == Verdict.INVESTIGATE
    assert deal.state == DealState.MONITORING


def test_wrong_item_message_investigates():
    deal, decision, results = _run(
        CAMERA, DELIVERED, messages=["this is the wrong item entirely"])
    assert results["condition_clear"].status.value == "fail"
    assert decision.verdict == Verdict.INVESTIGATE
    assert deal.state == DealState.MONITORING


# --- amount mismatches (evidence-level; amounts are decisive) ---

def _adjudicate_amounts(terms_cents, authorized_cents):
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))
    deal.terms.amount_cents = terms_cents
    evidence = {"tracking": DELIVERED, "days_since_funding": 3,
                "authorized_cents": authorized_cents,
                "dispute_filed": False, "seller_completion_rate": 0.95,
                "messages": []}
    results = run_checklist(deal, evidence)
    return adjudicate(deal, results), {r.name: r for r in results}


def test_amount_mismatch_by_one_cent_refunds():
    """1 cent off is still off: amount_match is a Tier 0 hard fail.
    (The ladder showed numeric PRESENCE contaminates the forest;
    the verdict here never reaches it — Tier 0 settles.)"""
    decision, results = _adjudicate_amounts(50000, 49999)
    assert results["amount_match"].status.value == "fail"
    assert decision.verdict == Verdict.REFUND
    assert decision.tier == 0


def test_amount_mismatch_by_forty_percent_refunds():
    decision, results = _adjudicate_amounts(50000, 30000)
    assert results["amount_match"].status.value == "fail"
    assert decision.verdict == Verdict.REFUND
    assert decision.tier == 0
