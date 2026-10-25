"""A3 — post-settlement outcome feedback.

Outcomes label settled deals after the fact (a RELEASE later
disputed, a REFUND contested, a SPLIT accepted or rejected, or a
clean close) as their own append-only audit record type referencing
the original decision. Covered here: recording every kind,
kind<->verdict validation, retrieval of the labeled decision,
aggregate counts over a mixed set, unknown / unsettled rejection,
idempotent duplicates, and the append-only invariant (the original
decision bytes are never touched). All audit paths use the shared
``audit_log`` fixture (temp file), exactly like the rest of the
suite — the real audit.log.jsonl is never written.
"""
import json

import pytest

from src import audit, outcomes
from src.deal_manager import DealManager
from src.models import DealState, Decision, Verdict
from src.outcomes import (OutcomeKind, OutcomeSource, aggregate_outcomes,
                          labeled_decision, read_outcomes, record_outcome)

from .e2e_support import CAMERA, KEYBOARD, audit_log, make_terms  # noqa: F401


def _audit_decision(deal_id, verdict, decided_at, **kw):
    """Append a decision record the way DealManager.monitor does."""
    d = Decision(deal_id=deal_id, verdict=verdict,
                 confidence=kw.pop("confidence", 0.95),
                 reasoning=kw.pop("reasoning", "test decision"),
                 tier=0, cost_usd=0.0, **kw)
    d.decided_at = decided_at
    audit.record(d)
    return d


def _settled(deal_id="deal-rel-1", verdict=Verdict.RELEASE,
             decided_at="2026-11-05T14:00:00+00:00"):
    _audit_decision(deal_id, verdict, decided_at)
    return deal_id


# --- recording: every outcome kind ---

def test_record_clean_on_release(audit_log):
    _settled()
    rec = record_outcome("deal-rel-1", OutcomeKind.CLEAN,
                         OutcomeSource.BUYER, "item kept, no issues",
                         occurred_at="2026-11-20T09:00:00+00:00")
    assert rec["record_type"] == "outcome"
    assert rec["deal_id"] == "deal-rel-1"
    assert rec["kind"] == "clean"
    assert rec["source"] == "buyer"
    assert rec["note"] == "item kept, no issues"
    assert rec["verdict"] == "release"
    assert rec["decision_decided_at"] == "2026-11-05T14:00:00+00:00"
    assert rec["occurred_at"] == "2026-11-20T09:00:00+00:00"
    assert rec["recorded_at"]  # stamped at append time
    assert rec["outcome_id"].startswith("outcome-")


def test_record_release_disputed_later(audit_log):
    _settled()
    rec = record_outcome("deal-rel-1", "release_disputed_later",
                         "buyer", "buyer says item never arrived")
    assert rec["kind"] == "release_disputed_later"
    assert rec["verdict"] == "release"


def test_record_refund_contested(audit_log):
    _settled("deal-ref-1", Verdict.REFUND, "2026-11-06T10:00:00+00:00")
    rec = record_outcome("deal-ref-1", OutcomeKind.REFUND_CONTESTED,
                         "seller", "seller insists item was shipped")
    assert rec["kind"] == "refund_contested"
    assert rec["verdict"] == "refund"
    assert rec["source"] == "seller"


def test_record_clean_on_refund(audit_log):
    _settled("deal-ref-1", Verdict.REFUND, "2026-11-06T10:00:00+00:00")
    rec = record_outcome("deal-ref-1", "CLEAN", "operator",
                         "buyer confirmed money back")
    assert rec["kind"] == "clean"  # kind names accept any case
    assert rec["verdict"] == "refund"


def test_record_split_accepted(audit_log):
    _settled("deal-split-1", Verdict.SPLIT, "2026-11-07T10:00:00+00:00")
    rec = record_outcome("deal-split-1", "split_accepted", "seller",
                         "seller accepted the split as final")
    assert rec["kind"] == "split_accepted"
    assert rec["verdict"] == "split"


def test_record_split_rejected(audit_log):
    _settled("deal-split-1", Verdict.SPLIT, "2026-11-07T10:00:00+00:00")
    rec = record_outcome("deal-split-1", OutcomeKind.SPLIT_REJECTED,
                         OutcomeSource.OPERATOR,
                         "buyer rejected the split, escalating")
    assert rec["kind"] == "split_rejected"
    assert rec["verdict"] == "split"


def test_record_clean_on_split(audit_log):
    _settled("deal-split-1", Verdict.SPLIT, "2026-11-07T10:00:00+00:00")
    rec = record_outcome("deal-split-1", "clean", "operator")
    assert rec["kind"] == "clean"
    assert rec["verdict"] == "split"
    assert rec["note"] == ""


