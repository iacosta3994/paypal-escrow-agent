"""Share links, milestone deals, and the Atlas Glass shell
(2026-10-26, user-flow spec).

Token discipline under test: when a deal issues per-party share
links, the token matching the party gates BOTH the party page and
the action POSTs; a missing/wrong/cross-party token is a clean
403 and never renders the other party's view or performs their
actions. Milestones under test: editor validation (shares sum to
the total), the board's live states, per-milestone settlement
through the UI, and a disputed milestone resolving as a split
without unwinding its settled sibling.
"""
import pytest

from src import config, milestones as milestones_mod, notifications
from src.app import MANAGER, app
from src.deal_manager import DealManager
from src.models import DealState, DealTerms, Milestone

from .e2e_support import CAMERA, LAPTOP, make_terms


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTIFICATIONS_PATH",
                        str(tmp_path / "notifications.jsonl"))
    monkeypatch.setattr(config, "AUDIT_LOG_PATH",
                        str(tmp_path / "audit.jsonl"))
    mgr = DealManager(use_paypal=False)
    mgr.on_transition(notifications.transition_listener)
    old = app.config["DEAL_MANAGER"]
    app.config["DEAL_MANAGER"] = mgr
    try:
        yield app.test_client(), mgr
    finally:
        app.config["DEAL_MANAGER"] = old


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTIFICATIONS_PATH",
                        str(tmp_path / "notifications.jsonl"))
    monkeypatch.setattr(config, "AUDIT_LOG_PATH",
                        str(tmp_path / "audit.jsonl"))
    old = app.config["DEAL_MANAGER"]
    app.config["DEAL_MANAGER"] = MANAGER
    try:
        yield app.test_client()
    finally:
        app.config["DEAL_MANAGER"] = old


def _linked_deal(mgr):
    return mgr.propose(make_terms(CAMERA), proposer="seller",
                       share_links=True)


def _milestones():
    return [
        Milestone(id="m1", title="Delivery",
                  requirement="Delivered to the buyer's ZIP",
                  check="delivered", amount_cents=90000,
                  due_date="2026-11-05"),
        Milestone(id="m2", title="Handover call",
                  requirement="Seller walks the buyer through setup",
                  check=None, amount_cents=30000,
                  due_date="2026-11-09"),
    ]


def _milestone_deal(mgr):
    return mgr.propose(make_terms(LAPTOP), proposer="seller",
                       milestones=_milestones(), share_links=True)


# --- share-link tokens ---

