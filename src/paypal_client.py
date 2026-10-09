"""PayPal sandbox client: authorize / capture / void.

Uses the Orders API v2 directly over REST. No SDK needed.
Docs: https://developer.paypal.com/docs/api/orders/v2/
"""
import base64
import time

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


def create_authorization(amount_cents: int, currency: str,
                         description: str) -> dict:
    """Create an order and authorize it: funds are held, nobody has them."""
    amount = f"{amount_cents / 100:.2f}"
    r = requests.post(
        f"{_base_url()}/v2/checkout/orders",
        headers={**_headers(), "PayPal-Request-Id": f"escrow-{int(time.time()*1000)}"},
        json={
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": amount},
                "description": description,
            }],
        },
        timeout=30,
    )
    r.raise_for_status()
    order = r.json()
    order_id = order["id"]
    # Authorize the order (in sandbox with a test buyer approval this
    # normally happens via the approval flow; for the demo the simulator
    # stands in until buyer approval is wired).
    r = requests.post(
        f"{_base_url()}/v2/checkout/orders/{order_id}/authorize",
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    auth = r.json()["purchase_units"][0]["payments"]["authorizations"][0]
    return {"order_id": order_id, "authorization_id": auth["id"],
            "status": auth["status"]}


def capture(authorization_id: str) -> dict:
    """Release: capture the held funds to the seller."""
    r = requests.post(
        f"{_base_url()}/v2/payments/authorizations/{authorization_id}/capture",
        headers={**_headers(),
                 "PayPal-Request-Id": f"capture-{int(time.time()*1000)}"},
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
