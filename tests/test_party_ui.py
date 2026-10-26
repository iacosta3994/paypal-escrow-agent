"""Track L3 — party views + notifications.

The party surface (src/app.py) and the notification outbox
(src/notifications.py): a per-party deal page distinct from the
operator dashboard, server-side permissions via the single
``allowed_actions`` function (rendering and POST enforcement share
it), and an append-only JSONL outbox fed by the DealManager
transition listener — one notice per party per real state change,
plus explicit notices for the no-state-change events (receipt
confirmation, evidence submission).

Every test runs against a FRESH DealManager swapped into
``app.config["DEAL_MANAGER"]`` with its own transition listener,
and the outbox path is redirected to a tmp file, so counts are
exact per test.
"""
from datetime import datetime

import pytest

from src import config, disputes, notifications
from src.app import MANAGER, allowed_actions, app
from src.deal_manager import DealManager
from src.models import DealState

from .e2e_support import CAMERA, make_terms


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """(test_client, fresh manager with the notification listener
    registered), outbox + audit log redirected to tmp files."""
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
    """A test client against the app's own seeded MANAGER."""
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


def _proposed_seller(mgr):
    return mgr.propose(make_terms(CAMERA), proposer="seller")


def _funded(mgr):
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    return deal


def _post(client, deal, party, action, **fields):
    return client.post(f"/deal/{deal.id}/action",
                       data={"party": party, "action": action,
                             **fields})


