"""Evidence gatherers + tracking simulator.

For the hackathon demo, tracking events are simulated (scripted scenarios).
The gatherer interface is carrier-shaped so a real API (AfterShip/EasyPost)
can be dropped in later without changing the checklist.

Kernel code reaches this module through the EvidenceAdapter
interface (Wave 1 / A1): src/adapters/simulator.py's
SimulatorEvidenceAdapter delegates to gather_evidence() here, and
src/adapters/base.py documents the evidence-dict contract a second
adapter (e.g. a GitHub code-deliverable gatherer) must satisfy.
"""
import random
from datetime import datetime, timezone

from .models import Deal


class TrackingSimulator:
    """Scripted tracking scenarios for the demo."""

    def __init__(self, scenario: str = "clean_delivery"):
        self.scenario = scenario
        self.day = 0

    def advance(self, days: int = 1) -> dict:
        self.day += days
        if self.scenario == "clean_delivery":
            return self._clean()
        if self.scenario == "never_ships":
            return self._never_ships()
        if self.scenario == "ambiguous":
            return self._ambiguous()
        raise ValueError(f"unknown scenario {self.scenario}")

    def _clean(self) -> dict:
        stages = [
            {"number": "1ZDEMO001", "carrier": "UPS", "pickup_scan": None},
            {"number": "1ZDEMO001", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True},
            {"number": "1ZDEMO001", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True},
            {"number": "1ZDEMO001", "carrier": "UPS",
             "pickup_scan": "2026-11-02T10:00:00Z",
             "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
             "delivered_zip": "47172"},
        ]
        return stages[min(self.day, len(stages) - 1)]

    def _never_ships(self) -> dict:
        return {"number": "1ZDEMO002", "carrier": "UPS",
                "stalled_days": self.day}

    def _ambiguous(self) -> dict:
        base = {"number": "1ZDEMO003", "carrier": "FedEx",
                "pickup_scan": "2026-11-03T09:00:00Z",
                "delivered": True, "delivered_at": "2026-11-06T11:00:00Z",
                "delivered_zip": "47172", "exception": "package visibly damaged"}
        return base


def _merge_dispute_tracking(deal: Deal, tracking: dict) -> dict:
    """Merge seller-submitted tracking proof into carrier tracking.

    Carrier data always wins: dispute-submitted tracking fills ONLY
    fields missing from the carrier tracking, never overwrites them.
    Only the LATEST seller tracking_proof record is considered, and
    only ``number`` / ``carrier`` / ``pickup_scan`` can be filled —
    delivery facts (delivered / delivered_zip / stalled_days) are
    never taken from submitted evidence.
    """
    merged = dict(tracking) if tracking else {}
    proofs = [r for r in deal.dispute_evidence
              if r.get("party") == "seller"
              and r.get("kind") == "tracking_proof"]
    if not proofs:
        return merged
    data = proofs[-1].get("data") or {}
    if not merged.get("number") and data.get("tracking_number"):
        merged["number"] = data["tracking_number"]
        if not merged.get("carrier") and data.get("carrier"):
            merged["carrier"] = data["carrier"]
    if not merged.get("pickup_scan") and data.get("pickup_scan"):
        merged["pickup_scan"] = data["pickup_scan"]
    return merged


def gather_evidence(deal: Deal, tracking: dict,
                    days_since_funding: int,
                    messages: list[str] | None = None) -> dict:
    """Assemble the evidence dict the checklist consumes."""
    return {
        "tracking": _merge_dispute_tracking(deal, tracking),
        "days_since_funding": days_since_funding,
        "authorized_cents": deal.terms.amount_cents,
        # From real deal state (DealManager.open_dispute), never a
        # constant — a hardcoded False here used to make the
        # checklist's dispute check unreachable through the product.
        "dispute_filed": deal.dispute_filed,
        # Dispute-submitted evidence (src/disputes.py), carried so
        # the checklist can read damage reports and settlement
        # offers the same way it reads everything else.
        "dispute_evidence": list(deal.dispute_evidence),
        "buyer_confirmed": deal.buyer_confirmed_at is not None,
        "receipt_confirmed_at": deal.buyer_confirmed_at,
        "seller_completion_rate": round(random.uniform(0.85, 1.0), 2),
        "messages": messages or [],
        "gathered_at": datetime.now(timezone.utc).isoformat(),
    }
