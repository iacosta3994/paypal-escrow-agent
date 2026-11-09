"""Track L4 — PayPal webhooks (src/webhooks.py).

Handler logic tested with fixture payloads shaped like real PayPal
events against a real (sim-mode) DealManager: per-event application,
replay idempotency, unknown-deal events ignored WITHOUT being
recorded (so a later redelivery can apply), terminal deals never
flipped by late events, malformed payloads rejected, and the Flask
blueprint exercised through a test client on a throwaway app
(Track L3 registers the blueprint on the real app later).
"""
import pytest
from flask import Flask

from src import config, webhooks
from src.deal_manager import DealManager
from src.models import DealState
from src.webhooks import find_deal, handle_event, register_webhooks

from .e2e_support import CAMERA, make_terms

ORDER_ID = "ORDER123"
AUTH_ID = "AUTH999"
CREATE_TIME = "2026-10-28T12:34:56Z"


# --- payload / deal builders ---

def _event(event_id, event_type, resource_id=None, order_id=None,
           resource_type="payment", amount="500.00", status=None):
    """A PayPal-shaped webhook event."""
    resource = {"id": resource_id}
    if status is not None:
        resource["status"] = status
    if amount is not None:
        resource["amount"] = {"value": amount, "currency_code": "USD"}
    if order_id is not None:
        resource["supplementary_data"] = {
            "related_ids": {"order_id": order_id}}
    return {"id": event_id, "event_type": event_type,
            "create_time": CREATE_TIME, "resource_type": resource_type,
            "resource": resource}


def _order_approved(event_id="WH-ORDER-1"):
    # For CHECKOUT.ORDER.APPROVED the resource IS the order.
    return _event(event_id, "CHECKOUT.ORDER.APPROVED",
                  resource_id=ORDER_ID, resource_type="checkout-order",
                  amount=None, status="APPROVED")


def _awaiting_deal(mgr, order_id=ORDER_ID):
    """A buyer-flow deal parked in AWAITING_APPROVAL with its PayPal
    order id assigned (sim mode: complete_funding flips it to
    FUNDED)."""
    deal = mgr.propose(make_terms(CAMERA))
    deal.state = DealState.AWAITING_APPROVAL
    deal.paypal_order_id = order_id
    return deal


def _funded_deal(mgr, state=DealState.FUNDED, auth_id=AUTH_ID):
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)  # sim mode -> FUNDED
    deal.paypal_order_id = ORDER_ID
    deal.paypal_auth_id = auth_id
    if state != DealState.FUNDED:
        deal.state = state
    return deal


# --- CHECKOUT.ORDER.APPROVED ---

def test_order_approved_completes_funding():
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)

    result = handle_event(mgr, _order_approved())

    assert result["status"] == "applied"
    assert result["action"] == "funding_completed"
    assert result["deal_id"] == deal.id
    assert result["event_type"] == "CHECKOUT.ORDER.APPROVED"
    assert deal.buyer_approved_at == CREATE_TIME
    assert deal.state == DealState.FUNDED


def test_order_approved_replay_is_duplicate_no_refund():
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)
    calls = {"n": 0}
    real = mgr.complete_funding

    def spy(d):
        calls["n"] += 1
        return real(d)

    mgr.complete_funding = spy

    first = handle_event(mgr, _order_approved())
    assert first["status"] == "applied"
    assert calls["n"] == 1

    replay = handle_event(mgr, _order_approved())  # same event id
    assert replay["status"] == "duplicate"
    assert replay["event_id"] == "WH-ORDER-1"
    assert calls["n"] == 1  # funding NOT re-run
    assert deal.state == DealState.FUNDED


def test_order_approved_funding_failure_keeps_approval():
    """complete_funding raising (e.g. PayPal 422 in paypal mode)
    leaves the approval recorded and the deal AWAITING_APPROVAL."""
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)

    def boom(d):
        raise RuntimeError("422 ORDER_NOT_APPROVED")

    mgr.complete_funding = boom
    result = handle_event(mgr, _order_approved())

    assert result["status"] == "applied"
    assert result["action"] == "approval_recorded"
    assert result["funding"] == "pending"
    assert "ORDER_NOT_APPROVED" in result["error"]
    assert deal.buyer_approved_at == CREATE_TIME
    assert deal.state == DealState.AWAITING_APPROVAL


def test_order_approved_wrong_state_ignored():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)  # already FUNDED

    result = handle_event(mgr, _order_approved())

    assert result["status"] == "ignored"
    assert result["reason"] == "deal not awaiting approval"
    assert deal.buyer_approved_at is None
    assert deal.state == DealState.FUNDED


# --- PAYMENT.AUTHORIZATION.CREATED ---

