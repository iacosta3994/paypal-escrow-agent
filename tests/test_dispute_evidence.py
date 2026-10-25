"""Track L5 — dispute evidence.

Parties attach evidence to an OPEN dispute (src/disputes.py;
storage on Deal.dispute_evidence). Submitted evidence feeds the
existing evidence/checklist path instead of dispute_filed being a
bare boolean:

- seller tracking proof merges into the tracking evidence, filling
  ONLY fields the carrier tracking lacks (carrier data always wins);
- buyer damage reports feed condition_clear — and stay on the
  record after the dispute flag itself is resolved;
- a buyer's settlement offer surfaces as the dispute_resolution
  check carrying the split amount in detail. NOTE: the Tier 0 SPLIT
  verdict rule that consumes that check is wired by the coordinator
  at integration — these tests assert the CHECK, not a SPLIT
  verdict.
"""
from datetime import datetime

import pytest

from src import evidence
from src.checklist import run_checklist
from src.deal_manager import DealManager
from src.disputes import KINDS, submit_evidence, timeline
from src.evidence import gather_evidence
from src.models import CheckStatus, DealState, Verdict

from .e2e_support import CAMERA, audit_log, make_terms, no_grok

AMOUNT = CAMERA[0]  # 50000


@pytest.fixture
def stable_seller(monkeypatch):
    """Pin the simulated seller completion rate into PASS territory
    so it never interferes with the dispute assertions."""
    monkeypatch.setattr(evidence.random, "uniform", lambda a, b: 0.95)


def _funded_deal(mgr):
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    return deal


def _disputed_deal(mgr):
    deal = _funded_deal(mgr)
    mgr.open_dispute(deal)
    return deal


def _cycle(mgr, deal, tracking, days):
    """One full monitor+settle pass. DealManager.run_cycle returns
    only the deal, so its two steps are driven directly here to let
    the tests assert on the verdict and the checklist results."""
    decision, results = mgr.monitor(deal, tracking,
                                    days_since_funding=days)
    mgr.settle(deal, decision)
    return decision, results


def _by_name(results):
    return {r.name: r for r in results}


def _clean_delivered_tracking():
    return {"number": "1ZDEMO001", "carrier": "UPS",
            "pickup_scan": "2026-11-02T10:00:00Z",
            "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
            "delivered_zip": "47172"}


# --- submit_evidence: kinds + record shape ---

def test_kinds_are_exactly_the_five():
    assert KINDS == {"tracking_proof", "damage_report",
                     "condition_proof", "settlement_offer", "note"}


def test_record_shape_and_defaults():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    rec = submit_evidence(deal, "buyer", "note", text="see photo",
                          file_ref="photos/1.jpg", data={"k": "v"})
    assert set(rec) == {"party", "kind", "text", "file_ref", "data",
                        "submitted_at"}
    assert rec["party"] == "buyer"
    assert rec["kind"] == "note"
    assert rec["text"] == "see photo"
    assert rec["file_ref"] == "photos/1.jpg"
    assert rec["data"] == {"k": "v"}
    datetime.fromisoformat(rec["submitted_at"])  # parses as ISO
    assert deal.dispute_evidence[-1] == rec

    bare = submit_evidence(deal, "seller", "condition_proof")
    assert bare["text"] == ""
    assert bare["file_ref"] is None
    assert bare["data"] == {}


# --- submit_evidence: validation matrix ---

def test_invalid_party_raises():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    with pytest.raises(ValueError):
        submit_evidence(deal, "stranger", "note", text="x")
    assert deal.dispute_evidence == []


def test_invalid_kind_raises():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    with pytest.raises(ValueError):
        submit_evidence(deal, "buyer", "affidavit", text="x")
    assert deal.dispute_evidence == []


def test_evidence_requires_open_dispute():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)
    assert deal.dispute_filed is False
    with pytest.raises(ValueError) as exc:
        submit_evidence(deal, "buyer", "note", text="x")
    assert str(exc.value) == "no open dispute on this deal"
    # Also after the dispute is resolved.
    mgr.open_dispute(deal)
    mgr.resolve_dispute(deal)
    with pytest.raises(ValueError):
        submit_evidence(deal, "seller", "note", text="x")
    assert deal.dispute_evidence == []


