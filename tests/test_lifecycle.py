"""Track L1 — two-party lifecycle.

Covers the acceptance gate on party-proposed deals (backward
compatible: manager-proposed deals fund exactly as before), decline
as a terminal no-money state, buyer confirm_receipt as a first-class
Tier 0 release input, the transition listeners other tracks register
on, and the models.py seams (DECLINED / SPLIT_SETTLED / SPLIT /
split_release_cents / CheckResult.detail / new Deal fields).
"""
import pytest

from src import evidence
from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.evidence import gather_evidence
from src.models import (CheckResult, CheckStatus, Deal, DealState,
                        Decision, Verdict)

from .e2e_support import CAMERA, audit_log, make_terms, no_grok


@pytest.fixture
def stable_seller(monkeypatch):
    """Pin the simulated seller completion rate into PASS territory
    so it never interferes with the lifecycle assertions."""
    monkeypatch.setattr(evidence.random, "uniform", lambda a, b: 0.95)


def _clean_tracking_no_zip():
    """Delivered, but with NO delivered_zip key: the address check
    stays UNKNOWN, so Tier 0's standard release cannot fire."""
    return {"number": "1ZCONF", "carrier": "UPS",
            "pickup_scan": "2026-11-02T10:00:00Z",
            "delivered": True, "delivered_at": "2026-11-05T14:00:00Z"}


def _clean_tracking():
    t = _clean_tracking_no_zip()
    t["delivered_zip"] = "47172"
    return t


# --- models.py seams ---

def test_model_seams_exist_with_defaults():
    assert DealState.DECLINED.value == "declined"
    assert DealState.SPLIT_SETTLED.value == "split_settled"
    assert Verdict.SPLIT.value == "split"

    d = Decision(deal_id="x", verdict=Verdict.SPLIT, confidence=0.5,
                 reasoning="r")
    assert d.split_release_cents is None
    assert d.to_dict()["split_release_cents"] is None
    d.split_release_cents = 12345
    assert d.to_dict()["split_release_cents"] == 12345

    cr1 = CheckResult("a", CheckStatus.PASS)
    cr2 = CheckResult("b", CheckStatus.PASS)
    assert cr1.detail == {} and cr2.detail == {}
    cr1.detail["k"] = 1
    assert cr2.detail == {}  # no shared mutable default

    deal = Deal(id="seam-1", terms=make_terms(CAMERA))
    assert deal.proposed_by is None
    assert deal.terms_acceptances == {}
    assert deal.buyer_confirmed_at is None
    assert deal.buyer_approved_at is None
    assert deal.dispute_evidence == []
    assert deal.capture_id is None
    assert deal.split_released_cents is None
    assert deal.split_voided_cents is None


def test_partial_delivery_detail():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    tracking = {"number": "1ZP", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "delivered": True, "delivered_at": "2026-11-05T14:00:00Z",
                "delivered_zip": "47172",
                "items_expected": 2, "items_delivered": 1}
    ev = gather_evidence(deal, tracking, days_since_funding=3)
    by_name = {r.name: r for r in run_checklist(deal, ev)}
    assert by_name["delivered"].status == CheckStatus.UNKNOWN
    assert by_name["delivered"].detail == {"items_delivered": 1,
                                           "items_expected": 2}


# --- acceptance gate ---

def test_seller_proposed_fund_requires_buyer_acceptance():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="seller")
    assert deal.proposed_by == "seller"
    assert "seller" in deal.terms_acceptances  # proposer authored terms
    with pytest.raises(ValueError) as exc:
        mgr.fund(deal)
    assert "buyer" in str(exc.value)
    assert deal.state == DealState.PROPOSED
    assert deal.funded_at is None

    mgr.accept_terms(deal, "buyer")
    assert "buyer" in deal.terms_acceptances
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED


def test_buyer_proposed_fund_requires_seller_acceptance():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="buyer")
    assert deal.proposed_by == "buyer"
    assert "buyer" in deal.terms_acceptances
    with pytest.raises(ValueError) as exc:
        mgr.fund(deal)
    assert "seller" in str(exc.value)
    assert deal.state == DealState.PROPOSED

    mgr.accept_terms(deal, "seller")
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED


def test_manager_proposed_funds_immediately():
    """Legacy behavior pinned: proposer=None is pre-agreed, no
    acceptance gate, the existing suite's flow is unchanged."""
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    assert deal.proposed_by is None
    assert deal.terms_acceptances == {}
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    assert deal.funded_at is not None


def test_propose_invalid_proposer_raises():
    mgr = DealManager(use_paypal=False)
    with pytest.raises(ValueError):
        mgr.propose(make_terms(CAMERA), proposer="stranger")


def test_reaccept_keeps_original_timestamp():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="seller")
    mgr.accept_terms(deal, "buyer")
    first = deal.terms_acceptances["buyer"]
    mgr.accept_terms(deal, "buyer")  # no-op
    assert deal.terms_acceptances["buyer"] == first


# --- decline ---

def test_decline_by_counterparty_is_terminal_no_money():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="seller")
    mgr.decline_terms(deal, "buyer")
    assert deal.state == DealState.DECLINED
    # No money fields were ever set.
    assert deal.paypal_auth_id is None
    assert deal.paypal_order_id is None
    assert deal.funded_at is None
    # Terminal: fund / accept / confirm all refuse.
    with pytest.raises(ValueError):
        mgr.fund(deal)
    with pytest.raises(ValueError):
        mgr.accept_terms(deal, "seller")
    with pytest.raises(ValueError):
        mgr.confirm_receipt(deal)
    assert deal.state == DealState.DECLINED


