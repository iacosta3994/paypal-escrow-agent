"""PayPal implementation of MoneyAdapter.

A thin delegating wrapper over src/paypal_client.py — all REST
behavior (OAuth, Orders v2 shapes, error propagation) stays in that
module, exactly as tested against the sandbox.

Delegation is deliberately DYNAMIC: each method looks its function
up on the paypal_client module at call time. The existing test
suite monkeypatches paypal_client functions directly (e.g.
``monkeypatch.setattr(paypal_client, "capture", fake)`` in the
guard/idempotency/split tests, and the live sandbox helpers in
tests/e2e_support.py call the module too) — resolving through the
module keeps every one of those fixtures working unchanged.
"""
from .. import paypal_client


class PayPalMoneyAdapter:
    """MoneyAdapter backed by the PayPal Orders v2 client."""

    def create_authorization(self, amount_cents, currency, description,
                             payment_source=None):
        return paypal_client.create_authorization(
            amount_cents, currency, description,
            payment_source=payment_source)

    def authorize_order(self, order_id):
        return paypal_client.authorize_order(order_id)

    def get_order(self, order_id):
        return paypal_client.get_order(order_id)

    def get_authorization(self, authorization_id):
        return paypal_client.get_authorization(authorization_id)

    def capture(self, authorization_id, amount_cents=None,
                currency="USD", final_capture=False):
        # Preserve the pre-A1 CALL SHAPES exactly: the kernel's full
        # capture was paypal_client.capture(auth_id) with no kwargs,
        # and the split capture passed amount/currency/final_capture
        # as kwargs. Callers (and test doubles) that only accept the
        # bare shape must not see arguments the old code never sent.
        if amount_cents is None:
            return paypal_client.capture(authorization_id)
        return paypal_client.capture(
            authorization_id, amount_cents=amount_cents,
            currency=currency, final_capture=final_capture)

    def void(self, authorization_id):
        return paypal_client.void(authorization_id)