@pytest.mark.parametrize("data", [
    None, {}, {"tracking_number": ""}, {"tracking_number": "   "},
    {"carrier": "UPS"},
])
def test_tracking_proof_requires_tracking_number(data):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    with pytest.raises(ValueError):
        submit_evidence(deal, "seller", "tracking_proof", data=data)
    assert deal.dispute_evidence == []


def test_settlement_offer_seller_raises():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    with pytest.raises(ValueError):
        submit_evidence(deal, "seller", "settlement_offer",
                        data={"split_release_cents": 20000})
    assert deal.dispute_evidence == []


@pytest.mark.parametrize("data", [
    None, {}, {"split_release_cents": None},
    {"split_release_cents": "20000"}, {"split_release_cents": 100.5},
    {"split_release_cents": True}, {"split_release_cents": 0},
    {"split_release_cents": -5}, {"split_release_cents": AMOUNT},
    {"split_release_cents": AMOUNT + 1},
])
def test_settlement_offer_amount_validation(data):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    with pytest.raises(ValueError):
        submit_evidence(deal, "buyer", "settlement_offer", data=data)
    assert deal.dispute_evidence == []


def test_settlement_offer_boundary_amounts_pass():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "settlement_offer",
                    data={"split_release_cents": 1})
    submit_evidence(deal, "buyer", "settlement_offer",
                    data={"split_release_cents": AMOUNT - 1})
    assert len(deal.dispute_evidence) == 2


# --- timeline ---

def test_timeline_order_and_copy_semantics():
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "seller", "tracking_proof",
                    data={"tracking_number": "1ZX", "seq": 1})
    submit_evidence(deal, "buyer", "damage_report", text="cracked")
    submit_evidence(deal, "buyer", "note", text="photos attached")

    tl = timeline(deal)
    assert [r["kind"] for r in tl] == ["tracking_proof",
                                       "damage_report", "note"]
    assert tl == deal.dispute_evidence
    assert tl is not deal.dispute_evidence

    # Mutating the copy — list, record, and nested data — leaves the
    # deal's stored evidence untouched.
    tl.append({"party": "buyer", "kind": "note"})
    tl[0]["party"] = "buyer"
    tl[0]["data"]["seq"] = 99
    assert len(deal.dispute_evidence) == 3
    assert deal.dispute_evidence[0]["party"] == "seller"
    assert deal.dispute_evidence[0]["data"]["seq"] == 1


# --- gather_evidence: dispute_evidence carried + tracking merge ---

def test_gather_evidence_carries_dispute_evidence(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "damage_report", text="dented")
    submit_evidence(deal, "seller", "note", text="shipped Monday")
    ev = gather_evidence(deal, {}, days_since_funding=2)
    assert len(ev["dispute_evidence"]) == 2
    assert [r["kind"] for r in ev["dispute_evidence"]] == [
        "damage_report", "note"]
    assert ev["dispute_evidence"][0]["text"] == "dented"
    assert ev["dispute_evidence"] is not deal.dispute_evidence


def test_proof_fills_missing_number_carrier_and_pickup(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "seller", "tracking_proof", data={
        "tracking_number": "1ZPROOF", "carrier": "FedEx",
        "pickup_scan": "2026-11-03T09:00:00Z"})
    carrier_tracking = {"stalled_days": 3}
    ev = gather_evidence(deal, carrier_tracking, days_since_funding=3)
    t = ev["tracking"]
    assert t["number"] == "1ZPROOF"
    assert t["carrier"] == "FedEx"
    assert t["pickup_scan"] == "2026-11-03T09:00:00Z"
    assert t["stalled_days"] == 3
    # The caller's tracking dict is not mutated by the merge.
    assert carrier_tracking == {"stalled_days": 3}


