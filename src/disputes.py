"""Dispute evidence submitted by the parties on an open dispute.

Storage lives on the deal itself (``Deal.dispute_evidence``, laid
down by Track L1): an append-only list of records in submission
order. This module is the write path (validated submissions) and
the read path (the timeline a party / operator sees). Submitted
evidence feeds the normal evidence/checklist path — see
``evidence.gather_evidence`` (tracking-proof merge) and
``checklist`` (damage reports, settlement offers) — instead of the
dispute being a bare boolean flag.
"""
import copy
from datetime import datetime, timezone

from .models import Deal

KINDS = {"tracking_proof", "damage_report", "condition_proof",
         "settlement_offer", "note"}

_PARTIES = {"buyer", "seller"}


def submit_evidence(deal: Deal, party: str, kind: str, text: str = "",
                    file_ref=None, data: dict | None = None) -> dict:
    """Attach one piece of evidence to the deal's OPEN dispute.

    Returns the stored record. Raises ValueError on any invalid
    submission; a rejected submission appends nothing.
    """
    if party not in _PARTIES:
        raise ValueError(
            f"party must be 'buyer' or 'seller', got {party!r}")
    if kind not in KINDS:
        raise ValueError(
            f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    if not deal.dispute_filed:
        raise ValueError("no open dispute on this deal")
    data = data or {}
    if kind == "tracking_proof":
        tracking_number = data.get("tracking_number")
        if not tracking_number or (isinstance(tracking_number, str)
                                   and not tracking_number.strip()):
            raise ValueError(
                "tracking_proof requires data with a non-empty "
                "'tracking_number'")
    if kind == "settlement_offer":
        if party != "buyer":
            raise ValueError(
                "only the buyer can submit a settlement_offer")
        amount = data.get("split_release_cents")
        if (not isinstance(amount, int) or isinstance(amount, bool)
                or not 0 < amount < deal.terms.amount_cents):
            raise ValueError(
                "settlement_offer requires data with "
                "'split_release_cents' an int with "
                f"0 < x < {deal.terms.amount_cents}, got {amount!r}")
    record = {
        "party": party,
        "kind": kind,
        "text": text,
        "file_ref": file_ref,
        "data": data,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    deal.dispute_evidence.append(record)
    return record


def timeline(deal: Deal) -> list[dict]:
    """The dispute evidence timeline: a copy of the deal's records
    in submission order. Mutating the returned list (or its records)
    does not touch the deal's stored evidence."""
    return copy.deepcopy(deal.dispute_evidence)