def test_decline_by_proposer_withdraws():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="buyer")
    mgr.decline_terms(deal, "buyer")
    assert deal.state == DealState.DECLINED
    assert deal.funded_at is None
    assert deal.paypal_auth_id is None


# --- invalid parties / wrong-state transitions ---

def test_invalid_party_strings_raise():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA), proposer="seller")
    with pytest.raises(ValueError):
        mgr.accept_terms(deal, "stranger")
    with pytest.raises(ValueError):
        mgr.decline_terms(deal, "stranger")
    with pytest.raises(ValueError):
        mgr.confirm_receipt(deal, party="stranger")
    # The failed calls changed nothing.
    assert deal.state == DealState.PROPOSED
    assert "stranger" not in deal.terms_acceptances
    assert deal.buyer_confirmed_at is None


def test_accept_and_decline_after_funding_raise():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    with pytest.raises(ValueError):
        mgr.accept_terms(deal, "buyer")
    with pytest.raises(ValueError):
        mgr.decline_terms(deal, "seller")
    assert deal.state == DealState.FUNDED


# --- confirm_receipt ---

def test_confirm_by_seller_raises():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    with pytest.raises(ValueError):
        mgr.confirm_receipt(deal, party="seller")
    assert deal.buyer_confirmed_at is None


def test_confirm_in_proposed_raises():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    assert deal.state == DealState.PROPOSED
    with pytest.raises(ValueError):
        mgr.confirm_receipt(deal)
    assert deal.buyer_confirmed_at is None


def test_double_confirm_keeps_first_timestamp():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.confirm_receipt(deal)
    first = deal.buyer_confirmed_at
    assert first is not None
    mgr.confirm_receipt(deal)  # no-op
    assert deal.buyer_confirmed_at == first
    assert deal.state == DealState.FUNDED  # confirmation changes no state


# --- confirmation as a Tier 0 release input ---

def test_confirmation_releases_when_address_unknown(
        audit_log, no_grok, stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    tracking = _clean_tracking_no_zip()

    decision, results = mgr.monitor(deal, tracking, days_since_funding=3)
    by_name = {r.name: r.status for r in results}
    assert by_name["address_match"] == CheckStatus.UNKNOWN
    assert by_name["delivered"] == CheckStatus.PASS
    assert decision.verdict != Verdict.RELEASE
    mgr.settle(deal, decision)
    assert deal.state != DealState.RELEASED

    mgr.confirm_receipt(deal)
    decision, _ = mgr.monitor(deal, tracking, days_since_funding=3)
    assert decision.verdict == Verdict.RELEASE
    assert decision.tier == 0 and decision.cost_usd == 0.0
    assert decision.confidence == 0.96
    assert "confirmed receipt" in decision.reasoning
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED


def test_confirmation_counts_as_delivery_evidence(
        audit_log, no_grok, stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.confirm_receipt(deal)

    # No tracking at all: the buyer's confirmation IS the delivery
    # evidence.
    decision, results = mgr.monitor(deal, {}, days_since_funding=3)
    by_name = {r.name: r for r in results}
    assert by_name["delivered"].status == CheckStatus.PASS
    assert "buyer confirmed receipt at" in by_name["delivered"].evidence
    assert decision.verdict == Verdict.RELEASE
    assert decision.tier == 0
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED


def test_confirmation_does_not_override_condition_dispute(
        audit_log, no_grok, stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.confirm_receipt(deal)
    decision, results = mgr.monitor(
        deal, _clean_tracking(), days_since_funding=3,
        messages=["it arrived damaged, screen is cracked"])
    by_name = {r.name: r.status for r in results}
    assert by_name["condition_clear"] == CheckStatus.FAIL
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.verdict != Verdict.RELEASE
    mgr.settle(deal, decision)
    assert deal.state == DealState.MONITORING


def test_evidence_carries_buyer_confirmation(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    ev = gather_evidence(deal, {}, days_since_funding=1)
    assert ev["buyer_confirmed"] is False
    assert ev["receipt_confirmed_at"] is None
    mgr.confirm_receipt(deal)
    ev = gather_evidence(deal, {}, days_since_funding=1)
    assert ev["buyer_confirmed"] is True
    assert ev["receipt_confirmed_at"] == deal.buyer_confirmed_at


# --- transition listeners ---

def test_transition_listeners_sequence_and_noop(audit_log, stable_seller):
    mgr = DealManager(use_paypal=False)
    events = []
    mgr.on_transition(
        lambda deal, old, new: events.append((deal.id, old, new)))

    deal = mgr.propose(make_terms(CAMERA), proposer="seller")
    mgr.accept_terms(deal, "buyer")  # no state change -> no event
    assert events == []

    mgr.fund(deal)
    mgr.confirm_receipt(deal)  # no state change -> no event
    mgr.confirm_receipt(deal)  # repeat no-op -> still no event
    assert events == [(deal.id, DealState.PROPOSED, DealState.FUNDED)]

    mgr.run_cycle(deal, _clean_tracking(), days_since_funding=3)
    assert deal.state == DealState.RELEASED
    assert events == [
        (deal.id, DealState.PROPOSED, DealState.FUNDED),
        (deal.id, DealState.FUNDED, DealState.MONITORING),
        (deal.id, DealState.MONITORING, DealState.RELEASED),
    ]
