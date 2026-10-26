"""The agreement surface: what both parties agreed at the start,
shown live.

A deal's terms carry a requirements list (DealTerms.requirements):
the concrete conditions the parties accepted when the deal was
proposed. Each requirement either names the checklist item that
verifies it ("check") or is a manual-judgment condition no check
can read (check=None). This module renders that agreement:

- the agreed completion date, derived from the deal's creation
  date + its delivery window and always shown explicitly;
- each requirement's live status — pending (no monitoring pass
  yet), met / failed / unknown (from the latest checklist run),
  or manual (judged by the parties, never by a check).

Read-only presentation logic: nothing here adjudicates, moves
money, or mutates a deal. The kernel still owns every verdict.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import CheckStatus, Deal

# The standard verifiable conditions offered on the propose form:
# (checklist item, requirement text in the parties' words). The
# form may pre-check a subset; parties can also add their own
# free-text requirements, which land as manual-judgment entries.
STANDARD_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("pickup_confirmed", "Seller ships the item (carrier pickup scan)"),
    ("delivered", "Item is delivered to the buyer"),
    ("address_match", "Delivery ZIP matches the agreed delivery ZIP"),
    ("within_window", "Delivered within the agreed delivery window"),
    ("condition_clear",
     "Item condition as described — no damage reported"),
    ("amount_match", "Authorized amount equals the agreed amount"),
    ("no_dispute", "No dispute is open at settlement"),
)

# Pre-checked on the propose form: the conditions a typical
# shipment deal is agreed on.
DEFAULT_CHECKS: tuple[str, ...] = (
    "delivered", "address_match", "within_window", "condition_clear",
    "amount_match",
)


def standard_requirement(check: str) -> dict:
    """One requirements entry for a standard (check-linked) condition."""
    text = dict(STANDARD_REQUIREMENTS)[check]
    return {"text": text, "check": check}


def manual_requirement(text: str) -> dict:
    """One requirements entry for a free-text (manual) condition."""
    return {"text": text, "check": None}


def completion_date(deal: Deal) -> str:
    """The agreed completion date: proposal date + delivery window.

    Derived from the window the parties agreed to, and shown
    explicitly on every agreement surface — "within the window"
    is a date, not a vibe.
    """
    created = datetime.fromisoformat(deal.created_at)
    due = created + timedelta(days=deal.terms.window_days)
    return due.date().isoformat()


def requirement_status(deal: Deal, requirement: dict,
                         results: list | None = None) -> dict:
    """One requirement's live status against the latest evidence.

    Statuses: "pending" (check-linked, no monitoring pass yet),
    "met" / "failed" / "unknown" (from the check's latest result),
    "manual" (no check reads this condition — the parties judge it;
    shown, never machine-graded).
    """
    check = requirement.get("check")
    entry = {"text": requirement.get("text", ""), "check": check}
    if check is None:
        entry.update(status="manual",
                     evidence="manual judgment — not machine-checked; "
                              "the parties decide whether this is met")
        return entry
    results = deal.last_check_results if results is None else results
    if not results:
        entry.update(status="pending",
                     evidence="no monitoring pass yet")
        return entry
    by_name = {r.name: r for r in results}
    result = by_name.get(check)
    if result is None:
        entry.update(status="unknown",
                     evidence=f"check '{check}' did not run")
        return entry
    status = {CheckStatus.PASS: "met", CheckStatus.FAIL: "failed",
              CheckStatus.UNKNOWN: "unknown"}[result.status]
    entry.update(status=status, evidence=result.evidence)
    return entry


def agreement_status(deal: Deal, results: list | None = None) -> list[dict]:
    """Every requirement's live status, in agreement order."""
    return [requirement_status(deal, req, results)
            for req in deal.terms.requirements]


def acceptance_summary(deal: Deal) -> dict:
    """Who has accepted the terms, for the agreement header."""
    return {
        "proposed_by": deal.proposed_by,
        "buyer": deal.terms_acceptances.get("buyer"),
        "seller": deal.terms_acceptances.get("seller"),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
