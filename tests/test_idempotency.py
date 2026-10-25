"""Part E — idempotency / replay safety.

Re-running monitor/settle cycles must never duplicate PayPal calls:
one deal, at most one capture or one void, ever. The manager-level
tests count calls through spies on paypal_client (they test
DealManager's discipline, not PayPal); the live replay test at the
bottom proves it against the real sandbox.
"""
import pytest

from src import paypal_client
from src.deal_manager import DealManager
from src.evidence import TrackingSimulator
from src.models import DealState, Decision, Verdict

from .e2e_support import (CAMERA, KEYBOARD, TEST_CARD_SOURCE, audit_log,
                          audit_rows_for, held_cleanup, make_terms,
                          no_grok, order_captures, pp_auth,
                          requires_sandbox)


def _decision(deal, verdict):
    return Decision(deal_id=deal.id, verdict=verdict, confidence=0.97,
                    reasoning="idempotency test", tier=0)


def test_settle_calls_paypal_exactly_once(monkeypatch):
    calls = {"capture": 0, "void": 0}

    def fake_capture(auth_id):
        calls["capture"] += 1
        return {"id": "CAP-TEST", "status": "COMPLETED"}

    def fake_void(auth_id):
        calls["void"] += 1
        return {"status": "VOIDED"}

    monkeypatch.setattr(paypal_client, "capture", fake_capture)
    monkeypatch.setattr(paypal_client, "void", fake_void)

    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    deal.state = DealState.FUNDED
    deal.paypal_auth_id = "AUTH-TEST-1"

    mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    assert calls == {"capture": 1, "void": 0}

    # Every replay is refused BEFORE any PayPal call.
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.RELEASE))
    with pytest.raises(ValueError):
        mgr.settle(deal, _decision(deal, Verdict.REFUND))
    assert calls == {"capture": 1, "void": 0}


def test_run_cycle_replay_is_refused(audit_log):
    """Full cycle in sim mode, then the same cycle again: monitor()
    refuses settled deals, no second decision is audited."""
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    sim = TrackingSimulator("clean_delivery")
    sim.advance(1)
    delivered = sim.advance(2)

    mgr.run_cycle(deal, delivered, days_since_funding=3)
    assert deal.state == DealState.RELEASED
    assert len(audit_rows_for(deal.id)) == 1

    with pytest.raises(ValueError):
        mgr.run_cycle(deal, delivered, days_since_funding=3)
    assert len(audit_rows_for(deal.id)) == 1  # unchanged


@requires_sandbox
def test_double_settle_after_real_capture_refused(audit_log, no_grok,
                                                  held_cleanup):
    """Live replay: capture really happens once; a second settle is
    refused locally and PayPal still shows exactly one capture."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)

    sim = TrackingSimulator("clean_delivery")
    sim.advance(1)
    decision, _ = mgr.monitor(deal, sim.advance(2), days_since_funding=3)
    assert decision.verdict == Verdict.RELEASE
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED
    assert len(order_captures(deal.paypal_order_id)) == 1

    with pytest.raises(ValueError):
        mgr.settle(deal, decision)
    assert len(order_captures(deal.paypal_order_id)) == 1
    assert pp_auth(deal.paypal_auth_id)["status"] == "CAPTURED"