def test_authorization_created_via_related_order_id():
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)
    assert deal.paypal_auth_id is None

    payload = _event("WH-AUTH-1", "PAYMENT.AUTHORIZATION.CREATED",
                     resource_id=AUTH_ID, order_id=ORDER_ID,
                     resource_type="authorization", status="CREATED")
    assert find_deal(mgr, payload) is deal  # matched by related order

    result = handle_event(mgr, payload)

    assert result["status"] == "applied"
    assert result["action"] == "authorization_recorded"
    assert result["deal_id"] == deal.id
    assert deal.paypal_auth_id == AUTH_ID
    # Reconciliation: authorized through another path while the deal
    # still thought it was awaiting approval.
    assert deal.state == DealState.FUNDED


def test_authorization_created_keeps_existing_auth_id():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr)  # auth id already AUTH_ID

    payload = _event("WH-AUTH-2", "PAYMENT.AUTHORIZATION.CREATED",
                     resource_id="AUTHOTHER", order_id=ORDER_ID,
                     resource_type="authorization", status="CREATED")
    result = handle_event(mgr, payload)

    assert result["status"] == "applied"
    assert deal.paypal_auth_id == AUTH_ID  # not overwritten
    assert deal.state == DealState.FUNDED


# --- PAYMENT.AUTHORIZATION.VOIDED ---

def test_authorization_voided_refunds_monitoring_deal():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr, state=DealState.MONITORING)

    payload = _event("WH-VOID-1", "PAYMENT.AUTHORIZATION.VOIDED",
                     resource_id=AUTH_ID, resource_type="authorization",
                     status="VOIDED")
    result = handle_event(mgr, payload)

    assert result["status"] == "applied"
    assert result["action"] == "deal_refunded"
    assert result["deal_id"] == deal.id
    assert deal.state == DealState.REFUNDED


def test_voided_after_released_does_not_flip():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr, state=DealState.RELEASED)

    payload = _event("WH-VOID-2", "PAYMENT.AUTHORIZATION.VOIDED",
                     resource_id=AUTH_ID, resource_type="authorization",
                     status="VOIDED")
    result = handle_event(mgr, payload)

    assert result["status"] == "ignored"
    assert deal.state == DealState.RELEASED


# --- PAYMENT.CAPTURE.COMPLETED ---

def test_capture_completed_releases_and_records_capture():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr, state=DealState.MONITORING)

    # The capture's own id is new; the deal is found via related_ids.
    payload = _event("WH-CAP-1", "PAYMENT.CAPTURE.COMPLETED",
                     resource_id="CAPTURE777", order_id=ORDER_ID,
                     resource_type="capture", status="COMPLETED")
    result = handle_event(mgr, payload)

    assert result["status"] == "applied"
    assert result["action"] == "deal_released"
    assert result["capture_amount"] == 500.00
    assert deal.state == DealState.RELEASED
    assert deal.capture_id == "CAPTURE777"


def test_capture_after_refunded_does_not_flip():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr, state=DealState.REFUNDED)

    payload = _event("WH-CAP-2", "PAYMENT.CAPTURE.COMPLETED",
                     resource_id="CAPTURE888", order_id=ORDER_ID,
                     resource_type="capture", status="COMPLETED")
    result = handle_event(mgr, payload)

    assert result["status"] == "applied"  # capture id still recorded
    assert result["action"] == "capture_recorded"
    assert deal.state == DealState.REFUNDED


# --- unknown deals / unhandled types / idempotency bookkeeping ---

def test_unknown_deal_ignored_and_not_recorded_then_applies():
    mgr = DealManager(use_paypal=False)
    payload = _order_approved("WH-LATE-1")

    result = handle_event(mgr, payload)  # no deal has ORDER123 yet
    assert result["status"] == "ignored"
    assert result["reason"] == "no matching deal"
    assert "WH-LATE-1" not in mgr._webhook_event_ids

    deal = _awaiting_deal(mgr)  # the deal appears (created late)
    retry = handle_event(mgr, payload)  # same event id redelivered
    assert retry["status"] == "applied"
    assert retry["action"] == "funding_completed"
    assert deal.state == DealState.FUNDED
    assert "WH-LATE-1" in mgr._webhook_event_ids


def test_unhandled_event_type_ignored():
    mgr = DealManager(use_paypal=False)
    deal = _funded_deal(mgr, state=DealState.MONITORING)

    payload = _event("WH-OTHER-1", "CUSTOMER.DISPUTE.CREATED",
                     resource_id=AUTH_ID, resource_type="dispute")
    result = handle_event(mgr, payload)

    assert result["status"] == "ignored"
    assert result["reason"] == "unhandled event type"
    assert deal.state == DealState.MONITORING
    assert "WH-OTHER-1" not in mgr._webhook_event_ids


def test_find_deal_malformed_resources_return_none():
    mgr = DealManager(use_paypal=False)
    _funded_deal(mgr)
    assert find_deal(mgr, None) is None
    assert find_deal(mgr, {}) is None
    assert find_deal(mgr, {"resource": "junk"}) is None
    assert find_deal(mgr, {"resource": {}}) is None
    assert find_deal(mgr, {"resource": {
        "supplementary_data": "junk"}}) is None
    assert find_deal(mgr, {"resource": {
        "supplementary_data": {"related_ids": "junk"}}}) is None
    # A resource id matching nothing, and no false match against a
    # deal whose PayPal ids are unset:
    mgr2 = DealManager(use_paypal=False)
    bare = mgr2.propose(make_terms(CAMERA))
    assert bare.paypal_order_id is None and bare.paypal_auth_id is None
    assert find_deal(mgr2, {"resource": {"id": "WHATEVER"}}) is None


