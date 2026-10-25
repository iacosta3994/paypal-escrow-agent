"""Part C — the settle() guard.

Regression tests for the verification probe of 2026-10-24, which
re-settled a RELEASED deal as REFUNDED without any error: settle()
accepted any decision on any deal in any state. Settlement must now
be refused, loudly, on already-settled deals — and a PayPal deal with
no recorded authorization must refuse rather than flip state while
no money moves.
"""
import pytest

from src.deal_manager import DealManager
from src.models import DealState, Decision, Verdict

from .e2e_support import CAMERA, make_terms


def _decision(deal, verdict):
    return Decision(deal_id=deal.id, verdict=verdict, confidence=0.97,
                    reasoning="settle-guard test", tier=0)


def _released_sim_deal():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert deal.state == DealState.RELEASED
    return mgr, deal


def test_resettle_same_verdict_refused():
    mgr, deal = _released_sim_deal()
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert deal.state == DealState.RELEASED


def test_resettle_opposite_verdict_refused():
    """The exact 2026-10-24 probe: RELEASED -> settle(REFUND)."""
    mgr, deal = _released_sim_deal()
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.REFUND))
    assert deal.state == DealState.RELEASED


def test_settle_refunded_deal_refused():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, _decision(deal, Verdict.REFUND))
    assert deal.state == DealState.REFUNDED
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert deal.state == DealState.REFUNDED


def test_settle_before_funding_refused():
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    assert deal.state == DealState.PROPOSED
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert deal.state == DealState.PROPOSED


def test_paypal_settle_without_authorization_refused():
    """A PayPal deal whose authorization id is missing must NOT flip
    to RELEASED while no capture can run (the old silent failure)."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    deal.state = DealState.FUNDED  # funded record, authorization lost
    assert deal.paypal_auth_id is None
    with pytest.raises(RuntimeError):
        mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert deal.state == DealState.FUNDED
