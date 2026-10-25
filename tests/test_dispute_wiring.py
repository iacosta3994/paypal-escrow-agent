"""Part D — dispute wiring.

Regression tests for the hardcoded dispute_filed=False in
gather_evidence(): a genuinely-open dispute could never reach the
checklist through DealManager, so the no_dispute protection was dead
code in the product flow. Disputes are now real deal state
(DealManager.open_dispute), evidence gathering reads them, and an
open dispute blocks release even when delivery evidence is perfect.
"""
import pytest

from src import evidence
from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.evidence import TrackingSimulator, gather_evidence
from src.models import DealState, Verdict

from .e2e_support import CAMERA, audit_log, make_terms, no_grok


@pytest.fixture
def stable_seller(monkeypatch):
    """Pin the simulated seller completion rate into PASS territory so
    the only FAIL in these tests is the dispute itself."""
    monkeypatch.setattr(evidence.random, "uniform", lambda a, b: 0.95)


def _delivered_tracking():
    sim = TrackingSimulator("clean_delivery")
    sim.advance(1)
    return sim.advance(2)  # day-3 stage: delivered, ZIP matches


def test_evidence_reads_dispute_from_deal_state(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    ev = gather_evidence(deal, _delivered_tracking(), 3)
    assert ev["dispute_filed"] is False

    mgr.open_dispute(deal)
    ev = gather_evidence(deal, _delivered_tracking(), 3)
    assert ev["dispute_filed"] is True
    by_name = {r.name: r.status.value for r in run_checklist(deal, ev)}
    assert by_name["no_dispute"] == "fail"
    # Everything else about this delivery is release-grade.
    assert by_name["delivered"] == "pass"
    assert by_name["address_match"] == "pass"


def test_open_dispute_blocks_release(audit_log, no_grok, stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.open_dispute(deal)
    decision, results = mgr.monitor(deal, _delivered_tracking(),
                                    days_since_funding=3)
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.verdict != Verdict.RELEASE
    mgr.settle(deal, decision)
    assert deal.state == DealState.MONITORING  # held, not released


def test_resolved_dispute_allows_release(audit_log, no_grok,
                                         stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.open_dispute(deal)
    decision, _ = mgr.monitor(deal, _delivered_tracking(),
                              days_since_funding=3)
    assert decision.verdict == Verdict.INVESTIGATE
    mgr.settle(deal, decision)

    mgr.resolve_dispute(deal)
    decision, _ = mgr.monitor(deal, _delivered_tracking(),
                              days_since_funding=3)
    assert decision.verdict == Verdict.RELEASE
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED
