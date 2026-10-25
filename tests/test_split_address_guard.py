"""Regression guard: partial splits require a verified address.

Finding (A5 corpus growth engine, 2026-10-24): the Tier 0
partial-shipment split rule guarded amount / no-dispute / condition
but never checked address_match, so a partial delivery to a WRONG
ZIP — or with no delivered ZIP recorded at all — split-released the
delivered fraction with the address never verified. Two probe cases
in scripts/generate_corpus.py (probe_partial_wrong_zip,
probe_partial_no_delivered_zip) caught it and were excluded from
the admitted corpus rather than enshrined.

Fix: the partial-split rule now requires address_match PASS, with
one exception consistent with the buyer-confirmation rule above it
in decision_engine.py — a buyer who confirmed receipt is the
address authority. These tests pin all three branches.
"""
import pytest

from src.models import DealState, Verdict

from .e2e_support import CAMERA, audit_log, make_terms, no_grok  # noqa: F401
from src.deal_manager import DealManager

pytestmark = pytest.mark.usefixtures("audit_log", "no_grok")

DELIVERED = {"number": "1ZGUARD01", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z",
             "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
             "delivered_zip": "47172"}


def _run(tracking, confirm=False):
    mgr = DealManager()  # simulated funding; adjudication is real
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    if confirm:
        mgr.confirm_receipt(deal)
    decision, results = mgr.monitor(deal, tracking, 3, None)
    mgr.settle(deal, decision)
    return deal, decision, {r.name: r for r in results}


def test_partial_wrong_zip_investigates_never_splits():
    """1 of 2 items delivered to ZIP 99999 (terms: 47172): the
    delivered fraction must NOT be paid out on an unverified
    address — INVESTIGATE, deal still monitoring, no split."""
    t = {**DELIVERED, "delivered_zip": "99999",
         "items_expected": 2, "items_delivered": 1}
    deal, decision, results = _run(t)
    assert results["address_match"].status.value == "fail"
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.split_release_cents is None
    assert deal.state == DealState.MONITORING


def test_partial_no_delivered_zip_investigates_never_splits():
    """1 of 2 items delivered with no delivered ZIP recorded:
    address UNKNOWN is not verification — INVESTIGATE, no split."""
    t = {k: v for k, v in DELIVERED.items() if k != "delivered_zip"}
    t.update({"items_expected": 2, "items_delivered": 1})
    deal, decision, results = _run(t)
    assert results["address_match"].status.value == "unknown"
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.split_release_cents is None
    assert deal.state == DealState.MONITORING


def test_partial_correct_zip_still_splits():
    """Control: the guard must not break the legitimate partial
    split — 1 of 2 items at the agreed ZIP still settles 50/50."""
    t = {**DELIVERED, "items_expected": 2, "items_delivered": 1}
    deal, decision, _ = _run(t)
    assert decision.verdict == Verdict.SPLIT
    assert decision.split_release_cents == CAMERA[0] // 2
    assert deal.state == DealState.SPLIT_SETTLED


def test_partial_buyer_confirmed_splits_without_zip():
    """The documented exception: buyer confirmation makes the
    buyer the address authority, so a confirmed partial delivery
    splits even when tracking recorded no delivered ZIP."""
    t = {k: v for k, v in DELIVERED.items() if k != "delivered_zip"}
    t.update({"items_expected": 2, "items_delivered": 1})
    deal, decision, _ = _run(t, confirm=True)
    assert decision.verdict == Verdict.SPLIT
    assert decision.split_release_cents == CAMERA[0] // 2
    assert deal.state == DealState.SPLIT_SETTLED
