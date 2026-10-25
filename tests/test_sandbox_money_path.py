"""Part A — the real sandbox money path, end to end through DealManager.

These tests move REAL (sandbox) money: orders, authorizations,
captures and voids are created on PayPal's sandbox and asserted by
reading PayPal's own state back — local deal state is never trusted
on its own.

How the funding legs work, honestly:
- Card-funded orders (payment_source = PayPal's published sandbox
  test card) are PayPal's server-side flow: creating the order is the
  payer's go-ahead, so the order returns with an authorization
  attached and funds held. This exercises authorize/capture/void for
  real, today.
- Buyer-account-funded orders (no payment_source) still require the
  buyer to log in at the approve URL — PayPal offers NO API for that
  step, and this project has no sandbox buyer credentials in its env,
  so the full buyer-login leg cannot complete in this environment.
  What IS tested for real: the order waits in AWAITING_APPROVAL with
  a genuine approve URL, PayPal shows it CREATED/unapproved, and
  completing funding early is refused by PayPal itself
  (422 ORDER_NOT_APPROVED). The credentialed completion test below
  activates when PAYPAL_SANDBOX_BUYER_EMAIL /
  PAYPAL_SANDBOX_BUYER_PASSWORD are provided; a human approval click
  works too (ESCROW_TEST_MANUAL_APPROVAL=1 polls for it).

Run: ~/laya-scratch-venv/bin/python -m pytest tests/ (sandbox creds in
.env; mode must be "sandbox" or every test here skips).
"""
import os
import time

import pytest
import requests

from src import paypal_client
from src.deal_manager import DealManager
from src.evidence import TrackingSimulator
from src.models import DealState, Verdict

from .e2e_support import (CAMERA, KEYBOARD, TEST_CARD_SOURCE, audit_log,
                          audit_rows_for, held_cleanup, make_terms,
                          no_grok, order_captures, pp_auth, pp_order,
                          requires_sandbox)

pytestmark = requires_sandbox


def test_card_funded_release_captures_for_real(audit_log, no_grok,
                                               held_cleanup):
    """$500 camera: fund (card) -> held -> delivered -> RELEASE ->
    capture. PayPal-side: authorization CREATED 500.00, then CAPTURED
    with a COMPLETED capture of 500.00."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)

    # Funding really happened, on PayPal's side.
    assert deal.state == DealState.FUNDED
    assert deal.paypal_order_id and deal.paypal_auth_id
    assert deal.paypal_approve_url is None
    order = pp_order(deal.paypal_order_id)
    assert order["status"] == "COMPLETED"  # authorized at creation
    auth = pp_auth(deal.paypal_auth_id)
    assert auth["status"] == "CREATED"  # held, nobody has it yet
    assert auth["amount"]["value"] == "500.00"
    assert auth["amount"]["currency_code"] == "USD"

    sim = TrackingSimulator("clean_delivery")
    # Day 1: in transit — no verdict yet, money stays held.
    decision, _ = mgr.monitor(deal, sim.advance(1), days_since_funding=1)
    assert decision.verdict == Verdict.INVESTIGATE
    mgr.settle(deal, decision)
    assert deal.state == DealState.MONITORING
    assert pp_auth(deal.paypal_auth_id)["status"] == "CREATED"
    assert order_captures(deal.paypal_order_id) == []

    # Day 3: delivered clean — Tier 0 RELEASE, settle captures.
    decision, _ = mgr.monitor(deal, sim.advance(2), days_since_funding=3)
    assert decision.verdict == Verdict.RELEASE
    assert decision.tier == 0
    mgr.settle(deal, decision)
    assert deal.state == DealState.RELEASED

    auth = pp_auth(deal.paypal_auth_id)
    assert auth["status"] == "CAPTURED"
    captures = order_captures(deal.paypal_order_id)
    assert len(captures) == 1
    assert captures[0]["status"] == "COMPLETED"
    assert captures[0]["amount"]["value"] == "500.00"

    rows = audit_rows_for(deal.id)
    assert [r["verdict"] for r in rows] == ["investigate", "release"]
    assert rows[-1]["tier"] == 0


def test_card_funded_refund_voids_for_real(audit_log, no_grok,
                                           held_cleanup):
    """$300 keyboard that never ships: fund (card) -> day-6 stall ->
    REFUND -> settle voids. PayPal-side: authorization VOIDED, no
    capture anywhere on the order."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    held_cleanup.append(deal)
    mgr.fund(deal, payment_source=TEST_CARD_SOURCE)
    assert deal.state == DealState.FUNDED
    assert pp_auth(deal.paypal_auth_id)["status"] == "CREATED"
    assert pp_auth(deal.paypal_auth_id)["amount"]["value"] == "300.00"

    sim = TrackingSimulator("never_ships")
    decision, _ = mgr.monitor(deal, sim.advance(6), days_since_funding=6)
    assert decision.verdict == Verdict.REFUND
    assert decision.tier == 0
    mgr.settle(deal, decision)
    assert deal.state == DealState.REFUNDED

    assert pp_auth(deal.paypal_auth_id)["status"] == "VOIDED"
    assert order_captures(deal.paypal_order_id) == []
    rows = audit_rows_for(deal.id)
    assert [r["verdict"] for r in rows] == ["refund"]