def test_outcome_written_to_audit_trail(audit_log):
    _settled()
    record_outcome("deal-rel-1", "clean", "operator", "ok")
    rows = audit.read_all()
    assert len(rows) == 2  # decision + outcome, same trail
    assert rows[0].get("record_type") in (None, "decision")
    assert rows[1]["record_type"] == "outcome"
    # Outcome rows carry no cost: spend totals stay decision-only.
    assert audit.total_spend() == 0.0


def test_outcome_links_settling_decision_not_earlier_investigate(
        audit_log):
    """Monitoring leaves INVESTIGATE rows before the settling one;
    the outcome must reference the settling decision."""
    _audit_decision("deal-multi", Verdict.INVESTIGATE,
                    "2026-11-03T09:00:00+00:00")
    _audit_decision("deal-multi", Verdict.INVESTIGATE,
                    "2026-11-04T09:00:00+00:00")
    _audit_decision("deal-multi", Verdict.RELEASE,
                    "2026-11-05T14:00:00+00:00")
    rec = record_outcome("deal-multi", "clean", "operator")
    assert rec["decision_decided_at"] == "2026-11-05T14:00:00+00:00"
    assert rec["verdict"] == "release"


def test_explicit_decided_at_reference(audit_log):
    _audit_decision("deal-multi", Verdict.RELEASE,
                    "2026-11-05T14:00:00+00:00")
    rec = record_outcome("deal-multi", "clean", "buyer",
                         decided_at="2026-11-05T14:00:00+00:00")
    assert rec["decision_decided_at"] == "2026-11-05T14:00:00+00:00"


# --- validation: kind / source / kind<->verdict ---

def test_invalid_kind_rejected(audit_log):
    _settled()
    with pytest.raises(ValueError, match="kind must be one of"):
        record_outcome("deal-rel-1", "exploded", "buyer")
    assert read_outcomes() == []  # nothing appended


def test_invalid_source_rejected(audit_log):
    _settled()
    with pytest.raises(ValueError, match="source must be one of"):
        record_outcome("deal-rel-1", "clean", "carrier")
    assert read_outcomes() == []


@pytest.mark.parametrize("kind", ["split_accepted", "split_rejected",
                                  "refund_contested"])
def test_kind_verdict_mismatch_rejected_on_release(audit_log, kind):
    _settled()
    with pytest.raises(ValueError, match="cannot label a 'release'"):
        record_outcome("deal-rel-1", kind, "buyer")
    assert read_outcomes() == []


def test_release_disputed_rejected_on_refund(audit_log):
    _settled("deal-ref-1", Verdict.REFUND, "2026-11-06T10:00:00+00:00")
    with pytest.raises(ValueError, match="cannot label a 'refund'"):
        record_outcome("deal-ref-1", "release_disputed_later", "buyer")


def test_split_kinds_rejected_on_split_mismatch_free(audit_log):
    """Control: split kinds DO attach to a split verdict."""
    _settled("deal-split-1", Verdict.SPLIT, "2026-11-07T10:00:00+00:00")
    record_outcome("deal-split-1", "split_accepted", "buyer")
    assert len(read_outcomes("deal-split-1")) == 1


def test_decided_at_pointing_at_investigate_rejected(audit_log):
    _audit_decision("deal-multi", Verdict.INVESTIGATE,
                    "2026-11-03T09:00:00+00:00")
    _audit_decision("deal-multi", Verdict.RELEASE,
                    "2026-11-05T14:00:00+00:00")
    with pytest.raises(ValueError, match="does not settle"):
        record_outcome("deal-multi", "clean", "buyer",
                       decided_at="2026-11-03T09:00:00+00:00")


def test_decided_at_unknown_rejected(audit_log):
    _settled()
    with pytest.raises(ValueError, match="no decision record"):
        record_outcome("deal-rel-1", "clean", "buyer",
                       decided_at="2026-01-01T00:00:00+00:00")


# --- validation: unknown / unsettled deals ---

def test_unknown_deal_rejected(audit_log):
    with pytest.raises(ValueError, match="unknown deal"):
        record_outcome("deal-ghost", "clean", "operator")
    assert audit.read_all() == []


def test_unsettled_investigate_only_rejected(audit_log):
    _audit_decision("deal-open", Verdict.INVESTIGATE,
                    "2026-11-03T09:00:00+00:00")
    with pytest.raises(ValueError, match="has not settled"):
        record_outcome("deal-open", "clean", "operator")
    assert read_outcomes() == []


def test_unsettled_deal_object_rejected(audit_log):
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    with pytest.raises(ValueError, match="has not settled"):
        record_outcome(deal.id, "clean", "operator", deal=deal)