def test_carrier_tracking_wins_over_proof(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "seller", "tracking_proof", data={
        "tracking_number": "1ZPROOF", "carrier": "FedEx",
        "pickup_scan": "2026-11-03T09:00:00Z"})
    ev = gather_evidence(deal, {
        "number": "1ZCARRIER", "carrier": "UPS",
        "pickup_scan": "2026-11-02T08:00:00Z",
    }, days_since_funding=3)
    t = ev["tracking"]
    assert t["number"] == "1ZCARRIER"
    assert t["carrier"] == "UPS"
    # The proof's pickup_scan does NOT override the carrier's.
    assert t["pickup_scan"] == "2026-11-02T08:00:00Z"


def test_proof_never_supplies_delivery_facts(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "seller", "tracking_proof", data={
        "tracking_number": "1ZPROOF", "stalled_days": 99,
        "delivered": True, "delivered_zip": "99999"})
    ev = gather_evidence(deal, {
        "number": "1ZCARRIER", "carrier": "UPS", "stalled_days": 7,
    }, days_since_funding=7)
    t = ev["tracking"]
    assert t["stalled_days"] == 7  # untouched by evidence
    assert "delivered" not in t
    assert "delivered_zip" not in t


def test_latest_seller_proof_wins_and_buyer_proof_ignored(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "seller", "tracking_proof",
                    data={"tracking_number": "1ZOLD"})
    submit_evidence(deal, "buyer", "tracking_proof",
                    data={"tracking_number": "1ZBUYER"})
    submit_evidence(deal, "seller", "tracking_proof",
                    data={"tracking_number": "1ZNEW"})
    ev = gather_evidence(deal, {}, days_since_funding=1)
    assert ev["tracking"]["number"] == "1ZNEW"

    mgr2 = DealManager(use_paypal=False)
    deal2 = _disputed_deal(mgr2)
    submit_evidence(deal2, "buyer", "tracking_proof",
                    data={"tracking_number": "1ZBUYER"})
    ev2 = gather_evidence(deal2, {}, days_since_funding=1)
    assert "number" not in ev2["tracking"]


# --- E2E visible change #1: tracking proof rescues a stalled deal ---

def test_tracking_proof_rescues_stalled_deal(audit_log, no_grok,
                                            stable_seller):
    stalled = lambda: {"number": "1ZDEMO002", "carrier": "UPS",
                       "stalled_days": 7}

    # Before: the same deal with no proof REFUNDs — the stalled
    # label is a Tier 0 hard fail on pickup_confirmed.
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)
    decision, results = _cycle(mgr, deal, stalled(),
                               days=7)
    assert _by_name(results)["pickup_confirmed"].status == \
        CheckStatus.FAIL
    assert decision.verdict == Verdict.REFUND
    assert deal.state == DealState.REFUNDED

    # After: a fresh identical deal, but the seller submits tracking
    # proof (number + pickup scan) as dispute evidence. The proof
    # fills the missing pickup scan, the hard fail clears, and the
    # cycle INVESTIGATEs instead of refunding.
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)
    mgr.open_dispute(deal)
    submit_evidence(deal, "seller", "tracking_proof", data={
        "tracking_number": "1ZDEMO002",
        "pickup_scan": "2026-11-03T09:00:00Z"})
    decision, results = _cycle(mgr, deal, stalled(),
                               days=7)
    assert _by_name(results)["pickup_confirmed"].status == \
        CheckStatus.PASS
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.verdict != Verdict.REFUND
    assert deal.state == DealState.MONITORING


# --- E2E visible change #2: damage evidence outlives the flag ---

