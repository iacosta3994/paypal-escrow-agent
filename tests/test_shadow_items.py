"""Per-item shadow reads (2026-10-26): the forest's answers broken
out per checklist item, recorded as an ADDITIVE audit field
(Decision.shadow_items) and rendered by the per-item detail view.

The invariants under test are the shadow-mode ones: the items are a
pure derivation from outputs the forest already produced (no extra
inference), Laya stays advisory (a stubbed shadow carrying items
still cannot decide), and the agreement marker is honest about the
forest's known limits — a read on an UNKNOWN fact is "unverifiable",
never an agreement.
"""
import json

import pytest

from src import audit, config, laya_forest, notifications
from src.app import app
from src.decision_engine import _attach_shadow, adjudicate
from src.laya_forest import FACT_CHECKS, item_reads
from src.laya_questions import FACTS
from src.models import (CheckResult, CheckStatus, Deal, DealTerms,
                        Decision, Verdict)


def _results(**statuses):
    return [CheckResult(name, status, evidence=f"ev:{name}")
            for name, status in statuses.items()]


def _item(items, check):
    return next(i for i in items if i["check"] == check)


# --- item_reads: the pure derivation ---

def test_item_reads_cover_every_fact_in_canonical_order():
    facts = {f: "UNKNOWN" for f in FACTS}
    items = item_reads(facts, [], [])
    assert [i["fact"] for i in items] == list(FACTS)
    assert {i["check"] for i in items} == set(FACT_CHECKS.values())
    # No tree read anything: every read is unknown / unverifiable.
    assert all(i["laya_read"] == "unknown" for i in items)
    assert all(i["agreement"] == "unverifiable" for i in items)


def test_item_reads_agree_when_laya_tracks_tier0():
    results = _results(pickup_confirmed=CheckStatus.PASS,
                       delivered=CheckStatus.PASS,
                       no_dispute=CheckStatus.PASS)
    facts = {"SHIPPED": "YES", "DELIVERED": "YES",
             "BUYER_COMPLAINT": "NO"}
    trees = [
        {"tree": "fulfillment", "shipped": "yes", "delivered": "yes"},
        {"tree": "condition", "complaint": "no"},
        {"tree": "risk", "shipped": "yes"},
    ]
    items = item_reads(facts, results, trees)
    shipped = _item(items, "pickup_confirmed")
    assert shipped["tier0_status"] == "pass"
    assert shipped["evidence"] == "ev:pickup_confirmed"
    assert shipped["laya_read"] == "approve"
    assert shipped["agreement"] == "agree"
    assert {r["tree"] for r in shipped["laya_answers"]} == \
        {"fulfillment", "risk"}
    # The complaint polarity: no dispute (check PASS) + Laya "no"
    # is an approving, agreeing read — not a disagreement.
    dispute = _item(items, "no_dispute")
    assert dispute["laya_read"] == "approve"
    assert dispute["agreement"] == "agree"


def test_item_reads_disagreement_is_marked_not_hidden():
    """The documented both-NO blind spot shape: the fact says the
    item never shipped (Tier 0 FAIL) while the model answers yes.
    The item must show approve-vs-fail as a DISAGREEMENT."""
    results = _results(pickup_confirmed=CheckStatus.FAIL)
    facts = {"SHIPPED": "NO"}
    trees = [{"tree": "fulfillment", "shipped": "yes"},
             {"tree": "risk", "shipped": "yes"}]
    item = _item(item_reads(facts, results, trees), "pickup_confirmed")
    assert item["tier0_status"] == "fail"
    assert item["laya_read"] == "approve"
    assert item["agreement"] == "disagree"


def test_item_reads_dispute_filed_polarity():
    """Dispute filed: no_dispute FAILs, the complaint fact is YES,
    and a Laya "yes" DISAPPROVES the check — agreeing with Tier 0."""
    results = _results(no_dispute=CheckStatus.FAIL)
    facts = {"BUYER_COMPLAINT": "YES"}
    trees = [{"tree": "condition", "complaint": "yes"}]
    item = _item(item_reads(facts, results, trees), "no_dispute")
    assert item["laya_read"] == "disapprove"
    assert item["agreement"] == "agree"