# --- validation ---

@pytest.mark.parametrize("payload", [
    {},                                                     # nothing
    {"event_type": "X", "resource": {}},                    # missing id
    {"id": "WH-1", "resource": {}},                         # missing type
    {"id": "WH-1", "event_type": "X"},                      # missing resource
    {"id": "WH-1", "event_type": "X", "resource": "junk"},  # bad resource
    ["not", "a", "dict"],                                   # not an object
])
def test_malformed_payloads_raise_value_error(payload):
    mgr = DealManager(use_paypal=False)
    with pytest.raises(ValueError):
        handle_event(mgr, payload)


# --- Flask blueprint ---

def _client(mgr):
    app = Flask(__name__)
    register_webhooks(app, mgr)
    return app.test_client()


def test_blueprint_valid_event_200_json():
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal", json=_order_approved())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "applied"
    assert body["action"] == "funding_completed"
    assert body["deal_id"] == deal.id
    assert deal.state == DealState.FUNDED


def test_blueprint_garbage_body_400():
    mgr = DealManager(use_paypal=False)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal", data="{not json",
                       content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_blueprint_malformed_event_400():
    mgr = DealManager(use_paypal=False)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal",
                       json={"event_type": "CHECKOUT.ORDER.APPROVED"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- Signature verification (PAYPAL_WEBHOOK_ID configured) ---

_TX_HEADERS = {
    "PayPal-Auth-Algo": "SHA256withRSA",
    "PayPal-Cert-Url": "https://api.sandbox.paypal.com/certs/CERT-1",
    "PayPal-Transmission-Id": "tx-123",
    "PayPal-Transmission-Sig": "sig-abc",
    "PayPal-Transmission-Time": "2026-10-28T12:34:56Z",
}


@pytest.fixture
def _webhook_id(monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_WEBHOOK_ID", "WH-TEST-ID")


def test_verified_delivery_applies(_webhook_id, monkeypatch):
    monkeypatch.setattr(webhooks, "verify_signature",
                        lambda headers, payload: True)
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal", json=_order_approved(),
                       headers=_TX_HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "applied"
    assert deal.state == DealState.FUNDED


def test_unverified_delivery_rejected_no_side_effects(_webhook_id,
                                                      monkeypatch):
    monkeypatch.setattr(webhooks, "verify_signature",
                        lambda headers, payload: False)
    mgr = DealManager(use_paypal=False)
    deal = _awaiting_deal(mgr)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal", json=_order_approved(),
                       headers=_TX_HEADERS)
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "rejected"
    assert deal.state == DealState.AWAITING_APPROVAL
    assert deal.buyer_approved_at is None
    # Not recorded as processed: a later verified redelivery can apply.
    assert "WH-ORDER-1" not in getattr(mgr, "_webhook_event_ids", set())


def test_missing_transmission_headers_fail_without_network(_webhook_id,
                                                           monkeypatch):
    called = []
    monkeypatch.setattr(webhooks, "_verify_request",
                        lambda body: called.append(body) or True)
    mgr = DealManager(use_paypal=False)
    _awaiting_deal(mgr)
    client = _client(mgr)

    resp = client.post("/webhooks/paypal", json=_order_approved())
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "rejected"
    assert called == []


def test_verify_signature_builds_paypal_body(_webhook_id, monkeypatch):
    seen = {}
    monkeypatch.setattr(webhooks, "_verify_request",
                        lambda body: seen.update(body) or True)
    payload = _order_approved()

    assert webhooks.verify_signature(_TX_HEADERS, payload) is True
    assert seen["webhook_id"] == "WH-TEST-ID"
    assert seen["transmission_id"] == "tx-123"
    assert seen["auth_algo"] == "SHA256withRSA"
    assert seen["cert_url"].endswith("CERT-1")
    assert seen["webhook_event"] == payload


def test_verify_request_success_and_failure(monkeypatch):
    class Resp:
        def __init__(self, status):
            self._status = status

        def raise_for_status(self):
            pass

        def json(self):
            return {"verification_status": self._status}

    monkeypatch.setattr(webhooks.paypal_client, "_headers", lambda: {})
    monkeypatch.setattr(webhooks.requests, "post",
                        lambda *a, **k: Resp("SUCCESS"))
    assert webhooks._verify_request({}) is True
    monkeypatch.setattr(webhooks.requests, "post",
                        lambda *a, **k: Resp("FAILURE"))
    assert webhooks._verify_request({}) is False


def test_verify_request_fails_closed_on_transport_error(monkeypatch):
    monkeypatch.setattr(webhooks.paypal_client, "_headers", lambda: {})

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(webhooks.requests, "post", boom)
    assert webhooks._verify_request({}) is False