def _page(client, deal, party):
    resp = client.get(f"/deal/{deal.id}?party={party}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# --- notifications.py: the outbox itself ---

def test_emit_writes_one_record_per_party(ui):
    _, mgr = ui
    deal = _proposed_seller(mgr)
    records = notifications.emit(deal, "custom_event", "hello both")
    assert [r["party"] for r in records] == ["buyer", "seller"]
    for record in records:
        assert record["deal_id"] == deal.id
        assert record["event"] == "custom_event"
        assert record["message"] == "hello both"
        datetime.fromisoformat(record["at"])  # parses as ISO
    assert records[0]["email"] == "buyer@example.com"
    assert records[1]["email"] == "seller@example.com"
    # What emit returned is what the outbox holds, in order.
    assert notifications.for_deal(deal.id) == records
    assert notifications.for_party(deal.id, "buyer") == [records[0]]
    assert notifications.for_party(deal.id, "seller") == [records[1]]


def test_for_deal_missing_file_and_other_deals(ui):
    _, mgr = ui
    deal = _proposed_seller(mgr)
    other = _proposed_seller(mgr)
    assert notifications.for_deal(deal.id) == []  # no file yet
    notifications.emit(deal, "e1", "m1")
    notifications.emit(other, "e2", "m2")
    # One record per party, so the deal's event appears twice and
    # the other deal's records are filtered out.
    assert [r["event"] for r in notifications.for_deal(deal.id)] == \
        ["e1", "e1"]
    assert notifications.for_deal("deal-unknown") == []


def test_transition_listener_message_names_states(ui):
    _, mgr = ui
    deal = _proposed_seller(mgr)
    records = notifications.transition_listener(
        deal, DealState.PROPOSED, DealState.FUNDED)
    assert [r["event"] for r in records] == ["state_change"] * 2
    message = records[0]["message"]
    assert deal.id in message
    assert "Used mirrorless camera" in message
    assert "proposed" in message and "funded" in message


# --- allowed_actions: the permission matrix, directly ---

def test_allowed_actions_matrix(ui):
    _, mgr = ui
    deal = _proposed_seller(mgr)
    # Seller proposed, so the seller already accepted by proposing.
    # (2026-10-26: the proposer also holds "withdraw" while the
    # counterparty has not accepted — user-flow spec §3.)
    assert allowed_actions(deal, "seller") == {"withdraw"}
    assert allowed_actions(deal, "buyer") == {"accept", "decline"}
    assert allowed_actions(deal, "stranger") == set()

    deal = _funded(mgr)
    assert allowed_actions(deal, "buyer") == {"confirm", "dispute"}
    assert allowed_actions(deal, "seller") == {"dispute"}

    mgr.open_dispute(deal)
    assert allowed_actions(deal, "buyer") == {
        "confirm", "resolve_dispute", "evidence"}
    assert allowed_actions(deal, "seller") == {
        "resolve_dispute", "evidence"}

    mgr.confirm_receipt(deal)
    assert "confirm" not in allowed_actions(deal, "buyer")

    terminal = _proposed_seller(mgr)
    mgr.decline_terms(terminal, "buyer")
    assert terminal.state == DealState.DECLINED
    assert allowed_actions(terminal, "buyer") == set()
    assert allowed_actions(terminal, "seller") == set()


# --- permission enforcement on POST ---

def test_seller_cannot_confirm(ui):
    client, mgr = ui
    deal = _funded(mgr)
    resp = _post(client, deal, "seller", "confirm")
    assert resp.status_code == 403
    assert deal.buyer_confirmed_at is None


def test_buyer_evidence_without_open_dispute_is_403(ui):
    client, mgr = ui
    deal = _funded(mgr)
    assert deal.dispute_filed is False
    resp = _post(client, deal, "buyer", "evidence",
                 kind="note", text="hello")
    assert resp.status_code == 403
    assert deal.dispute_evidence == []


def test_accept_when_already_accepted_is_403(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    # The proposer (seller) accepted by proposing.
    assert _post(client, deal, "seller", "accept").status_code == 403
    # The buyer's first accept works; the second is refused.
    assert _post(client, deal, "buyer", "accept").status_code == 302
    assert "buyer" in deal.terms_acceptances
    assert _post(client, deal, "buyer", "accept").status_code == 403


def test_any_action_on_terminal_deal_is_403(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    mgr.decline_terms(deal, "buyer")
    assert deal.state == DealState.DECLINED
    for party in ("buyer", "seller"):
        for action in ("accept", "decline", "confirm", "dispute",
                       "resolve_dispute", "evidence"):
            resp = _post(client, deal, party, action,
                         kind="note", text="x")
            assert resp.status_code == 403, (party, action)


def test_invalid_party_is_400(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    assert client.get(f"/deal/{deal.id}").status_code == 400
    assert client.get(
        f"/deal/{deal.id}?party=stranger").status_code == 400
    assert _post(client, deal, "stranger", "accept").status_code == 400
    assert client.post(f"/deal/{deal.id}/action",
                       data={"action": "accept"}).status_code == 400


def test_unknown_deal_is_404(ui):
    client, _ = ui
    assert client.get(
        "/deal/deal-unknown?party=buyer").status_code == 404
    assert client.post(
        "/deal/deal-unknown/action",
        data={"party": "buyer", "action": "accept"}).status_code == 404
    assert client.get(
        "/api/deal/deal-unknown/notices").status_code == 404


# --- page rendering ---

def test_proposed_deal_pages_show_only_own_actions(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)

    buyer_page = _page(client, deal, "buyer")
    assert 'value="accept"' in buyer_page
    assert 'value="decline"' in buyer_page
    assert 'value="confirm"' not in buyer_page
    assert 'value="dispute"' not in buyer_page

    # The seller already accepted by proposing: no accept/decline.
    seller_page = _page(client, deal, "seller")
    assert 'value="accept"' not in seller_page
    assert 'value="decline"' not in seller_page
    assert 'value="confirm"' not in seller_page


def test_funded_deal_pages_show_only_own_actions(ui):
    client, mgr = ui
    deal = _funded(mgr)

    buyer_page = _page(client, deal, "buyer")
    assert 'value="confirm"' in buyer_page
    assert 'value="dispute"' in buyer_page
    assert 'value="accept"' not in buyer_page

    seller_page = _page(client, deal, "seller")
    assert 'value="dispute"' in seller_page
    assert 'value="confirm"' not in seller_page
    assert 'value="accept"' not in seller_page


def test_party_page_shows_terms_counterparty_and_state(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    buyer_page = _page(client, deal, "buyer")
    assert "Used mirrorless camera" in buyer_page
    assert "$500.00" in buyer_page
    assert "proposed" in buyer_page
    assert "seller@example.com" in buyer_page  # the counterparty
    seller_page = _page(client, deal, "seller")
    assert "buyer@example.com" in seller_page  # the counterparty


def test_dispute_evidence_timeline_renders_on_both_pages(ui):
    client, mgr = ui
    deal = _funded(mgr)
    mgr.open_dispute(deal)
    disputes.submit_evidence(
        deal, "seller", "tracking_proof",
        text="label created and moving",
        data={"tracking_number": "1ZTRACK99"})
    disputes.submit_evidence(
        deal, "buyer", "damage_report",
        text="screen arrived cracked")
    for party in ("buyer", "seller"):
        page = _page(client, deal, party)
        assert "tracking_proof" in page
        assert "1ZTRACK99" in page
        assert "damage_report" in page
        assert "screen arrived cracked" in page


# --- notice emission through the real flow ---

def test_state_change_notice_one_per_party_per_transition(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    # Accept changes no state -> no notice, even through the UI.
    assert _post(client, deal, "buyer", "accept").status_code == 302
    assert notifications.for_deal(deal.id) == []

    mgr.fund(deal)  # PROPOSED -> FUNDED: exactly one transition
    records = notifications.for_deal(deal.id)
    assert len(records) == 2
    assert {r["party"] for r in records} == {"buyer", "seller"}
    assert {r["event"] for r in records} == {"state_change"}
    for record in records:
        assert "proposed" in record["message"]
        assert "funded" in record["message"]
    assert records[0]["email"] == "buyer@example.com"


def test_failed_action_emits_nothing(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    # Confirm is not available in PROPOSED -> 403, and no notice.
    assert _post(client, deal, "buyer", "confirm").status_code == 403
    assert notifications.for_deal(deal.id) == []


def test_confirm_emits_receipt_confirmed_and_no_state_change(ui):
    client, mgr = ui
    deal = _funded(mgr)
    baseline = notifications.for_deal(deal.id)
    assert len(baseline) == 2  # the funding transition, one per party
    assert {r["event"] for r in baseline} == {"state_change"}

    resp = _post(client, deal, "buyer", "confirm")
    assert resp.status_code == 302
    assert deal.buyer_confirmed_at is not None

    records = notifications.for_deal(deal.id)
    assert len(records) == 4
    new = records[len(baseline):]
    assert {r["event"] for r in new} == {"receipt_confirmed"}
    assert {r["party"] for r in new} == {"buyer", "seller"}
    # Still exactly the one state_change pair from funding.
    assert sum(1 for r in records
               if r["event"] == "state_change") == 2


# --- POST flows: dispute / evidence / decline ---

def test_dispute_then_evidence_then_resolve_via_posts(ui):
    client, mgr = ui
    deal = _funded(mgr)

    assert _post(client, deal, "seller", "dispute").status_code == 302
    assert deal.dispute_filed is True

    resp = _post(client, deal, "seller", "evidence",
                 kind="tracking_proof", text="here is the label",
                 tracking_number="1ZPOSTED1")
    assert resp.status_code == 302
    assert len(deal.dispute_evidence) == 1
    record = deal.dispute_evidence[0]
    assert record["party"] == "seller"
    assert record["kind"] == "tracking_proof"
    assert record["data"] == {"tracking_number": "1ZPOSTED1"}
    evidence_notices = [r for r in notifications.for_deal(deal.id)
                        if r["event"] == "evidence_submitted"]
    assert len(evidence_notices) == 2  # one per party
    assert {r["party"] for r in evidence_notices} == {"buyer", "seller"}

    assert _post(client, deal, "buyer",
                 "resolve_dispute").status_code == 302
    assert deal.dispute_filed is False
    # Resolved: evidence is closed again.
    assert _post(client, deal, "buyer", "evidence",
                 kind="note", text="late").status_code == 403


def test_evidence_domain_errors_are_400_with_message(ui):
    client, mgr = ui
    deal = _funded(mgr)
    mgr.open_dispute(deal)
    before = notifications.for_deal(deal.id)

    # tracking_proof without a tracking number.
    resp = _post(client, deal, "seller", "evidence",
                 kind="tracking_proof", text="trust me")
    assert resp.status_code == 400
    assert "tracking_number" in resp.get_data(as_text=True)
    # A settlement offer is buyer-only: the seller's is a domain
    # ValueError, surfaced as 400 — and neither failure stored
    # evidence or emitted a notice.
    resp = _post(client, deal, "seller", "evidence",
                 kind="settlement_offer", split_release_cents="20000")
    assert resp.status_code == 400
    assert deal.dispute_evidence == []
    assert notifications.for_deal(deal.id) == before

    # The buyer's valid offer lands, split parsed to int cents.
    resp = _post(client, deal, "buyer", "evidence",
                 kind="settlement_offer", text="keep it, partial refund",
                 split_release_cents="20000")
    assert resp.status_code == 302
    assert deal.dispute_evidence[-1]["data"] == {
        "split_release_cents": 20000}


def test_decline_via_post_ends_deal_with_notice(ui):
    client, mgr = ui
    deal = _proposed_seller(mgr)
    resp = _post(client, deal, "buyer", "decline")
    assert resp.status_code == 302
    assert deal.state == DealState.DECLINED
    records = notifications.for_deal(deal.id)
    assert len(records) == 2
    assert {r["event"] for r in records} == {"state_change"}
    assert "declined" in records[0]["message"]


# --- notices API ---

def test_notices_api_per_party_and_all(ui):
    client, mgr = ui
    deal = _funded(mgr)
    _post(client, deal, "buyer", "confirm")

    resp = client.get(f"/api/deal/{deal.id}/notices")
    assert resp.status_code == 200
    all_notices = resp.get_json()
    assert len(all_notices) == 4
    assert {r["party"] for r in all_notices} == {"buyer", "seller"}

    buyer_notices = client.get(
        f"/api/deal/{deal.id}/notices?party=buyer").get_json()
    assert len(buyer_notices) == 2
    assert all(r["party"] == "buyer" for r in buyer_notices)
    assert {r["event"] for r in buyer_notices} == {
        "state_change", "receipt_confirmed"}

    assert client.get(
        f"/api/deal/{deal.id}/notices?party=stranger"
    ).status_code == 400


# --- seeded app + webhook wiring ---

def test_seeded_app_dashboard_and_party_pages(seeded_client):
    assert seeded_client.get("/").status_code == 200
    assert seeded_client.get("/api/decisions").status_code == 200

    deals = list(MANAGER.deals.values())
    assert len(deals) >= 3
    awaiting = [d for d in deals
                if d.state == DealState.PROPOSED
                and d.proposed_by == "seller"
                and "buyer" not in d.terms_acceptances]
    assert awaiting, "seed (a): seller-proposed deal awaiting buyer"
    monitoring = [d for d in deals if d.state == DealState.MONITORING]
    assert monitoring, "seed (b): funded deal in MONITORING"
    disputed = [d for d in deals
                if d.dispute_filed and d.dispute_evidence]
    assert disputed, "seed (c): funded deal, open dispute, evidence"
    assert disputed[0].dispute_evidence[0]["party"] == "seller"

    for deal in deals:
        for party in ("buyer", "seller"):
            # Seeded deals carry the stable demo tokens
            # (DEMO_SCRIPT.md); their links include them.
            resp = seeded_client.get(
                f"/deal/{deal.id}?party={party}"
                f"&k={deal.share_tokens[party]}")
            assert resp.status_code == 200, (deal.id, party)


def test_webhook_blueprint_registered(ui):
    client, _ = ui
    resp = client.post("/webhooks/paypal", data="{not json",
                       content_type="application/json")
    assert resp.status_code == 400