def test_valid_share_link_opens_party_page(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    for party in ("buyer", "seller"):
        resp = client.get(mgr.share_link(deal, party))
        assert resp.status_code == 200
        assert f"You are the {party}" in resp.get_data(as_text=True)


def test_missing_wrong_and_cross_tokens_are_403(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    base = f"/deal/{deal.id}"
    assert client.get(f"{base}?party=buyer").status_code == 403
    assert client.get(
        f"{base}?party=buyer&k=wrong-token").status_code == 403
    # The seller's token does not open the buyer's page.
    assert client.get(
        f"{base}?party=buyer&k={deal.share_tokens['seller']}"
    ).status_code == 403
    # ... and a 403 never leaks the other party's view.
    page = client.get(f"{base}?party=buyer").get_data(as_text=True)
    assert "You are the" not in page


def test_post_requires_matching_token(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    url = f"/deal/{deal.id}/action"
    # No token: refused, nothing recorded.
    resp = client.post(url, data={"party": "buyer",
                                  "action": "accept"})
    assert resp.status_code == 403
    assert "buyer" not in deal.terms_acceptances
    # Buyer token presented as the seller: refused (cross-party).
    resp = client.post(url, data={
        "party": "seller", "action": "decline",
        "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 403
    assert deal.state == DealState.PROPOSED
    # The right token works.
    resp = client.post(url, data={
        "party": "buyer", "action": "accept",
        "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    assert "buyer" in deal.terms_acceptances


def test_proposer_sees_waiting_state_and_counterparty_link(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    page = client.get(mgr.share_link(deal, "seller")).get_data(
        as_text=True)
    assert "Waiting for counterparty" in page
    # Links render HTML-escaped (& -> &amp;) inside the copy field.
    buyer_link = mgr.share_link(deal, "buyer").replace("&", "&amp;")
    seller_link = mgr.share_link(deal, "seller").replace("&", "&amp;")
    assert buyer_link in page
    assert seller_link in page  # their own link
    # The counterparty's page never shows the proposer's link.
    buyer_page = client.get(mgr.share_link(deal, "buyer")).get_data(
        as_text=True)
    assert "Waiting for counterparty" not in buyer_page


def test_withdraw_unaccepted_proposal(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "seller", "action": "withdraw",
        "k": deal.share_tokens["seller"]})
    assert resp.status_code == 302
    assert deal.state == DealState.WITHDRAWN
    # Terminal: no actions remain for either party.
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "buyer", "action": "accept",
        "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 403


def test_withdraw_after_acceptance_is_refused(ui):
    client, mgr = ui
    deal = _linked_deal(mgr)
    mgr.accept_terms(deal, "buyer")
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "seller", "action": "withdraw",
        "k": deal.share_tokens["seller"]})
    assert resp.status_code == 403
    assert deal.state == DealState.PROPOSED


# --- milestone editor (propose form) ---

_MS_FORM = {
    "proposer": "seller", "item_description": "Laptop",
    "amount": "1200.00", "currency": "USD",
    "buyer_email": "buyer@example.com",
    "seller_email": "seller@example.com",
    "delivery_zip": "47172", "window_days": "14",
    "release_conditions": "delivery confirmed to buyer address",
    "structure": "milestones",
    "ms_title": ["Delivery", "Condition verified"],
    "ms_amount": ["900.00", "300.00"],
    "ms_due": ["2026-11-05", "2026-11-09"],
    "ms_check": ["delivered", "condition_clear"],
    "ms_requirement": ["Delivered to the buyer's ZIP",
                       "Condition as described"],
}


def test_milestone_editor_creates_structured_deal(ui):
    client, mgr = ui
    resp = client.post("/propose", data=_MS_FORM)
    assert resp.status_code == 302
    deal = next(iter(mgr.deals.values()))
    assert [m.title for m in deal.milestones] == [
        "Delivery", "Condition verified"]
    assert [m.amount_cents for m in deal.milestones] == [
        90000, 30000]
    assert deal.milestones[0].check == "delivered"
    # The agreement list mirrors the milestones.
    assert [r["check"] for r in deal.terms.requirements] == [
        "delivered", "condition_clear"]
    # The board renders on the party page with due dates + states.
    page = client.get(mgr.share_link(deal, "buyer")).get_data(
        as_text=True)
    assert "Milestones" in page
    assert "2026-11-05" in page and "pending" in page


def test_milestone_editor_rejects_shares_not_summing(ui):
    client, mgr = ui
    form = {**_MS_FORM, "ms_amount": ["900.00", "200.00"]}
    resp = client.post("/propose", data=form)
    assert resp.status_code == 400
    assert "sum" in resp.get_data(as_text=True)
    assert mgr.deals == {}


# --- milestone flow through the UI ---

def _funded_milestone_deal(client, mgr):
    deal = _milestone_deal(mgr)
    mgr.accept_terms(deal, "buyer")
    mgr.fund(deal)
    return deal


def test_manual_milestone_settles_on_buyer_confirmation(ui):
    client, mgr = ui
    deal = _funded_milestone_deal(client, mgr)
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "buyer", "action": "confirm_milestone",
        "milestone_id": "m2", "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    m2 = deal.milestones[1]
    assert m2.state.value == "released"
    assert m2.released_cents == 30000
    # The deal is still open: m1 has not settled.
    assert deal.state in (DealState.FUNDED, DealState.MONITORING)


def test_seller_cannot_confirm_milestone(ui):
    client, mgr = ui
    deal = _funded_milestone_deal(client, mgr)
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "seller", "action": "confirm_milestone",
        "milestone_id": "m2", "k": deal.share_tokens["seller"]})
    assert resp.status_code == 403
    assert deal.milestones[1].state.value == "funded-held"


def test_disputed_milestone_resolves_split_without_unwinding(ui):
    client, mgr = ui
    deal = _funded_milestone_deal(client, mgr)
    # Settle m2 (manual) first: it must survive m1's dispute.
    mgr.confirm_milestone(deal, "m2")
    assert deal.milestones[1].state.value == "released"
    # Dispute m1, add evidence, resolve as a split of its share.
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "buyer", "action": "dispute_milestone",
        "milestone_id": "m1", "text": "arrived at the wrong ZIP",
        "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    assert deal.milestones[0].state.value == "disputed"
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "seller", "action": "evidence",
        "milestone_id": "m1", "kind": "tracking_proof",
        "text": "carrier record", "tracking_number": "1ZMS1",
        "k": deal.share_tokens["seller"]})
    assert resp.status_code == 302
    assert milestones_mod.milestone_evidence(
        deal, deal.milestones[0])
    resp = client.post(f"/deal/{deal.id}/action", data={
        "party": "buyer", "action": "resolve_milestone_split",
        "milestone_id": "m1", "split_release_cents": "45000",
        "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    m1, m2 = deal.milestones
    assert m1.state.value == "released" and m1.released_cents == 45000
    assert m2.state.value == "released"  # untouched by the dispute
    # All milestones settled: the deal closes at the aggregate.
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.split_released_cents == 75000
    assert deal.split_voided_cents == 45000


def test_monitor_releases_check_milestone_and_disputes_condition(ui):
    client, mgr = ui
    deal = _funded_milestone_deal(client, mgr)
    tracking = {"number": "1Z9", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "delivered": True,
                "delivered_at": "2026-11-05T14:00:00Z",
                "delivered_zip": "47172"}
    mgr.monitor(deal, tracking, days_since_funding=4,
                messages=["box arrived dented"])
    m1, m2 = deal.milestones
    assert m1.state.value == "released"  # delivered check passed
    # m2 is manual: untouched by the monitor pass.
    assert m2.state.value == "funded-held"
    # The per-item view groups the delivered item under m1.
    page = client.get(f"/deal/{deal.id}/detail").get_data(
        as_text=True)
    assert "Delivery" in page and "delivered" in page


# --- seeds + design shell ---

def test_seeded_laptop_is_a_milestone_deal(seeded_client):
    seeded_client.get("/")  # triggers seeding
    laptop = next(d for d in MANAGER.deals.values()
                  if d.terms.amount_cents == 120000)
    assert [m.amount_cents for m in laptop.milestones] == [
        90000, 30000]
    assert laptop.milestones[1].state.value == "disputed"
    page = seeded_client.get(
        f"/deal/{laptop.id}/detail").get_data(as_text=True)
    assert "Condition verified" in page


def test_atlas_glass_shell_on_every_surface(seeded_client):
    for url in ("/", "/propose", "/grade"):
        page = seeded_client.get(url).get_data(as_text=True)
        assert 'class="top"' in page, url       # shared shell header
        assert "--bg:#0b0e13" in page, url      # the one stylesheet
        assert 'class="card"' in page, url
    dashboard = seeded_client.get("/").get_data(as_text=True)
    assert "Start an agreement" in dashboard
    assert "released only when the agreed" in dashboard
    deal = next(iter(MANAGER.deals.values()))
    party_page = seeded_client.get(
        MANAGER.share_link(deal, "buyer")).get_data(as_text=True)
    assert 'class="card"' in party_page
    assert 'class="pill' in party_page
