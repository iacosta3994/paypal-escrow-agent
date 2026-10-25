"""The verification checklist array.

This is the product's IP: which checks exist, their weights, and the
escalation thresholds. Each check returns pass / fail / unknown + evidence.
Checks are deterministic and free — the LLM only adjudicates, it doesn't
gather.
"""
from dataclasses import dataclass

from .models import CheckResult, CheckStatus, Deal


@dataclass
class Check:
    name: str
    weight: float
    run: callable  # (deal, evidence) -> CheckResult


def _tracking_exists(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking")
    if t and t.get("number"):
        return CheckResult("tracking_exists", CheckStatus.PASS,
                           f"tracking {t['number']} via {t.get('carrier')}", 1.0)
    return CheckResult("tracking_exists", CheckStatus.FAIL,
                       "no tracking number provided", 1.0)


def _pickup_confirmed(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking", {})
    if t.get("pickup_scan"):
        return CheckResult("pickup_confirmed", CheckStatus.PASS,
                           f"pickup {t['pickup_scan']}", 1.2)
    if t.get("number"):
        # A fresh label with no pickup scan isn't proof of non-shipment;
        # only a stalled label is a hard fail.
        if t.get("stalled_days", 0) >= 5:
            return CheckResult("pickup_confirmed", CheckStatus.FAIL,
                               f"label created, no pickup in "
                               f"{t['stalled_days']} days", 1.5)
        return CheckResult("pickup_confirmed", CheckStatus.UNKNOWN,
                           "label created, awaiting pickup scan", 0.6)
    return CheckResult("pickup_confirmed", CheckStatus.UNKNOWN,
                       "no tracking data", 0.5)


def _in_transit_progressing(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking", {})
    if t.get("stalled_days", 0) >= 5:
        return CheckResult("in_transit", CheckStatus.FAIL,
                           f"no scan in {t['stalled_days']} days", 1.3)
    if t.get("in_transit"):
        return CheckResult("in_transit", CheckStatus.PASS,
                           "scans progressing", 0.8)
    return CheckResult("in_transit", CheckStatus.UNKNOWN, "no transit data", 0.5)


def _delivered(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking", {})
    if t.get("delivered"):
        # Partial shipment: a delivery scan for SOME of the items is
        # not delivery of the deal. UNKNOWN (not PASS, not FAIL): the
        # remainder may still be in transit, so the case investigates
        # instead of releasing the full amount or refunding it.
        expected = t.get("items_expected")
        got = t.get("items_delivered")
        if expected and got is not None and got < expected:
            return CheckResult("delivered", CheckStatus.UNKNOWN,
                               f"partial delivery: {got} of {expected} "
                               f"items delivered", 0.9,
                               detail={"items_delivered": got,
                                       "items_expected": expected})
        return CheckResult("delivered", CheckStatus.PASS,
                           f"delivered {t.get('delivered_at')}", 2.0)
    if ev.get("buyer_confirmed"):
        # No delivery scan, but the buyer — the recipient — says the
        # item arrived. That is delivery evidence in its own right.
        return CheckResult("delivered", CheckStatus.PASS,
                           f"buyer confirmed receipt at "
                           f"{ev.get('receipt_confirmed_at')}", 2.0)
    return CheckResult("delivered", CheckStatus.UNKNOWN, "no delivery scan", 0.5)


def _address_match(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking", {})
    if t.get("delivered_zip") == deal.terms.delivery_zip:
        return CheckResult("address_match", CheckStatus.PASS,
                           f"delivered ZIP {t['delivered_zip']} matches", 1.5)
    if t.get("delivered_zip"):
        return CheckResult("address_match", CheckStatus.FAIL,
                           f"delivered ZIP {t['delivered_zip']} != "
                           f"{deal.terms.delivery_zip}", 1.8)
    return CheckResult("address_match", CheckStatus.UNKNOWN,
                       "no delivery ZIP", 0.5)


def _within_window(deal: Deal, ev: dict) -> CheckResult:
    days = ev.get("days_since_funding", 0)
    if days <= deal.terms.window_days:
        return CheckResult("within_window", CheckStatus.PASS,
                           f"day {days} of {deal.terms.window_days}", 0.8)
    return CheckResult("within_window", CheckStatus.FAIL,
                       f"day {days} exceeds {deal.terms.window_days}-day window",
                       1.4)


def _amount_match(deal: Deal, ev: dict) -> CheckResult:
    if ev.get("authorized_cents") == deal.terms.amount_cents:
        return CheckResult("amount_match", CheckStatus.PASS,
                           "authorized == deal amount", 1.0)
    return CheckResult("amount_match", CheckStatus.FAIL,
                       "authorized != deal amount", 1.6)


def _no_dispute(deal: Deal, ev: dict) -> CheckResult:
    if ev.get("dispute_filed"):
        return CheckResult("no_dispute", CheckStatus.FAIL,
                           "dispute filed by a party", 1.7)
    return CheckResult("no_dispute", CheckStatus.PASS, "no disputes", 0.7)


def _seller_history(deal: Deal, ev: dict) -> CheckResult:
    rate = ev.get("seller_completion_rate")
    if rate is None:
        return CheckResult("seller_history", CheckStatus.UNKNOWN,
                           "no history", 0.4)
    if rate >= 0.9:
        return CheckResult("seller_history", CheckStatus.PASS,
                           f"{rate:.0%} prior completion", 0.6)
    return CheckResult("seller_history", CheckStatus.FAIL,
                       f"{rate:.0%} prior completion", 1.0)


def _condition_clear(deal: Deal, ev: dict) -> CheckResult:
    t = ev.get("tracking", {})
    msgs = " ".join(ev.get("messages", [])).lower()
    problems = []
    if t.get("exception"):
        problems.append(f"carrier exception: {t['exception']}")
    for phrase in ("dented", "damaged", "wrong item", "not what",
                   "broken", "not working"):
        if phrase in msgs:
            problems.append(f"buyer reports: '{phrase}'")
            break
    # A buyer damage report submitted as dispute evidence is a
    # condition problem the same way a buyer message is — and,
    # unlike the dispute flag itself, it stays on the record after
    # the dispute is resolved/closed.
    for record in ev.get("dispute_evidence") or []:
        if (record.get("party") == "buyer"
                and record.get("kind") == "damage_report"):
            problems.append(
                f"buyer dispute evidence: "
                f"{record.get('text') or 'damage reported via dispute'}")
    if problems:
        return CheckResult("condition_clear", CheckStatus.FAIL,
                           "; ".join(problems), 1.8)
    return CheckResult("condition_clear", CheckStatus.UNKNOWN,
                       "no condition report", 0.4)


def _dispute_resolution(deal: Deal, ev: dict) -> CheckResult:
    """An accepted settlement offer on an open dispute.

    PASS carries the agreed split amount in ``detail`` so a Tier 0
    rule can settle SPLIT directly off this result (that wiring
    lives in the decision engine, not here). This check never
    passes or fails on its own authority: with no open dispute, or
    an open dispute with no buyer offer, it is UNKNOWN.
    """
    if not ev.get("dispute_filed", deal.dispute_filed):
        return CheckResult("dispute_resolution", CheckStatus.UNKNOWN,
                           "no open dispute", 1.0)
    offers = [r for r in ev.get("dispute_evidence") or []
              if r.get("party") == "buyer"
              and r.get("kind") == "settlement_offer"]
    if offers:
        # Latest offer wins.
        x = (offers[-1].get("data") or {}).get("split_release_cents")
        return CheckResult(
            "dispute_resolution", CheckStatus.PASS,
            f"buyer accepts split settlement: release {x} of "
            f"{deal.terms.amount_cents} cents to close the dispute",
            1.0, detail={"split_release_cents": x})
    return CheckResult("dispute_resolution", CheckStatus.UNKNOWN,
                       "dispute open; no accepted settlement offer", 1.0)


CHECKS: list[Check] = [
    Check("tracking_exists", 1.0, _tracking_exists),
    Check("pickup_confirmed", 1.2, _pickup_confirmed),
    Check("in_transit", 0.8, _in_transit_progressing),
    Check("delivered", 2.0, _delivered),
    Check("address_match", 1.5, _address_match),
    Check("within_window", 0.8, _within_window),
    Check("amount_match", 1.0, _amount_match),
    Check("no_dispute", 0.7, _no_dispute),
    Check("seller_history", 0.6, _seller_history),
    Check("condition_clear", 0.4, _condition_clear),
    Check("dispute_resolution", 1.0, _dispute_resolution),
]


def run_checklist(deal: Deal, evidence: dict) -> list[CheckResult]:
    """Run every check against the deal + gathered evidence."""
    return [c.run(deal, evidence) for c in CHECKS]