def test_deal_object_id_mismatch_rejected(audit_log):
    _settled()
    mgr = DealManager(use_paypal=False)
    other = mgr.propose(make_terms(CAMERA))
    with pytest.raises(ValueError, match="does not match"):
        record_outcome("deal-rel-1", "clean", "operator", deal=other)


# --- the Deal-object path: state is authoritative ---

def test_record_via_settled_deal_object(audit_log):
    """A deal settled by direct settle() (decision never audited)
    still takes an outcome when the Deal object proves settlement;
    with no decision row to reference, decision_decided_at is None
    and the verdict comes from the terminal state."""
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(KEYBOARD))
    mgr.fund(deal)
    mgr.settle(deal, Decision(deal_id=deal.id, verdict=Verdict.REFUND,
                              confidence=0.95, reasoning="never shipped",
                              tier=0))
    assert deal.state == DealState.REFUNDED
    assert audit.read_all() == []  # settle() itself writes no audit row
    rec = record_outcome(deal.id, "refund_contested", "seller",
                         "contests the refund", deal=deal)
    assert rec["verdict"] == "refund"
    assert rec["decision_decided_at"] is None
    labeled = labeled_decision(deal.id)
    assert labeled["verdict"] == "refund"
    assert labeled["outcomes"] == [rec]
    assert labeled["is_labeled"] is True


def test_full_manager_flow_monitor_settle_then_outcome(audit_log):
    """End to end in sim mode: monitor audits the RELEASE decision,
    settle lands RELEASED, and the outcome links that decision."""
    mgr = DealManager(use_paypal=False)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    tracking = {"number": "1ZOUT1", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "delivered": True,
                "delivered_at": "2026-11-05T14:00:00Z",
                "delivered_zip": "47172"}
    decision, _ = mgr.monitor(deal, tracking, days_since_funding=5)
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED
    rec = record_outcome(deal.id, "release_disputed_later", "buyer",
                         "buyer opened a chargeback two weeks later",
                         deal=deal)
    assert rec["verdict"] == "release"
    assert rec["decision_decided_at"] == decision.decided_at


# --- retrieval: the labeled decision ---

def test_labeled_decision_links_decision_and_outcomes(audit_log):
    _audit_decision("deal-multi", Verdict.INVESTIGATE,
                    "2026-11-03T09:00:00+00:00")
    _audit_decision("deal-multi", Verdict.RELEASE,
                    "2026-11-05T14:00:00+00:00")
    record_outcome("deal-multi", "clean", "operator", "all good")
    record_outcome("deal-multi", "release_disputed_later", "buyer",
                   "later disputed after all")
    labeled = labeled_decision("deal-multi")
    assert labeled["deal_id"] == "deal-multi"
    assert len(labeled["decisions"]) == 2
    assert labeled["decision"]["verdict"] == "release"
    assert labeled["decision"]["decided_at"] == \
        "2026-11-05T14:00:00+00:00"
    assert labeled["verdict"] == "release"
    assert [o["kind"] for o in labeled["outcomes"]] == [
        "clean", "release_disputed_later"]
    assert all(o["decision_decided_at"] ==
               "2026-11-05T14:00:00+00:00"
               for o in labeled["outcomes"])
    assert labeled["is_labeled"] is True


def test_labeled_decision_unlabeled_settled_deal(audit_log):
    _settled()
    labeled = labeled_decision("deal-rel-1")
    assert labeled["verdict"] == "release"
    assert labeled["outcomes"] == []
    assert labeled["is_labeled"] is False


def test_labeled_decision_unknown_deal_is_none(audit_log):
    assert labeled_decision("deal-ghost") is None


def test_read_outcomes_filters_by_deal(audit_log):
    _settled("deal-a", Verdict.RELEASE, "2026-11-05T14:00:00+00:00")
    _settled("deal-b", Verdict.REFUND, "2026-11-06T10:00:00+00:00")
    record_outcome("deal-a", "clean", "operator")
    record_outcome("deal-b", "clean", "operator")
    assert len(read_outcomes()) == 2
    assert [o["deal_id"] for o in read_outcomes("deal-a")] == ["deal-a"]


# --- aggregates over a mixed set ---

