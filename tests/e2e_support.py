"""Shared helpers for the end-to-end test suite.

Real examples throughout: the demo deals ($500 camera / $300 keyboard /
$1,200 laptop) against the REAL PayPal sandbox. Nothing here mocks
PayPal; the only doubles are (a) the Grok tier-2 stub — Grok is a
reserved tier whose unavailability path (INVESTIGATE) is itself product
behavior, and it is out of credits — and (b) call-counting spies in the
guard/idempotency unit tests, which test DealManager's call discipline
rather than PayPal.

SAFETY: sandbox tests refuse to run unless PAYPAL_MODE is literally
"sandbox" — this suite must never touch live PayPal.
"""
import pytest

from src import audit, config, paypal_client
from src.models import DealTerms

ZIP = "47172"
CAMERA = (50000, "Used mirrorless camera")
KEYBOARD = (30000, "Mechanical keyboard")
LAPTOP = (120000, "Laptop")

# PayPal-published sandbox test card. Funding an order with a card
# payment_source is PayPal's server-side (ACDC-style) flow: creation IS
# the payer's go-ahead, so the order returns already authorized. This
# is what lets the suite exercise real authorize/capture/void without
# a browser. Buyer-account-funded orders still need the approve-URL
# login — see test_sandbox_money_path.py's module docstring.
TEST_CARD_SOURCE = {
    "card": {
        "number": "4032038634279578",
        "expiry": "2030-12",
        "security_code": "123",
        "name": "Test Buyer",
        "billing_address": {
            "address_line_1": "2211 North First Street",
            "admin_area_2": "San Jose",
            "admin_area_1": "CA",
            "postal_code": "95131",
            "country_code": "US",
        },
    }
}


def make_terms(amount_item) -> DealTerms:
    amount_cents, item = amount_item
    return DealTerms(
        amount_cents=amount_cents, currency="USD",
        item_description=item,
        buyer_email="buyer@example.com", seller_email="seller@example.com",
        delivery_zip=ZIP, window_days=14,
    )


def sandbox_ready() -> bool:
    return (config.PAYPAL_MODE == "sandbox"
            and bool(config.PAYPAL_CLIENT_ID)
            and bool(config.PAYPAL_CLIENT_SECRET))


requires_sandbox = pytest.mark.skipif(
    not sandbox_ready(),
    reason="needs PayPal sandbox credentials and PAYPAL_MODE=sandbox "
           f"(mode is {config.PAYPAL_MODE!r}) — refusing to run "
           "against anything but the sandbox",
)


# --- PayPal-side truth helpers (read back, never trust local state) ---

def pp_order(order_id: str) -> dict:
    return paypal_client.get_order(order_id)


def pp_auth(authorization_id: str) -> dict:
    return paypal_client.get_authorization(authorization_id)


def order_payments(order_id: str) -> dict:
    order = pp_order(order_id)
    units = order.get("purchase_units", [])
    return units[0].get("payments", {}) if units else {}


def order_captures(order_id: str) -> list:
    return order_payments(order_id).get("captures", [])


def audit_rows_for(deal_id: str) -> list:
    return [r for r in audit.read_all() if r.get("deal_id") == deal_id]


# --- fixtures ---

@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Redirect the audit log to a temp file for this test."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(path))
    return path


@pytest.fixture
def no_grok(monkeypatch):
    """Disable the reserved Grok tier: its failure path (INVESTIGATE)
    is the documented product fallback, and the account is out of
    credits. Tier 0 + the shadow forest still run for real."""
    from src import decision_engine

    def _unavailable(deal, results):
        raise ImportError("XAI disabled in tests")

    monkeypatch.setattr(decision_engine, "_tier2_grok", _unavailable)


@pytest.fixture
def held_cleanup():
    """Register funded deals; void any authorization still held at
    teardown so failed tests don't leave sandbox money dangling."""
    deals = []
    yield deals
    for deal in deals:
        if deal.paypal_auth_id and deal.state.value not in (
                "released", "refunded"):
            try:
                paypal_client.void(deal.paypal_auth_id)
            except Exception:
                pass  # teardown hygiene only; the test already reported