def test_item_reads_unknown_fact_is_unverifiable():
    """A forced answer on an UNKNOWN fact is a guess: whatever the
    model said, the marker must not claim agreement."""
    results = _results(condition_clear=CheckStatus.UNKNOWN)
    facts = {"CONDITION_OK": "UNKNOWN"}
    trees = [{"tree": "condition", "condition_ok": "yes"}]
    item = _item(item_reads(facts, results, trees), "condition_clear")
    assert item["laya_read"] == "approve"
    assert item["agreement"] == "unverifiable"


def test_item_reads_conflicting_trees_are_unknown():
    results = _results(pickup_confirmed=CheckStatus.PASS)
    facts = {"SHIPPED": "YES"}
    trees = [{"tree": "fulfillment", "shipped": "yes"},
             {"tree": "risk", "shipped": "no"}]
    item = _item(item_reads(facts, results, trees), "pickup_confirmed")
    assert item["laya_read"] == "unknown"
    assert item["agreement"] == "unverifiable"
    assert len(item["laya_answers"]) == 2


# --- shadow() populates items; _attach_shadow persists them ---

class _OracleRouter:
    """Answers each question by reading its fact line in the state
    (same oracle as tests/test_laya_questions.py)."""

    def predict(self, state, questions, model=None):
        from src import laya_questions as lq
        facts = {}
        for line in state.splitlines():
            k, v = line.split(": ")
            facts[k.strip()] = v.strip().rstrip(".").lower()
        return {"answers": {
            qid: {"choice": facts.get(lq.QUESTIONS[qid].fact, "unknown")}
            for qid in questions}}


def test_shadow_populates_items_from_tree_outputs():
    forest = laya_forest.LayaForest()
    forest._router = _OracleRouter()
    deal = Deal(id="t", terms=DealTerms(
        amount_cents=100, currency="USD", item_description="x",
        buyer_email="b@x", seller_email="s@x", delivery_zip="47172"))
    results = _results(
        pickup_confirmed=CheckStatus.PASS, delivered=CheckStatus.PASS,
        address_match=CheckStatus.PASS, within_window=CheckStatus.PASS,
        amount_match=CheckStatus.PASS, no_dispute=CheckStatus.PASS,
        condition_clear=CheckStatus.UNKNOWN,
        seller_history=CheckStatus.PASS)
    shadow = forest.shadow(deal, results)
    assert len(shadow.items) == len(FACTS)
    delivered = _item(list(shadow.items), "delivered")
    assert delivered["laya_read"] == "approve"
    assert delivered["agreement"] == "agree"
    # The oracle guesses on the UNKNOWN condition fact: unverifiable.
    condition = _item(list(shadow.items), "condition_clear")
    assert condition["agreement"] == "unverifiable"


def test_attach_shadow_persists_items_and_never_decides():
    items = ({"fact": "SHIPPED", "check": "pickup_confirmed",
              "tier0_status": "pass", "evidence": "ev",
              "laya_read": "approve", "laya_answers": [],
              "agreement": "agree"},)
    shadow_decision = Decision(deal_id="d1", verdict=Verdict.RELEASE,
                               confidence=1.0, reasoning="stub",
                               tier=1, cost_usd=0.0)
    stub = laya_forest.ShadowResult(decision=shadow_decision,
                                    votes={}, items=items)
    real = Decision(deal_id="d1", verdict=Verdict.INVESTIGATE,
                    confidence=0.0, reasoning="real", tier=1,
                    cost_usd=0.0)
    _attach_shadow(real, stub)
    # The verdict is untouched; the items ride along as audit data.
    assert real.verdict == Verdict.INVESTIGATE
    assert real.shadow_items == [dict(items[0])]
    round_tripped = json.loads(json.dumps(real.to_dict()))
    assert round_tripped["shadow_items"] == [dict(items[0])]


