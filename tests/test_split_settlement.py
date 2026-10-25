"""Split settlement (L2): capture X cents, void the remainder.

Unit tests pin the integer-cent math and every guard; engine tests
run partial-delivery evidence through the real checklist + Tier 0;
settle tests run the full DealManager path in simulated mode; a
call-discipline test spies on the PayPal client through a PayPal-mode
manager; and one live sandbox test proves the split against PayPal's
own read-backs.

Live-test shape — observed on the sandbox 2026-10-24 before this
implementation was finalized (probe on $300.00 authorizations):
a partial capture of 150.00 with final_capture=False returns 201 /
COMPLETED and leaves the authorization PARTIALLY_CAPTURED; a
following void succeeds and the authorization reads back VOIDED,
with exactly one capture (150.00) on the order. (A single
final_capture=True capture was also probed: it completes, the
authorization reads CAPTURED, and a void is then rejected 422
PREVIOUSLY_CAPTURED — that is why execute_split ships the
capture-then-void shape.)
"""
import pytest

from src import paypal_client
from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.decision_engine import _tier0, adjudicate
from src.models import Deal, DealState, Decision, Verdict
from src.split_settlement import execute_split

from .e2e_support import (KEYBOARD, TEST_CARD_SOURCE, audit_log,  # noqa: F401
                          held_cleanup, make_terms, order_captures,
                          pp_auth, requires_sandbox)

DELIVERED = {"number": "1ZSPLIT001", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z",
             "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
             "delivered_zip": "47172"}


def _deal(amount_cents=30000, auth_id=None):
    deal = Deal(id="deal-split-test",
                terms=make_terms((amount_cents, "Mechanical keyboard")))
    deal.paypal_auth_id = auth_id
    return deal


def _decision(deal, verdict=Verdict.SPLIT, split=None):
    return Decision(deal_id=deal.id, verdict=verdict, confidence=0.9,
                    reasoning="split-settlement test", tier=0,
                    split_release_cents=split)


def _checklist(amount_cents, got, expected, tracking_extra=None):
    deal = _deal(amount_cents)
    tracking = {**DELIVERED, "items_expected": expected,
                "items_delivered": got, **(tracking_extra or {})}
    evidence = {"tracking": tracking, "days_since_funding": 3,
                "authorized_cents": amount_cents,
                "dispute_filed": False, "seller_completion_rate": 0.95,
                "messages": []}
    return deal, run_checklist(deal, evidence)


# --- math + guards (unit) ---

def test_split_math_floors_on_odd_cents():
    """10001 cents, 1 of 3 delivered -> floor(10001/3) = 3333."""
    deal, results = _checklist(10001, got=1, expected=3)
    decision = _tier0(deal, results)
    assert decision is not None
    assert decision.verdict == Verdict.SPLIT
    assert decision.split_release_cents == 3333


def test_no_split_when_nothing_delivered():
    deal, results = _checklist(30000, got=0, expected=2)
    decision = _tier0(deal, results)
    assert decision is None or decision.verdict != Verdict.SPLIT


def test_no_split_when_fully_delivered():
    deal, results = _checklist(30000, got=2, expected=2)
    decision = _tier0(deal, results)
    assert decision is not None
    assert decision.verdict == Verdict.RELEASE


@pytest.mark.parametrize("bad", [0, -1, 30000, 30001])
def test_execute_split_rejects_out_of_range_amounts(bad):
    deal = _deal(30000)
    with pytest.raises(ValueError):
        execute_split(deal, _decision(deal, split=bad), use_paypal=False)


def test_execute_split_rejects_missing_amount():
    deal = _deal(30000)
    with pytest.raises(ValueError):
        execute_split(deal, _decision(deal, split=None), use_paypal=False)


def test_execute_split_rejects_non_split_verdict():
    deal = _deal(30000)
    with pytest.raises(ValueError):
        execute_split(deal, _decision(deal, verdict=Verdict.RELEASE,
                                      split=15000),
                      use_paypal=False)


def test_execute_split_paypal_mode_requires_authorization():
    deal = _deal(30000, auth_id=None)
    with pytest.raises(RuntimeError):
        execute_split(deal, _decision(deal, split=15000), use_paypal=True)


