"""The agreement surfaces (2026-10-26): /propose creates a deal
whose requirements list IS the agreement; both party pages and the
dashboard show that agreement — requirements, agreed completion
date, live status — from the moment the deal exists.

Same fixture discipline as tests/test_party_ui.py: a FRESH
DealManager swapped into app.config, outbox + audit redirected to
tmp files.
"""
from datetime import datetime, timedelta

import pytest

from src import agreement, config, notifications
from src.app import MANAGER, app
from src.deal_manager import DealManager
from src.models import DealState

from .e2e_support import CAMERA, make_terms


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


_PROPOSE_FORM = {
    "proposer": "seller",
    "item_description": "Used mirrorless camera",
    "amount": "500.00",
    "currency": "USD",
    "buyer_email": "buyer@example.com",
    "seller_email": "seller@example.com",
    "delivery_zip": "47172",
    "window_days": "14",
    "release_conditions": "delivery confirmed to buyer address",
    "req_check": ["delivered", "within_window", "condition_clear"],
    "extra_requirements": "Gift wrapped\n",
}


def _expected_completion(deal) -> str:
    created = datetime.fromisoformat(deal.created_at)
    return (created + timedelta(days=deal.terms.window_days)
            ).date().isoformat()


# --- the propose flow, end to end ---

def test_propose_page_renders_with_standards(ui):
    client, _ = ui
    resp = client.get("/propose")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    for _, text in agreement.STANDARD_REQUIREMENTS:
        assert text in page


def test_propose_flow_create_accept_fund(ui):
    client, mgr = ui
    resp = client.post("/propose", data=_PROPOSE_FORM)
    assert resp.status_code == 302
    assert len(mgr.deals) == 1
    deal = next(iter(mgr.deals.values()))
    # The proposer lands on their own share link (token included).
    location = resp.headers["Location"]
    assert f"/deal/{deal.id}?party=seller" in location
    assert f"&k={deal.share_tokens['seller']}" in location

    # The agreement was stored as proposed: check-linked standards
    # plus the free-text line as a manual requirement.
    reqs = deal.terms.requirements
    assert [r["check"] for r in reqs] == [
        "delivered", "within_window", "condition_clear", None]
    assert reqs[-1]["text"] == "Gift wrapped"
    assert deal.terms.amount_cents == 50000
    assert deal.terms.window_days == 14

    # Seller accepted by proposing; buyer has not.
    assert "seller" in deal.terms_acceptances
    assert "buyer" not in deal.terms_acceptances

    # The buyer's page shows the agreement from the start: the
    # requirements, the explicit completion date, pending statuses.
    page = client.get(mgr.share_link(deal, "buyer")).get_data(
        as_text=True)
    assert "The agreement" in page
    assert "agreed completion date" in page
    assert _expected_completion(deal) in page
    assert "Item is delivered to the buyer" in page
    assert "Gift wrapped" in page
    assert "pending" in page  # no monitoring pass yet
    assert "manual" in page   # the free-text requirement

    # Buyer accepts through the existing action surface (with
    # their share-link token), then funds through the fund action:
    # the full propose -> share -> accept -> fund journey.
    resp = client.post(f"/deal/{deal.id}/action",
                       data={"party": "buyer", "action": "accept",
                             "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    assert "buyer" in deal.terms_acceptances
    resp = client.post(f"/deal/{deal.id}/action",
                       data={"party": "buyer", "action": "fund",
                             "k": deal.share_tokens["buyer"]})
    assert resp.status_code == 302
    assert deal.state == DealState.FUNDED


@pytest.mark.parametrize("patch", [
    {"amount": "not-a-number"},
    {"amount": "-5"},
    {"window_days": "0"},
    {"delivery_zip": ""},
    {"item_description": ""},
    {"proposer": "stranger"},
])
def test_propose_invalid_input_is_400_and_creates_nothing(ui, patch):
    client, mgr = ui
    form = {**_PROPOSE_FORM, **patch}
    resp = client.post("/propose", data=form)
    assert resp.status_code == 400
    assert mgr.deals == {}


# --- live agreement status ---

def test_agreement_status_tracks_monitoring_evidence(ui):
    client, mgr = ui
    terms = make_terms(CAMERA)
    terms.requirements = [
        agreement.standard_requirement("delivered"),
        agreement.standard_requirement("within_window"),
        agreement.standard_requirement("condition_clear"),
        agreement.manual_requirement("Gift wrapped"),
    ]
    deal = mgr.propose(terms)
    # Before any monitoring pass everything check-linked is pending.
    assert [s["status"] for s in agreement.agreement_status(deal)] == [
        "pending", "pending", "pending", "manual"]

    mgr.fund(deal)
    tracking = {"number": "1Z1", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "delivered": True,
                "delivered_at": "2026-11-05T14:00:00Z",
                "delivered_zip": "47172"}
    mgr.monitor(deal, tracking, days_since_funding=3)
    statuses = {s["check"]: s["status"]
                for s in agreement.agreement_status(deal)}
    assert statuses["delivered"] == "met"
    assert statuses["within_window"] == "met"
    # No condition report exists: honestly unknown, never "met".
    assert statuses["condition_clear"] == "unknown"
    assert statuses[None] == "manual"
    # ... and the deal remembers the pass for the views.
    assert deal.last_check_results

    page = client.get(f"/deal/{deal.id}?party=seller").get_data(
        as_text=True)
    assert "<b>met</b>" in page


def test_completion_date_is_window_from_proposal():
    from src.models import Deal
    deal = Deal(id="d", terms=make_terms(CAMERA),
                created_at="2026-10-26T12:00:00+00:00")
    assert agreement.completion_date(deal) == "2026-11-09"


# --- dashboard + merged surfaces ---

def test_dashboard_lists_deals_agreements_and_detail_links(
        seeded_client):
    page = seeded_client.get("/").get_data(as_text=True)
    assert "Deals &amp; agreements" in page
    assert "/propose" in page
    for deal in MANAGER.deals.values():
        assert deal.id in page
        assert _expected_completion(deal) in page
        assert f"/deal/{deal.id}/detail" in page


def test_merged_surfaces_coexist(seeded_client):
    """Workbench merge integrity: /grade, the dashboard, the party
    pages and the new agreement surfaces all answer in one app."""
    assert seeded_client.get("/grade").status_code == 200
    assert seeded_client.get("/").status_code == 200
    assert seeded_client.get("/propose").status_code == 200
    deal = next(iter(MANAGER.deals.values()))
    assert seeded_client.get(
        MANAGER.share_link(deal, "buyer")).status_code == 200
    assert seeded_client.get(
        f"/deal/{deal.id}/detail").status_code == 200
