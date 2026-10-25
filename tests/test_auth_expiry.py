"""Authorization-expiry surfacing (improvement plan S5, 2026-10-24).

PayPal authorizations are fully honored ~3 days and expire at 29.
A quiet deal must not discover that at day 30. Thresholds: warn at
20 days (audit flag), escalate at 25 (audit + deal ESCALATED), each
recorded once; settled deals never surface; PayPal's own
expiration_time, when read back at funding, drives the levels.
"""
from datetime import datetime, timedelta, timezone

from src import auth_expiry, paypal_client
from src.deal_manager import DealManager
from src.evidence import TrackingSimulator
from src.models import DealState

from .e2e_support import (CAMERA, audit_log, audit_rows_for,  # noqa: F401
                          make_terms, no_grok)  # noqa: F401

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _aged_deal(days: float, **kw):
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    deal.funded_at = (NOW - timedelta(days=days)).isoformat()
    for k, v in kw.items():
        setattr(deal, k, v)
    return mgr, deal


def test_status_levels_by_age():
    cases = [(5, "ok"), (19.9, "ok"), (20, "warn"), (24.9, "warn"),
             (25, "escalate"), (28.9, "escalate"), (29, "expired"),
             (40, "expired")]
    for days, level in cases:
        _, deal = _aged_deal(days)
        st = auth_expiry.expiry_status(deal, NOW)
        assert st["level"] == level, f"age {days}: {st}"
        assert abs(st["days_left"] - (29 - days)) < 0.01


def test_status_none_for_unfunded_and_settled():
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))  # never funded
    assert auth_expiry.expiry_status(deal, NOW) is None
    _, deal2 = _aged_deal(30)
    deal2.state = DealState.RELEASED
    assert auth_expiry.expiry_status(deal2, NOW) is None
    deal2.state = DealState.REFUNDED
    assert auth_expiry.expiry_status(deal2, NOW) is None


def test_paypal_expiration_time_drives_levels():
    """A young authorization whose REAL expiry is close escalates on
    days-left, not on age (re-authorizations, partial holds, etc.)."""
    _, deal = _aged_deal(1, paypal_auth_expires_at=(
        NOW + timedelta(days=3)).isoformat())
    assert auth_expiry.expiry_status(deal, NOW)["level"] == "escalate"
    _, deal2 = _aged_deal(1, paypal_auth_expires_at=(
        NOW + timedelta(days=8)).isoformat())
    assert auth_expiry.expiry_status(deal2, NOW)["level"] == "warn"
    _, deal3 = _aged_deal(1, paypal_auth_expires_at=(
        NOW - timedelta(days=1)).isoformat())
    assert auth_expiry.expiry_status(deal3, NOW)["level"] == "expired"


def test_warn_records_once_and_deal_keeps_monitoring(audit_log, no_grok):
    mgr, deal = _aged_deal(21)
    tracking = TrackingSimulator("clean_delivery").advance(1)
    mgr.monitor(deal, tracking, 21, now=NOW)
    rows = [r for r in audit_rows_for(deal.id)
            if "Authorization aging" in r["reasoning"]]
    assert len(rows) == 1
    assert "21.0 days" in rows[0]["reasoning"]
    assert deal.state == DealState.MONITORING
    mgr.monitor(deal, tracking, 21, now=NOW)  # again: no duplicate
    rows = [r for r in audit_rows_for(deal.id)
            if "Authorization aging" in r["reasoning"]]
    assert len(rows) == 1


def test_escalate_escalates_and_sticks(audit_log, no_grok):
    from src.models import Decision, Verdict
    mgr, deal = _aged_deal(26)
    deal.state = DealState.MONITORING
    mgr.check_auth_expiry(deal, NOW)
    assert deal.state == DealState.ESCALATED
    rows = [r for r in audit_rows_for(deal.id)
            if "Authorization expiry imminent" in r["reasoning"]]
    assert len(rows) == 1
    # re-checking does not duplicate the escalation record
    mgr.check_auth_expiry(deal, NOW)
    rows = [r for r in audit_rows_for(deal.id)
            if "Authorization expiry imminent" in r["reasoning"]]
    assert len(rows) == 1
    # and an inconclusive adjudication afterwards must not resurrect
    # the deal back to MONITORING (settle's sticky-escalation guard)
    mgr.settle(deal, Decision(deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                               confidence=0.0, reasoning="still unclear",
                               key_evidence=[], tier=0, cost_usd=0.0))
    assert deal.state == DealState.ESCALATED


def test_funding_stamps_timestamps_and_reads_back_expiry(monkeypatch):
    """fund() stamps funded_at and, for PayPal deals, enriches the
    deal with PayPal's own create/expiration times."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    monkeypatch.setattr(paypal_client, "create_authorization",
                        lambda *a, **k: {
                            "order_id": "ORD-EXP",
                            "authorization_id": "AUTH-EXP",
                            "status": "CREATED",
                            "approve_url": None})
    monkeypatch.setattr(paypal_client, "get_authorization",
                        lambda auth_id: {
                            "id": auth_id, "status": "CREATED",
                            "create_time": "2026-11-24T12:00:00Z",
                            "expiration_time": "2026-12-23T12:00:00Z"})
    mgr.fund(deal)
    assert deal.funded_at is not None
    assert deal.paypal_auth_created_at == "2026-11-24T12:00:00Z"
    assert deal.paypal_auth_expires_at == "2026-12-23T12:00:00Z"


def test_readback_failure_is_best_effort(monkeypatch):
    """If the authorization read-back fails, funding still succeeds
    and expiry surfacing falls back to funded_at."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    monkeypatch.setattr(paypal_client, "create_authorization",
                        lambda *a, **k: {
                            "order_id": "ORD-NR",
                            "authorization_id": "AUTH-NR",
                            "status": "CREATED", "approve_url": None})

    def _boom(auth_id):
        raise RuntimeError("read-back down")
    monkeypatch.setattr(paypal_client, "get_authorization", _boom)
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    assert deal.paypal_auth_expires_at is None
    st = auth_expiry.expiry_status(deal)  # funded just now -> ok
    assert st is not None and st["level"] == "ok"