def test_execute_split_sim_mode_makes_no_calls(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("PayPal must not be called in sim mode")

    monkeypatch.setattr(paypal_client, "capture", _boom)
    monkeypatch.setattr(paypal_client, "void", _boom)
    deal = _deal(30001)
    result = execute_split(deal, _decision(deal, split=10000),
                           use_paypal=False)
    assert result == {"capture_id": None, "released_cents": 10000,
                      "voided_cents": 20001, "capture": None}


# --- engine: partial delivery through the real checklist ---

def test_engine_partial_delivery_splits_with_exact_cents():
    deal, results = _checklist(50000, got=1, expected=2)
    by_name = {r.name: r for r in results}
    assert by_name["delivered"].detail == {"items_delivered": 1,
                                           "items_expected": 2}
    decision = adjudicate(deal, results)
    assert decision.verdict == Verdict.SPLIT
    assert decision.tier == 0
    assert decision.split_release_cents == 25000


def test_engine_full_delivery_still_releases():
    """Regression: 2 of 2 items is a clean RELEASE, never a SPLIT."""
    deal, results = _checklist(50000, got=2, expected=2)
    decision = adjudicate(deal, results)
    assert decision.verdict == Verdict.RELEASE
    assert decision.split_release_cents is None


# --- simulated settle, end to end ---

def _funded_sim_deal(amount_cents=30000):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms((amount_cents, "Mechanical keyboard")))
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    return mgr, deal


def test_sim_settle_split_lands_split_settled():
    mgr, deal = _funded_sim_deal()
    mgr.settle(deal, _decision(deal, split=15000))
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.capture_id is None
    assert deal.split_released_cents == 15000
    assert deal.split_voided_cents == 15000


def test_sim_settle_split_is_terminal():
    mgr, deal = _funded_sim_deal()
    mgr.settle(deal, _decision(deal, split=15000))
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, split=15000))
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, verdict=Verdict.RELEASE))
    assert deal.state == DealState.SPLIT_SETTLED


def test_sim_settle_split_without_amount_raises():
    mgr, deal = _funded_sim_deal()
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, split=None))
    assert deal.state == DealState.FUNDED
    assert deal.split_released_cents is None


# --- call discipline through a PayPal-mode manager ---

def test_paypal_settle_split_call_discipline(monkeypatch):
    """Exactly one partial capture (right amount, final_capture
    False — the empirically accepted shape) and exactly one void."""
    calls = {"capture": [], "void": []}

    def fake_capture(authorization_id, amount_cents=None,
                     currency="USD", final_capture=False):
        calls["capture"].append({
            "authorization_id": authorization_id,
            "amount_cents": amount_cents, "currency": currency,
            "final_capture": final_capture})
        return {"id": "CAP-SPLIT-1", "status": "COMPLETED"}

    def fake_void(authorization_id):
        calls["void"].append(authorization_id)
        return {"status": "VOIDED"}

    monkeypatch.setattr(paypal_client, "capture", fake_capture)
    monkeypatch.setattr(paypal_client, "void", fake_void)

    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    deal.state = DealState.FUNDED  # funded record with a fake auth id
    deal.paypal_auth_id = "AUTH-SPLIT-FAKE"
    mgr.settle(deal, _decision(deal, split=15000))

    assert calls["capture"] == [{
        "authorization_id": "AUTH-SPLIT-FAKE", "amount_cents": 15000,
        "currency": "USD", "final_capture": False}]
    assert calls["void"] == ["AUTH-SPLIT-FAKE"]
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.capture_id == "CAP-SPLIT-1"
    assert deal.split_released_cents == 15000
    assert deal.split_voided_cents == 15000


# --- live sandbox split, proven by PayPal read-backs ---

@requires_sandbox
def test_live_sandbox_split_settlement(audit_log, held_cleanup):
    """Card-fund the $300 keyboard, split-settle 15000, and prove it
    from PayPal's side (see module docstring for the probed shape):
    exactly one capture on the order, COMPLETED, "150.00"; the deal
    is SPLIT_SETTLED with matching split fields and capture_id; and
    the remainder is genuinely released — the authorization reads
    back VOIDED after the partial capture + void, so nothing of the
    remaining $150.00 stays held."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)
    assert deal.state == DealState.FUNDED
    assert pp_auth(deal.paypal_auth_id)["status"] == "CREATED"
    assert pp_auth(deal.paypal_auth_id)["amount"]["value"] == "300.00"
    assert order_captures(deal.paypal_order_id) == []

    mgr.settle(deal, _decision(deal, split=15000))

    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.split_released_cents == 15000
    assert deal.split_voided_cents == 15000

    captures = order_captures(deal.paypal_order_id)
    assert len(captures) == 1
    assert captures[0]["status"] == "COMPLETED"
    assert captures[0]["amount"]["value"] == "150.00"
    assert captures[0]["amount"]["currency_code"] == "USD"
    assert deal.capture_id == captures[0]["id"]

    # Remainder released: the authorization itself is VOIDED.
    assert pp_auth(deal.paypal_auth_id)["status"] == "VOIDED"