def test_buyer_account_funding_waits_for_real_approval(audit_log,
                                                       held_cleanup):
    """Buyer-funded leg: without buyer approval there is no held
    money, and PayPal itself refuses early authorization. This is the
    step that used to crash fund() with a raw 422; it is now a state
    (AWAITING_APPROVAL) with the genuine approve URL on the deal."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    held_cleanup.append(deal)
    mgr.fund(deal)  # no payment_source -> buyer must approve

    assert deal.state == DealState.AWAITING_APPROVAL
    assert deal.paypal_auth_id is None
    assert deal.paypal_approve_url
    assert deal.paypal_approve_url.startswith(
        "https://www.sandbox.paypal.com/")
    order = pp_order(deal.paypal_order_id)
    assert order["status"] == "CREATED"  # not approved, not authorized

    with pytest.raises(requests.HTTPError) as excinfo:
        mgr.complete_funding(deal)
    assert excinfo.value.response.status_code == 422
    assert "ORDER_NOT_APPROVED" in excinfo.value.response.text
    # Nothing moved and nothing was recorded as held.
    assert deal.state == DealState.AWAITING_APPROVAL
    assert deal.paypal_auth_id is None


BUYER_EMAIL = os.environ.get("PAYPAL_SANDBOX_BUYER_EMAIL", "")
BUYER_PASSWORD = os.environ.get("PAYPAL_SANDBOX_BUYER_PASSWORD", "")
MANUAL = os.environ.get("ESCROW_TEST_MANUAL_APPROVAL") == "1"


@pytest.mark.skipif(
    not ((BUYER_EMAIL and BUYER_PASSWORD) or MANUAL),
    reason="buyer-login approval needs sandbox buyer credentials "
           "(PAYPAL_SANDBOX_BUYER_EMAIL/PASSWORD) or a human click "
           "(ESCROW_TEST_MANUAL_APPROVAL=1). PayPal has no approval "
           "API and this project env has no buyer credentials, so "
           "this leg cannot complete unattended in this environment.",
)
def test_buyer_approval_completes_funding(audit_log, held_cleanup):
    """Full buyer-funded leg: approve at the approve URL (buyer login
    or a human click in manual mode), then complete_funding()
    authorizes and the money is really held; voided for cleanup."""
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(KEYBOARD))
    held_cleanup.append(deal)
    mgr.fund(deal)
    assert deal.state == DealState.AWAITING_APPROVAL

    if MANUAL:
        print(f"\nApprove this sandbox order as the buyer: "
              f"{deal.paypal_approve_url}")
        # The checkout token IS the order id; operators relaying the
        # approval to a browser session need the id in plain form.
        print(f"ORDER_ID={deal.paypal_order_id}", flush=True)
    else:
        _approve_as_buyer(deal.paypal_approve_url,
                          BUYER_EMAIL, BUYER_PASSWORD)

    deadline = time.time() + (900 if MANUAL else 120)  # manual = human-speed
    while time.time() < deadline:
        if pp_order(deal.paypal_order_id)["status"] == "APPROVED":
            break
        time.sleep(2)
    else:
        pytest.fail("order never reached APPROVED on PayPal's side")

    mgr.complete_funding(deal)
    assert deal.state == DealState.FUNDED
    assert deal.paypal_auth_id
    auth = pp_auth(deal.paypal_auth_id)
    assert auth["status"] == "CREATED"
    assert auth["amount"]["value"] == "300.00"

    paypal_client.void(deal.paypal_auth_id)  # leave nothing held
    assert pp_auth(deal.paypal_auth_id)["status"] == "VOIDED"


def _approve_as_buyer(approve_url: str, email: str, password: str):
    """Drive the sandbox buyer login at the approve URL.

    PayPal's login is a web flow (no API exists); this performs the
    standard sandbox sign-in form post with the buyer's own test
    credentials. Any failure raises — the caller's PayPal-side poll
    for APPROVED is the actual assertion, so a broken login fails the
    test loudly instead of faking approval.
    """
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (escrow-e2e-test)"
    s.get(approve_url, timeout=30)
    r = s.post(
        "https://www.sandbox.paypal.com/signin",
        data={"login_email": email, "login_password": password},
        timeout=30,
    )
    r.raise_for_status()
    # Return to the approval page so the session can confirm the order.
    s.get(approve_url, timeout=30)
