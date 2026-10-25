"""PayPal sandbox client: authorize / capture / void.

Uses the Orders API v2 directly over REST. No SDK needed.
Docs: https://developer.paypal.com/docs/api/orders/v2/

Funding flow (Orders v2, intent=AUTHORIZE):
  1. create_order()      -> order CREATED (+ approve_url for buyer-funded
                            orders; card-funded orders come back already
                            carrying an authorization)
  2. buyer approves      -> the buyer opens approve_url and logs in with
                            their PayPal account; there is NO API for this
                            step. (Card-funded orders skip it: supplying
                            payment_source at creation is the payer's
                            authorization to charge that instrument.)
  3. authorize_order()   -> order APPROVED -> authorization created,
                            funds held. Raises 422 ORDER_NOT_APPROVED
                            until step 2 has happened.
  4. capture() / void()  -> settle the held authorization.
"""
import base64
import time
import uuid

import requests

from . import config

_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

_token: str | None = None
_token_expires: float = 0


def _base_url() -> str:
    return _BASE.get(config.PAYPAL_MODE, _BASE["sandbox"])


def _access_token() -> str:
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token
    creds = base64.b64encode(
        f"{config.PAYPAL_CLIENT_ID}:{config.PAYPAL_CLIENT_SECRET}".encode()
    ).decode()
    r = requests.post(
        f"{_base_url()}/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    _token = body["access_token"]
    _token_expires = time.time() + body.get("expires_in", 3600)
    return _token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }


def _order_summary(order: dict) -> dict:
    """Flatten an Orders v2 response into the fields the escrow flow uses."""
    approve_url = None
    for link in order.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            approve_url = link.get("href")
    auth = None
    for unit in order.get("purchase_units", []):
        auths = unit.get("payments", {}).get("authorizations", [])
        if auths:
            auth = auths[0]
            break
    return {
        "order_id": order["id"],
        "status": order.get("status"),
        "approve_url": approve_url,
        "authorization_id": auth["id"] if auth else None,
        "authorization_status": auth["status"] if auth else None,
    }


def create_order(amount_cents: int, currency: str, description: str,
                 payment_source: dict | None = None) -> dict:
    """Create an AUTHORIZE order.

    Without a payment_source the order is buyer-funded: it comes back
    CREATED with an approve_url, and authorize_order() will fail with
    ORDER_NOT_APPROVED until the buyer approves at that URL. With a
    payment_source (e.g. a card), PayPal treats creation as the payer's
    go-ahead and the order returns already carrying an authorization.
    """
    amount = f"{amount_cents / 100:.2f}"
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "amount": {"currency_code": currency, "value": amount},
            "description": description,
        }],
    }
    if payment_source:
        body["payment_source"] = payment_source
    r = requests.post(
        f"{_base_url()}/v2/checkout/orders",
        headers={**_headers(),
                 "PayPal-Request-Id": f"escrow-{uuid.uuid4().hex}"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return _order_summary(r.json())


def authorize_order(order_id: str) -> dict:
    """Authorize an APPROVED order: funds are held, nobody has them.

    Raises requests.HTTPError (422 ORDER_NOT_APPROVED) if the buyer has
    not approved the order yet.
    """
    r = requests.post(
        f"{_base_url()}/v2/checkout/orders/{order_id}/authorize",
        headers=_headers(),
        json={},
        timeout=30,
    )
    r.raise_for_status()
    return _order_summary(r.json())


def create_authorization(amount_cents: int, currency: str,
                         description: str,
                         payment_source: dict | None = None) -> dict:
    """Begin the funding flow: create the order (see create_order).

    Returns the order summary. authorization_id is set only when the
    order already carries an authorization (card-funded creation);
    for buyer-funded orders it is None and approve_url is set — the
    caller must wait for buyer approval, then call authorize_order().
    """
    return create_order(amount_cents, currency, description,
                        payment_source=payment_source)


def get_order(order_id: str) -> dict:
    """Read an order back from PayPal (source of truth for its state)."""
    r = requests.get(f"{_base_url()}/v2/checkout/orders/{order_id}",
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_authorization(authorization_id: str) -> dict:
    """Read an authorization back from PayPal."""
    r = requests.get(
        f"{_base_url()}/v2/payments/authorizations/{authorization_id}",
        headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def capture(authorization_id: str) -> dict:
    """Release: capture the held funds to the seller."""
    r = requests.post(
        f"{_base_url()}/v2/payments/authorizations/{authorization_id}/capture",
        headers={**_headers(),
                 "PayPal-Request-Id": f"capture-{uuid.uuid4().hex}"},
        json={},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def void(authorization_id: str) -> dict:
    """Refund: void the authorization, funds return to the buyer."""
    r = requests.post(
        f"{_base_url()}/v2/payments/authorizations/{authorization_id}/void",
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.text else {"status": "VOIDED"}