def _mixed_set():
    _settled("rel-clean", Verdict.RELEASE, "2026-11-01T10:00:00+00:00")
    _settled("rel-disputed", Verdict.RELEASE,
             "2026-11-02T10:00:00+00:00")
    _settled("rel-both", Verdict.RELEASE, "2026-11-03T10:00:00+00:00")
    _settled("ref-contested", Verdict.REFUND,
             "2026-11-04T10:00:00+00:00")
    _settled("ref-clean", Verdict.REFUND, "2026-11-05T10:00:00+00:00")
    _settled("split-ok", Verdict.SPLIT, "2026-11-06T10:00:00+00:00")
    _settled("split-no", Verdict.SPLIT, "2026-11-07T10:00:00+00:00")
    _settled("unlabeled", Verdict.RELEASE, "2026-11-08T10:00:00+00:00")
    record_outcome("rel-clean", "clean", "buyer")
    record_outcome("rel-disputed", "release_disputed_later", "buyer")
    record_outcome("rel-both", "clean", "operator")
    record_outcome("rel-both", "release_disputed_later", "seller",
                   "disputed after an initially clean close")
    record_outcome("ref-contested", "refund_contested", "seller")
    record_outcome("ref-clean", "clean", "operator")
    record_outcome("split-ok", "split_accepted", "buyer")
    record_outcome("split-no", "split_rejected", "seller")


def test_aggregate_counts_mixed_set(audit_log):
    _mixed_set()
    agg = aggregate_outcomes()
    assert agg["total_outcomes"] == 8
    assert agg["labeled_deals"] == 7  # 'unlabeled' carries none
    assert agg["by_kind"] == {
        "clean": 3, "release_disputed_later": 2, "refund_contested": 1,
        "split_accepted": 1, "split_rejected": 1}
    rel = agg["by_verdict"]["release"]
    assert rel["clean"] == 2
    assert rel["release_disputed_later"] == 2
    assert rel["refund_contested"] == 0
    assert rel["total"] == 4
    ref = agg["by_verdict"]["refund"]
    assert ref["refund_contested"] == 1
    assert ref["clean"] == 1
    assert ref["total"] == 2
    split = agg["by_verdict"]["split"]
    assert split["split_accepted"] == 1
    assert split["split_rejected"] == 1
    assert split["total"] == 2


def test_aggregate_empty_log(audit_log):
    agg = aggregate_outcomes()
    assert agg["total_outcomes"] == 0
    assert agg["labeled_deals"] == 0
    for verdict in ("release", "refund", "split"):
        assert agg["by_verdict"][verdict]["total"] == 0
    assert all(n == 0 for n in agg["by_kind"].values())


# --- duplicates: idempotent by identity ---

def test_duplicate_identical_outcome_is_idempotent(audit_log):
    """Chosen duplicate policy (see src/outcomes.py): recording the
    identical outcome again returns the stored record and appends
    nothing — retries can never double-count a label."""
    _settled()
    first = record_outcome("deal-rel-1", "clean", "buyer", "all good",
                           occurred_at="2026-11-20T09:00:00+00:00")
    size = audit_log.stat().st_size
    # A retry even carries a different timestamp: still identical.
    again = record_outcome("deal-rel-1", "clean", "buyer", "all good",
                           occurred_at="2026-11-21T09:00:00+00:00")
    assert again == first
    assert audit_log.stat().st_size == size
    assert len(read_outcomes()) == 1
    assert aggregate_outcomes()["total_outcomes"] == 1


def test_same_kind_different_note_is_a_new_outcome(audit_log):
    _settled()
    record_outcome("deal-rel-1", "release_disputed_later", "buyer",
                   "chargeback opened")
    record_outcome("deal-rel-1", "release_disputed_later", "buyer",
                   "buyer also reported damage on arrival")
    assert len(read_outcomes("deal-rel-1")) == 2


def test_same_kind_different_source_is_a_new_outcome(audit_log):
    _settled()
    record_outcome("deal-rel-1", "clean", "buyer", "fine by me")
    record_outcome("deal-rel-1", "clean", "seller", "fine by me")
    assert len(read_outcomes("deal-rel-1")) == 2


# --- append-only invariant ---

def test_append_only_original_decision_bytes_unchanged(audit_log):
    _settled()
    _audit_decision("deal-other", Verdict.REFUND,
                    "2026-11-06T10:00:00+00:00")
    before = audit_log.read_bytes()
    record_outcome("deal-rel-1", "clean", "operator", "closed clean")
    record_outcome("deal-rel-1", "release_disputed_later", "buyer",
                   "spoke too soon")
    after = audit_log.read_bytes()
    assert after.startswith(before)  # original lines are a strict prefix
    new_lines = after[len(before):].decode().strip().splitlines()
    assert len(new_lines) == 2
    assert all(json.loads(line)["record_type"] == "outcome"
               for line in new_lines)
    # And the original decision record still reads back untouched.
    decisions = [r for r in audit.read_all()
                 if outcomes.is_decision_row(r)]
    assert decisions[0]["verdict"] == "release"
    assert "kind" not in decisions[0]