def test_damage_evidence_outlives_dispute_flag(audit_log, no_grok,
                                               stable_seller):
    # Deal A: dispute opened and resolved with NO evidence — the
    # clean delivered deal releases exactly as before.
    mgr = DealManager(use_paypal=False)
    deal_a = _funded_deal(mgr)
    mgr.open_dispute(deal_a)
    mgr.resolve_dispute(deal_a)
    decision_a, results_a = _cycle(mgr, deal_a,
                                   _clean_delivered_tracking(), days=3)
    assert _by_name(results_a)["condition_clear"].status == \
        CheckStatus.UNKNOWN
    assert decision_a.verdict == Verdict.RELEASE
    assert deal_a.state == DealState.RELEASED

    # Deal B: identical, except the buyer submitted a damage report
    # while the dispute was open. Resolving the dispute clears the
    # flag but NOT the evidence: condition_clear FAILs citing the
    # dispute evidence and the deal does not release.
    mgr = DealManager(use_paypal=False)
    deal_b = _funded_deal(mgr)
    mgr.open_dispute(deal_b)
    submit_evidence(deal_b, "buyer", "damage_report",
                    text="screen arrived cracked")
    mgr.resolve_dispute(deal_b)
    assert deal_b.dispute_filed is False
    decision_b, results_b = _cycle(mgr, deal_b,
                                   _clean_delivered_tracking(), days=3)
    cond_b = _by_name(results_b)["condition_clear"]
    assert cond_b.status == CheckStatus.FAIL
    assert "buyer dispute evidence: screen arrived cracked" in \
        cond_b.evidence
    # The checklist difference is exactly the condition check.
    assert _by_name(results_b)["no_dispute"].status == \
        _by_name(results_a)["no_dispute"].status == CheckStatus.PASS
    assert _by_name(results_b)["delivered"].status == \
        _by_name(results_a)["delivered"].status == CheckStatus.PASS
    assert decision_b.verdict == Verdict.INVESTIGATE
    assert decision_b.verdict != Verdict.RELEASE
    assert deal_b.state == DealState.MONITORING


def test_damage_report_fallback_text_and_seller_ignored(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "damage_report")  # no text
    ev = gather_evidence(deal, _clean_delivered_tracking(),
                         days_since_funding=3)
    cond = _by_name(run_checklist(deal, ev))["condition_clear"]
    assert cond.status == CheckStatus.FAIL
    assert "buyer dispute evidence: damage reported via dispute" in \
        cond.evidence

    mgr2 = DealManager(use_paypal=False)
    deal2 = _disputed_deal(mgr2)
    submit_evidence(deal2, "seller", "damage_report",
                    text="buyer says it is broken")
    ev2 = gather_evidence(deal2, _clean_delivered_tracking(),
                          days_since_funding=3)
    cond2 = _by_name(run_checklist(deal2, ev2))["condition_clear"]
    assert cond2.status == CheckStatus.UNKNOWN  # seller report: no-op


# --- dispute_resolution check (settlement offers) ---
# The SPLIT verdict wiring for this check lands at integration (the
# coordinator's Tier 0 rule in decision_engine); here we assert the
# check itself: status, evidence string, and detail payload.

def _resolution(deal):
    ev = gather_evidence(deal, {}, days_since_funding=1)
    return _by_name(run_checklist(deal, ev))["dispute_resolution"]


def test_settlement_offer_passes_with_exact_detail(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "settlement_offer",
                    text="keep it for a partial refund",
                    data={"split_release_cents": 20000})
    r = _resolution(deal)
    assert r.status == CheckStatus.PASS
    assert r.detail == {"split_release_cents": 20000}
    assert r.detail["split_release_cents"] == 20000
    assert r.evidence == ("buyer accepts split settlement: release "
                          "20000 of 50000 cents to close the dispute")


def test_settlement_offer_latest_wins(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "settlement_offer",
                    data={"split_release_cents": 15000})
    submit_evidence(deal, "buyer", "settlement_offer",
                    data={"split_release_cents": 25000})
    r = _resolution(deal)
    assert r.status == CheckStatus.PASS
    assert r.detail["split_release_cents"] == 25000


def test_dispute_open_without_offer_is_unknown(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _disputed_deal(mgr)
    submit_evidence(deal, "buyer", "note", text="still waiting")
    r = _resolution(deal)
    assert r.status == CheckStatus.UNKNOWN
    assert r.evidence == "dispute open; no accepted settlement offer"
    assert r.detail == {}


def test_no_open_dispute_is_unknown(stable_seller):
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)
    r = _resolution(deal)
    assert r.status == CheckStatus.UNKNOWN
    assert r.evidence == "no open dispute"

    # An offer accepted while the dispute was open stops counting
    # once the dispute is closed.
    mgr.open_dispute(deal)
    submit_evidence(deal, "buyer", "settlement_offer",
                    data={"split_release_cents": 20000})
    mgr.resolve_dispute(deal)
    r = _resolution(deal)
    assert r.status == CheckStatus.UNKNOWN
    assert r.evidence == "no open dispute"
