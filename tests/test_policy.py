"""Policy-as-data (Wave 1 / A2, 2026-10-24).

Two kinds of pins:

1. DEFAULTS: every Policy default equals the literal that was
   hardcoded in src/ before A2. If someone retunes a default, this
   file fails on purpose — behavior changes must be deliberate.
2. OVERRIDES: injecting a Policy (or setting ESCROW_POLICY_* env)
   changes behavior in the expected place with NO code edit —
   auth-expiry thresholds, escalation cycles, checklist thresholds,
   shadow-routing bounds.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import auth_expiry
from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.decision_engine import adjudicate
from src.models import (CheckResult, CheckStatus, Deal, DealState,
                        DealTerms, Decision, Verdict)
from src.policy import DEFAULT_POLICY, Policy

from .e2e_support import CAMERA, audit_log, make_terms, no_grok  # noqa: F401

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


# --- 1. defaults reproduce the pre-A2 literals EXACTLY ---

def test_default_policy_values_are_the_old_literals():
    p = Policy()
    assert p.default_window_days == 14          # was models.DealTerms
    assert p.auth_expiry_days == 29             # was auth_expiry.EXPIRY_DAYS
    assert p.auth_warn_at_days == 20            # was auth_expiry.WARN_AT_DAYS
    assert p.auth_escalate_at_days == 25        # was auth_expiry.ESCALATE_AT_DAYS
    assert p.investigate_escalation_cycles == 3  # was deal_manager settle "n >= 3"
    assert p.stall_days == 5                    # was checklist ">= 5" (x2)
    assert p.seller_completion_threshold == 0.9  # was checklist ">= 0.9"
    assert p.tier2_min_confidence == 0.75       # was decision_engine "< 0.75"
    assert p.shadow_max_unknowns == 2           # was adjudicate "unknowns <= 2"
    assert p.shadow_max_fails == 1              # was adjudicate "len(fails) <= 1"
    assert DEFAULT_POLICY == Policy()  # no env overrides in the test env


def test_derived_days_left_thresholds():
    assert Policy().auth_days_left_warn == 9       # 29 - 20
    assert Policy().auth_days_left_escalate == 4   # 29 - 25


def test_auth_expiry_backcompat_aliases_match_default_policy():
    assert auth_expiry.EXPIRY_DAYS == 29
    assert auth_expiry.WARN_AT_DAYS == 20
    assert auth_expiry.ESCALATE_AT_DAYS == 25


def test_deal_terms_default_window_comes_from_policy():
    terms = DealTerms(
        amount_cents=100, currency="USD", item_description="x",
        buyer_email="b@example.com", seller_email="s@example.com",
        delivery_zip="47172")
    assert terms.window_days == 14


def test_env_override_mechanism(monkeypatch):
    monkeypatch.setenv("ESCROW_POLICY_AUTH_WARN_AT_DAYS", "18")
    monkeypatch.setenv("ESCROW_POLICY_TIER2_MIN_CONFIDENCE", "0.5")
    p = Policy.from_env()
    assert p.auth_warn_at_days == 18
    assert p.tier2_min_confidence == 0.5
    assert p.auth_escalate_at_days == 25  # untouched fields keep defaults


# --- 2. overrides change behavior in the expected place ---

def _aged_deal(days: float):
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    deal.funded_at = (NOW - timedelta(days=days)).isoformat()
    return deal


def test_auth_expiry_threshold_override():
    deal = _aged_deal(15)
    # Default policy: 15 days is still "ok" (warn at 20).
    assert auth_expiry.expiry_status(deal, NOW)["level"] == "ok"
    # Override: warn at 10, escalate at 14 -> the same deal escalates.
    p = Policy().with_overrides(auth_warn_at_days=10,
                                auth_escalate_at_days=14)
    assert auth_expiry.expiry_status(deal, NOW, policy=p)["level"] == "escalate"


def test_auth_expiry_override_flows_through_deal_manager(audit_log):
    p = Policy().with_overrides(auth_escalate_at_days=10)
    mgr = DealManager(policy=p)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    deal.funded_at = (NOW - timedelta(days=12)).isoformat()
    mgr.check_auth_expiry(deal, NOW)
    assert deal.state == DealState.ESCALATED


def _investigate(deal):
    return Decision(deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                    confidence=0.0, reasoning="unclear",
                    key_evidence=[], tier=0, cost_usd=0.0)


def test_escalation_cycles_default_is_three():
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, _investigate(deal))
    assert deal.state == DealState.MONITORING
    mgr.settle(deal, _investigate(deal))
    assert deal.state == DealState.MONITORING
    mgr.settle(deal, _investigate(deal))
    assert deal.state == DealState.ESCALATED


def test_escalation_cycles_override():
    mgr = DealManager(policy=Policy().with_overrides(
        investigate_escalation_cycles=1))
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, _investigate(deal))
    assert deal.state == DealState.ESCALATED


def _check(results, name):
    return {r.name: r for r in results}[name]


def test_stall_days_override():
    deal = Deal(id="p1", terms=make_terms(CAMERA))
    ev = {"tracking": {"number": "1Z", "carrier": "UPS", "stalled_days": 3},
          "days_since_funding": 3, "authorized_cents": CAMERA[0],
          "dispute_filed": False, "seller_completion_rate": 0.95}
    # Default stall threshold 5: 3 stalled days is still UNKNOWN.
    assert _check(run_checklist(deal, ev),
                  "pickup_confirmed").status == CheckStatus.UNKNOWN
    # Override stall threshold to 3: the same evidence hard-fails.
    p = Policy().with_overrides(stall_days=3)
    results = run_checklist(deal, ev, policy=p)
    assert _check(results, "pickup_confirmed").status == CheckStatus.FAIL
    assert _check(results, "in_transit").status == CheckStatus.FAIL


def test_seller_completion_threshold_override():
    deal = Deal(id="p2", terms=make_terms(CAMERA))
    ev = {"tracking": {}, "days_since_funding": 1,
          "authorized_cents": CAMERA[0], "dispute_filed": False,
          "seller_completion_rate": 0.85}
    assert _check(run_checklist(deal, ev),
                  "seller_history").status == CheckStatus.FAIL
    p = Policy().with_overrides(seller_completion_threshold=0.8)
    assert _check(run_checklist(deal, ev, policy=p),
                  "seller_history").status == CheckStatus.PASS


def test_shadow_routing_bounds_override(no_grok):
    """A Tier-0-unsettled case with 3 unknowns: default policy sends
    it toward Tier 2 (here stubbed unavailable -> 'Hard case');
    widening shadow_max_unknowns keeps it on the shadow path."""
    deal = Deal(id="p3", terms=make_terms(CAMERA))
    results = [
        CheckResult("delivered", CheckStatus.UNKNOWN, "no scan", 0.5),
        CheckResult("address_match", CheckStatus.UNKNOWN, "no zip", 0.5),
        CheckResult("in_transit", CheckStatus.UNKNOWN, "no data", 0.5),
        CheckResult("amount_match", CheckStatus.PASS, "ok", 1.0),
    ]
    d = adjudicate(deal, results)
    assert "Hard case" in d.reasoning
    p = Policy().with_overrides(shadow_max_unknowns=5)
    d2 = adjudicate(deal, results, policy=p)
    assert "shadow-only" in d2.reasoning
    assert d2.verdict == Verdict.INVESTIGATE  # sensor still never decides


def test_policy_is_immutable_and_with_overrides_copies():
    p = Policy()
    with pytest.raises(Exception):
        p.auth_warn_at_days = 1  # frozen dataclass
    p2 = p.with_overrides(auth_warn_at_days=1)
    assert p.auth_warn_at_days == 20 and p2.auth_warn_at_days == 1
