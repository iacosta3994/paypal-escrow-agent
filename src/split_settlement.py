"""Split settlement execution: capture part, release the rest.

A SPLIT decision captures ``split_release_cents`` to the seller and
voids the remainder of the authorization back to the buyer. Integer
cents throughout; the two parts always sum to the deal amount.

PayPal shape — determined empirically against the sandbox
2026-10-24 (probe on $300.00 card-funded authorizations):

- Partial capture of $150.00 with ``final_capture=False`` -> 201,
  capture COMPLETED, authorization PARTIALLY_CAPTURED; a following
  void succeeds and the authorization reads back VOIDED. That is
  the shape shipped below: capture the split part, then void the
  authorization to release the remainder.
- Alternative considered and rejected: a single capture of $150.00
  with ``final_capture=True`` also completes, but the authorization
  then reads CAPTURED and a following void is rejected 422
  UNPROCESSABLE_ENTITY / PREVIOUSLY_CAPTURED ("Authorization has
  been previously captured and hence cannot be voided") — the
  remainder's release is then implicit in PayPal closing the
  authorization rather than an explicit, readable void, so the
  two-step shape above is the one with read-back proof for both
  legs.
"""
from . import paypal_client
from .models import Deal, Decision, Verdict


def execute_split(deal: Deal, decision: Decision,
                  use_paypal: bool) -> dict:
    """Execute a SPLIT decision for ``deal``.

    Returns {"capture_id", "released_cents", "voided_cents",
    "capture"} — capture_id/capture are None in simulated mode,
    where no PayPal calls are made at all.
    """
    if decision.verdict != Verdict.SPLIT:
        raise ValueError(
            f"execute_split requires a SPLIT decision, got "
            f"{decision.verdict.value}")
    x = decision.split_release_cents
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValueError(
            f"SPLIT decision must carry integer split_release_cents, "
            f"got {x!r}")
    amount = deal.terms.amount_cents
    if not 0 < x < amount:
        raise ValueError(
            f"split_release_cents must be strictly between 0 and "
            f"the deal amount ({amount}), got {x}")
    voided = amount - x
    if not use_paypal:
        return {"capture_id": None, "released_cents": x,
                "voided_cents": voided, "capture": None}
    if not deal.paypal_auth_id:
        raise RuntimeError(
            f"cannot split-settle deal {deal.id}: no PayPal "
            "authorization recorded — money would not move")
    capture = paypal_client.capture(
        deal.paypal_auth_id, amount_cents=x,
        currency=deal.terms.currency, final_capture=False)
    paypal_client.void(deal.paypal_auth_id)
    return {"capture_id": capture.get("id"), "released_cents": x,
            "voided_cents": voided, "capture": capture}
