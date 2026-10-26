"""Milestones: a deal's agreement structured as independently
tracked, independently settled slices.

Each milestone names the checklist item that verifies it (or None
for manual judgment, verified by buyer confirmation). Tier 0's
rules apply per milestone, unchanged: a decisive PASS releases the
share, a hard FAIL refunds it, a condition FAIL disputes it
(INVESTIGATE — judgment is required, never a guess), and UNKNOWN
means the evidence is in but inconclusive. The Laya shadow reads
stay per item exactly as before (Decision.shadow_items); they are
rendered under their milestone in the per-item view and remain
advisory: they can flag, they can never settle a milestone.

Money shape (single authorization, proven primitives): a release
captures that milestone's share with a non-final partial capture —
the same first leg as the proven SPLIT path — leaving the rest
held. Refunded shares are decided per milestone and voided when
the authorization closes (a PayPal authorization can be voided
only once), which happens when the last open milestone resolves.
See README limitations.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import (CheckStatus, Deal, Milestone, MilestoneState,
                     Verdict)

# The hard-fail checks, mirrored from decision_engine._HARD_FAILS:
# a FAIL here is seller non-performance, so the share refunds
# without a judgment call. A condition_clear FAIL is NOT here —
# it disputes, exactly as Tier 0 treats it deal-wide.
HARD_FAIL_CHECKS = {"pickup_confirmed", "within_window", "amount_match"}

OPEN_STATES = (MilestoneState.PENDING, MilestoneState.FUNDED_HELD,
               MilestoneState.EVIDENCE_IN, MilestoneState.VERIFIED,
               MilestoneState.DISPUTED)
SETTLED_STATES = (MilestoneState.RELEASED, MilestoneState.REFUNDED)

# Evidence kinds a party may attach to a disputed milestone
# (settlement offers stay deal-level: milestone resolution has its
# own explicit release / refund / split actions).
EVIDENCE_KINDS = {"note", "tracking_proof", "damage_report",
                  "condition_proof"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_milestones(deal_amount_cents: int,
                        milestones: list[Milestone]) -> None:
    """Proposal-time validation. Raises ValueError on any problem:
    the shares must be positive and sum to the deal total exactly,
    ids must be unique, due dates must be ISO dates."""
    if not milestones:
        return
    total = 0
    seen = set()
    for m in milestones:
        if m.id in seen:
            raise ValueError(f"duplicate milestone id {m.id!r}")
        seen.add(m.id)
        if not isinstance(m.amount_cents, int) \
                or isinstance(m.amount_cents, bool) \
                or m.amount_cents <= 0:
            raise ValueError(
                f"milestone {m.id!r} share must be a positive "
                f"number of cents, got {m.amount_cents!r}")
        total += m.amount_cents
        try:
            datetime.fromisoformat(m.due_date).date()
        except (ValueError, TypeError):
            raise ValueError(
                f"milestone {m.id!r} due date {m.due_date!r} is "
                f"not an ISO date")
        if not m.title.strip() or not m.requirement.strip():
            raise ValueError(
                f"milestone {m.id!r} needs a title and a requirement")
    if total != deal_amount_cents:
        raise ValueError(
            f"milestone shares sum to {total} cents, but the deal "
            f"total is {deal_amount_cents} cents — shares must sum "
            f"to the total")


def get_milestone(deal: Deal, milestone_id: str) -> Milestone:
    for m in deal.milestones:
        if m.id == milestone_id:
            return m
    raise ValueError(f"unknown milestone {milestone_id!r}")


def evaluate_milestone(deal: Deal, milestone: Milestone,
                       results: list) -> tuple[Verdict | None, str]:
    """Tier 0, per milestone. Returns (verdict, reasoning) where
    verdict None means "no decision yet" (evidence inconclusive or
    not in). Settled and disputed milestones return None — a dispute
    is resolved by an explicit resolution action, never silently.
    """
    if milestone.state in SETTLED_STATES \
            or milestone.state == MilestoneState.DISPUTED:
        return None, "milestone is not open for evaluation"
    if milestone.check is None:
        # Manual judgment: the buyer's confirmation is the evidence
        # this milestone runs on (confirmation is evidence, and for
        # a manual item it is the decisive evidence there is).
        if milestone.buyer_confirmed_at is not None:
            return (Verdict.RELEASE,
                    f"Manual milestone '{milestone.title}': buyer "
                    f"confirmed completion at "
                    f"{milestone.buyer_confirmed_at}.")
        return None, "manual milestone awaiting buyer confirmation"
    by_name = {r.name: r for r in results}
    result = by_name.get(milestone.check)
    if result is None:
        return None, f"check '{milestone.check}' has not run"
    if result.status == CheckStatus.PASS:
        return (Verdict.RELEASE,
                f"Milestone '{milestone.title}': check "
                f"'{milestone.check}' passes — {result.evidence}.")
    if result.status == CheckStatus.FAIL:
        if milestone.check in HARD_FAIL_CHECKS:
            return (Verdict.REFUND,
                    f"Milestone '{milestone.title}': hard fail on "
                    f"'{milestone.check}' — {result.evidence}.")
        return (Verdict.INVESTIGATE,
                f"Milestone '{milestone.title}': check "
                f"'{milestone.check}' failed — {result.evidence}. "
                f"Judgment is required; the share is not guessed.")
    return (None,
            f"Milestone '{milestone.title}': check "
            f"'{milestone.check}' is unknown — {result.evidence}.")


def allowed_milestone_actions(deal: Deal, party: str,
                              milestone: Milestone) -> set[str]:
    """THE permission function for milestone actions — used both to
    render a milestone's forms and to enforce its POSTs, so the two
    can never drift (same pattern as app.allowed_actions).

    - confirm_milestone: the buyer, while the deal is FUNDED or
      MONITORING and the milestone is open and unconfirmed.
    - dispute_milestone: either party, same deal states, milestone
      open and not already disputed.
    - resolve_milestone_release / _refund / _split: either party
      (the demo's referee controls), milestone DISPUTED.
    Settled milestones and terminal deals allow nothing.
    """
    from .models import DealState
    if party not in ("buyer", "seller"):
        return set()
    if deal.state not in (DealState.FUNDED, DealState.MONITORING):
        return set()
    if milestone.state in SETTLED_STATES:
        return set()
    actions: set[str] = set()
    if milestone.state == MilestoneState.DISPUTED:
        actions.update(("evidence",
                        "resolve_milestone_release",
                        "resolve_milestone_refund",
                        "resolve_milestone_split"))
        return actions
    if party == "buyer" and milestone.buyer_confirmed_at is None:
        actions.add("confirm_milestone")
    actions.add("dispute_milestone")
    return actions


def open_milestone_dispute(deal: Deal, milestone: Milestone,
                           party: str, text: str = "") -> dict:
    """Open a dispute on ONE milestone: the milestone is marked
    DISPUTED and the opening is recorded on the deal's evidence
    timeline, tagged with the milestone id. The deal-wide dispute
    flag is NOT set — a milestone dispute does not poison the other
    milestones' checks."""
    milestone.state = MilestoneState.DISPUTED
    record = {
        "party": party,
        "kind": "note",
        "text": text or f"Dispute opened on milestone "
                        f"'{milestone.title}'",
        "file_ref": None,
        "data": {"milestone_id": milestone.id,
                 "milestone_dispute": "opened"},
        "submitted_at": _now_iso(),
    }
    deal.dispute_evidence.append(record)
    return record


def submit_milestone_evidence(deal: Deal, milestone: Milestone,
                              party: str, kind: str, text: str = "",
                              file_ref=None,
                              data: dict | None = None) -> dict:
    """Attach evidence to a DISPUTED milestone's record. Mirrors
    disputes.submit_evidence's validation for the shared kinds;
    the record is tagged with the milestone id so the per-item
    dispute surface can scope it."""
    if party not in ("buyer", "seller"):
        raise ValueError(
            f"party must be 'buyer' or 'seller', got {party!r}")
    if kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(EVIDENCE_KINDS)}, "
            f"got {kind!r}")
    if milestone.state != MilestoneState.DISPUTED:
        raise ValueError(
            f"milestone '{milestone.title}' is not disputed")
    data = dict(data or {})
    if kind == "tracking_proof":
        tracking_number = data.get("tracking_number")
        if not tracking_number or (isinstance(tracking_number, str)
                                   and not tracking_number.strip()):
            raise ValueError(
                "tracking_proof requires data with a non-empty "
                "'tracking_number'")
    data["milestone_id"] = milestone.id
    record = {
        "party": party,
        "kind": kind,
        "text": text,
        "file_ref": file_ref,
        "data": data,
        "submitted_at": _now_iso(),
    }
    deal.dispute_evidence.append(record)
    return record


def milestone_evidence(deal: Deal,
                       milestone: Milestone) -> list[dict]:
    """The evidence records tagged to one milestone, in order."""
    return [r for r in deal.dispute_evidence
            if (r.get("data") or {}).get("milestone_id") == milestone.id]
