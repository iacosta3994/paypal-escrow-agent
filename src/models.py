"""Core data models."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DealState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"  # order created; buyer must approve
    FUNDED = "funded"          # PayPal authorization holds the money
    MONITORING = "monitoring"
    DECIDED = "decided"
    RELEASED = "released"      # captured -> seller
    REFUNDED = "refunded"      # voided -> buyer
    ESCALATED = "escalated"    # humans decide
    DECLINED = "declined"      # terminal: terms declined, no money moved
    SPLIT_SETTLED = "split_settled"  # terminal: split capture/void done


class Verdict(str, Enum):
    RELEASE = "release"
    REFUND = "refund"
    INVESTIGATE = "investigate"
    SPLIT = "split"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class DealTerms:
    amount_cents: int
    currency: str
    item_description: str
    buyer_email: str
    seller_email: str
    delivery_zip: str
    window_days: int = 14
    release_conditions: str = "delivery confirmed to buyer address"


@dataclass
class Deal:
    id: str
    terms: DealTerms
    state: DealState = DealState.PROPOSED
    paypal_order_id: str | None = None
    paypal_auth_id: str | None = None
    paypal_approve_url: str | None = None  # buyer approval link, when pending
    dispute_filed: bool = False  # a party has an open dispute on this deal
    # Two-party lifecycle (L1 seams): who proposed the terms (None =
    # manager-proposed / pre-agreed), each party's acceptance
    # timestamp, buyer receipt confirmation / approval, dispute
    # evidence, and split-settlement bookkeeping.
    proposed_by: str | None = None
    terms_acceptances: dict[str, str] = field(default_factory=dict)
    buyer_confirmed_at: str | None = None
    buyer_approved_at: str | None = None
    dispute_evidence: list[dict] = field(default_factory=list)
    capture_id: str | None = None
    split_released_cents: int | None = None
    split_voided_cents: int | None = None
    # Authorization aging (S5): when the money was held, and — read
    # back from PayPal's authorization object when available — when
    # the hold was created and when it expires. PayPal fully honors
    # an authorization ~3 days and it expires at 29; see auth_expiry.
    funded_at: str | None = None
    paypal_auth_created_at: str | None = None
    paypal_auth_expires_at: str | None = None
    auth_expiry_warned: bool = False     # 20-day flag already recorded
    auth_expiry_escalated: bool = False  # 25-day escalation already done
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    evidence: str = ""
    weight: float = 1.0  # higher = more decisive
    detail: dict = field(default_factory=dict)  # generic structured payload


@dataclass
class Decision:
    deal_id: str
    verdict: Verdict
    confidence: float
    reasoning: str
    key_evidence: list[str] = field(default_factory=list)
    tier: int = 0            # 0=deterministic, 1=cheap LLM, 2=deep reasoning
    cost_usd: float = 0.0    # tracked for the unit-economics story
    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Shadow mode (Tier 1 demoted to a sensor, 2026-10-23): the Laya
    # forest runs on every case Tier 0 doesn't settle, but its verdict is
    # only recorded next to the real decision — it never overrides it.
    # Populated only when the forest ran; None otherwise.
    shadow_verdict: str | None = None      # forest's verdict, as logged
    shadow_confidence: float | None = None  # forest's tree-agreement fraction
    agreement: float | None = None  # fraction of trees backing the final verdict
    split_release_cents: int | None = None  # for SPLIT: cents to capture

    def to_dict(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_evidence": self.key_evidence,
            "tier": self.tier,
            "cost_usd": self.cost_usd,
            "decided_at": self.decided_at,
            "shadow_verdict": self.shadow_verdict,
            "shadow_confidence": self.shadow_confidence,
            "agreement": self.agreement,
            "split_release_cents": self.split_release_cents,
        }
