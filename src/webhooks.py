"""PayPal webhooks: push notifications for the money events.

PayPal can POST event notifications to a listener endpoint instead of
the agent polling for state changes. This module handles the four
events that matter to the escrow lifecycle:

- ``CHECKOUT.ORDER.APPROVED`` — the buyer approved a buyer-funded
  order. The approval timestamp is recorded on the deal
  (``buyer_approved_at``) and funding completion is attempted
  immediately via ``DealManager.complete_funding``.
- ``PAYMENT.AUTHORIZATION.CREATED`` — an authorization now holds the
  money. Its id is recorded on the deal (``paypal_auth_id``); if the
  deal was still ``AWAITING_APPROVAL`` (the authorization arrived
  through another path), the deal is reconciled to ``FUNDED``.
- ``PAYMENT.AUTHORIZATION.VOIDED`` — the hold was voided (money back
  to the buyer): the deal moves to ``REFUNDED`` unless it is already
  in a terminal settled state.
- ``PAYMENT.CAPTURE.COMPLETED`` — the held money was captured to the
  seller: the capture id is recorded (``capture_id``) and the deal
  moves to ``RELEASED`` unless it is already terminal.

Deals are located by PayPal id (see :func:`find_deal`): the resource
id matching a deal's authorization or order id, or the order id in
``resource.supplementary_data.related_ids.order_id``.

Idempotency: PayPal delivers webhooks at-least-once, so deliveries
repeat. Processed event ids are kept in a per-manager set
(``manager._webhook_event_ids``); a redelivery of an applied event
returns ``{"status": "duplicate", ...}`` with no side effects. An
event id is recorded ONLY when the event was applied — an event for
an unknown deal (or one that does not apply to the deal's current
state) is ignored WITHOUT being recorded, so a later redelivery,
e.g. after the deal exists locally, can still apply.

Polling remains the authoritative fallback: ``DealManager.monitor``
and PayPal read-backs do not depend on webhooks at all. If a webhook
is missed, delayed, or the listener is down, the polling/monitor
path still converges on the same state — webhooks only make the
common path faster, they are never the sole source of truth.

PRODUCTION NOTE — signature verification is REQUIRED in production
and is NOT implemented here. A real listener must verify every
delivery with PayPal's ``verify-webhook-signature`` API (using the
transmission id/sig, cert URL, auth algo, and the webhook id from
the request headers) before trusting the payload; without it, anyone
who can reach the endpoint could forge money events. This sandbox
listener exists to exercise the handler logic and is tested with
fixture payloads shaped like real PayPal events; do not expose it
publicly as-is.
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from .models import Deal, DealState

# Terminal settled states: a late money event must never flip a deal
# out of one of these.
_TERMINAL_SETTLED = (DealState.REFUNDED, DealState.RELEASED,
                     DealState.SPLIT_SETTLED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_deal(manager, payload) -> Deal | None:
    """Locate the deal a webhook payload refers to, or None.

    Matches, in any deal in ``manager.deals``:

    - ``resource.id`` == ``deal.paypal_auth_id``
    - ``resource.id`` == ``deal.paypal_order_id``
    - ``resource.supplementary_data.related_ids.order_id`` ==
      ``deal.paypal_order_id``

    Malformed payloads/resources return None, never raise.
    """
    if not isinstance(payload, dict):
        return None
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None
    resource_id = resource.get("id")
    related_order_id = None
    supplementary = resource.get("supplementary_data")
    if isinstance(supplementary, dict):
        related_ids = supplementary.get("related_ids")
        if isinstance(related_ids, dict):
            related_order_id = related_ids.get("order_id")
    if not resource_id and not related_order_id:
        return None
    for deal in manager.deals.values():
        if resource_id and resource_id in (deal.paypal_auth_id,
                                            deal.paypal_order_id):
            return deal
        if related_order_id and related_order_id == deal.paypal_order_id:
            return deal
    return None


def _event_ids(manager) -> set:
    """The per-manager set of processed (applied) webhook event ids."""
    ids = getattr(manager, "_webhook_event_ids", None)
    if ids is None:
        ids = set()
        manager._webhook_event_ids = ids
    return ids


def _result(status: str, payload: dict, **extra) -> dict:
    result = {"status": status, "event_id": payload["id"],
              "event_type": payload["event_type"]}
    result.update(extra)
    return result


def handle_event(manager, payload) -> dict:
    """Apply one PayPal webhook event to the manager's deals.

    Returns a result dict. Every result carries ``status``,
    ``event_id`` and ``event_type``; applied results also carry
    ``deal_id`` and an ``action``. ``status`` is one of:

    - ``"applied"`` — the event changed (or confirmed) deal state;
      the event id is recorded as processed.
    - ``"duplicate"`` — this event id was already applied; no side
      effects.
    - ``"ignored"`` — no matching deal, an unhandled event type, or
      the event does not apply to the deal's current state. NOT
      recorded as processed, so a later redelivery can still apply.

    Raises ValueError on a malformed payload (not a dict, missing
    ``id``/``event_type``, or a non-dict ``resource``).
    """
    if not isinstance(payload, dict):
        raise ValueError("webhook payload must be a JSON object")
    if not isinstance(payload.get("id"), str) or not payload["id"]:
        raise ValueError("webhook payload is missing its event 'id'")
    if (not isinstance(payload.get("event_type"), str)
            or not payload["event_type"]):
        raise ValueError("webhook payload is missing 'event_type'")
    if not isinstance(payload.get("resource"), dict):
        raise ValueError("webhook payload 'resource' must be an object")

    event_type = payload["event_type"]
    processed = _event_ids(manager)
    if payload["id"] in processed:
        return _result("duplicate", payload)

    deal = find_deal(manager, payload)
    if deal is None:
        return _result("ignored", payload, reason="no matching deal")

    resource = payload["resource"]

    if event_type == "CHECKOUT.ORDER.APPROVED":
        if deal.state != DealState.AWAITING_APPROVAL:
            return _result("ignored", payload,
                           reason="deal not awaiting approval")
        deal.buyer_approved_at = payload.get("create_time") or _now_iso()
        try:
            manager.complete_funding(deal)
        except Exception as exc:
            # The approval itself is real and stays recorded; only
            # the authorization is pending. Funding can still be
            # completed later (redelivery is a duplicate by design —
            # the retry path is complete_funding / the monitor).
            processed.add(payload["id"])
            return _result("applied", payload, deal_id=deal.id,
                           action="approval_recorded",
                           funding="pending", error=str(exc))
        processed.add(payload["id"])
        return _result("applied", payload, deal_id=deal.id,
                       action="funding_completed")

    if event_type == "PAYMENT.AUTHORIZATION.CREATED":
        if not deal.paypal_auth_id and resource.get("id"):
            deal.paypal_auth_id = resource["id"]
        if deal.state == DealState.AWAITING_APPROVAL:
            # Authorized through another path (e.g. a card-funded
            # order or a manual approval the listener missed):
            # reconcile the local state with PayPal's truth.
            manager._set_state(deal, DealState.FUNDED)
        processed.add(payload["id"])
        return _result("applied", payload, deal_id=deal.id,
                       action="authorization_recorded")

    if event_type == "PAYMENT.AUTHORIZATION.VOIDED":
        if deal.state in _TERMINAL_SETTLED:
            return _result("ignored", payload,
                           reason="deal already settled")
        manager._set_state(deal, DealState.REFUNDED)
        processed.add(payload["id"])
        return _result("applied", payload, deal_id=deal.id,
                       action="deal_refunded")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        if resource.get("id"):
            deal.capture_id = resource["id"]
        capture_amount = None
        amount = resource.get("amount")
        if isinstance(amount, dict) and amount.get("value") is not None:
            try:
                capture_amount = float(amount["value"])
            except (TypeError, ValueError):
                capture_amount = None
        flipped = deal.state not in _TERMINAL_SETTLED
        if flipped:
            manager._set_state(deal, DealState.RELEASED)
        processed.add(payload["id"])
        return _result("applied", payload, deal_id=deal.id,
                       action="deal_released" if flipped
                       else "capture_recorded",
                       capture_amount=capture_amount)

    return _result("ignored", payload, reason="unhandled event type")


def make_blueprint(manager) -> Blueprint:
    """Flask blueprint exposing POST /webhooks/paypal for `manager`."""
    bp = Blueprint("paypal_webhooks", __name__)

    @bp.post("/webhooks/paypal")
    def paypal_webhook():
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "request body must be JSON"}), 400
        try:
            result = handle_event(manager, payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result), 200

    return bp


def register_webhooks(app, manager):
    """Register the PayPal webhook blueprint on a Flask app."""
    app.register_blueprint(make_blueprint(manager))
    return app