def test_adjudicate_with_stubbed_shadow_records_items():
    from unittest.mock import patch

    from src.checklist import run_checklist
    from src.evidence import gather_evidence

    deal = Deal(id="d2", terms=DealTerms(
        amount_cents=50000, currency="USD", item_description="camera",
        buyer_email="b@x.com", seller_email="s@x.com",
        delivery_zip="47172"))
    tracking = {"number": "1Z1", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "in_transit": True}
    results = run_checklist(
        deal, gather_evidence(deal, tracking, days_since_funding=3))
    items = ({"fact": "DELIVERED", "check": "delivered",
              "tier0_status": "unknown", "evidence": "ev",
              "laya_read": "unknown", "laya_answers": [],
              "agreement": "unverifiable"},)
    stub = laya_forest.ShadowResult(
        decision=Decision(deal_id="d2", verdict=Verdict.RELEASE,
                          confidence=0.6667, reasoning="stub",
                          tier=1, cost_usd=0.0),
        votes={"fulfillment": Verdict.RELEASE}, items=items)
    with patch.object(laya_forest, "forest_shadow", return_value=stub):
        d = adjudicate(deal, results)
    assert d.verdict == Verdict.INVESTIGATE  # shadow never decides
    assert d.shadow_items == [dict(items[0])]


# --- the per-item detail view ---

@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTIFICATIONS_PATH",
                        str(tmp_path / "notifications.jsonl"))
    monkeypatch.setattr(config, "AUDIT_LOG_PATH",
                        str(tmp_path / "audit.jsonl"))
    from src.deal_manager import DealManager
    mgr = DealManager(use_paypal=False)
    mgr.on_transition(notifications.transition_listener)
    old = app.config["DEAL_MANAGER"]
    app.config["DEAL_MANAGER"] = mgr
    try:
        yield app.test_client(), mgr
    finally:
        app.config["DEAL_MANAGER"] = old


def _deal(mgr):
    from .e2e_support import CAMERA, make_terms
    return mgr.propose(make_terms(CAMERA))


def test_detail_view_renders_shadow_items(ui):
    client, mgr = ui
    deal = _deal(mgr)
    audit.record(Decision(
        deal_id=deal.id, verdict=Verdict.INVESTIGATE, confidence=0.0,
        reasoning="escalated", tier=1, cost_usd=0.0,
        shadow_items=[
            {"fact": "SHIPPED", "check": "pickup_confirmed",
             "tier0_status": "fail", "evidence": "no pickup in 9 days",
             "laya_read": "approve",
             "laya_answers": [{"tree": "risk", "question": "shipped",
                               "answer": "yes"}],
             "agreement": "disagree"},
            {"fact": "DELIVERED", "check": "delivered",
             "tier0_status": "unknown", "evidence": "no delivery scan",
             "laya_read": "unknown", "laya_answers": [],
             "agreement": "unverifiable"},
        ]))
    resp = client.get(f"/deal/{deal.id}/detail")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "pickup_confirmed" in page
    assert "no pickup in 9 days" in page
    assert "disapprove" not in page  # this record has none
    assert "approve" in page and "disagree" in page
    assert "unverifiable" in page
    assert "can never move money" in page  # the shadow disclaimer


def test_detail_view_empty_state_and_unknown_deal(ui):
    client, mgr = ui
    deal = _deal(mgr)
    resp = client.get(f"/deal/{deal.id}/detail")
    assert resp.status_code == 200
    assert "No monitoring pass" in resp.get_data(as_text=True)
    assert client.get("/deal/deal-nope/detail").status_code == 404


def test_detail_view_audit_only_deal(ui):
    client, _ = ui
    audit.record(Decision(
        deal_id="deal-ghost", verdict=Verdict.INVESTIGATE,
        confidence=0.0, reasoning="from another process", tier=1,
        cost_usd=0.0,
        shadow_items=[
            {"fact": "AMOUNT_MATCH", "check": "amount_match",
             "tier0_status": "pass", "evidence": "authorized == deal",
             "laya_read": "approve", "laya_answers": [],
             "agreement": "agree"}]))
    resp = client.get("/deal/deal-ghost/detail")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "amount_match" in page and "audit record" in page
