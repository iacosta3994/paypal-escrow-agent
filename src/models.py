"""Core data models."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DealState(str, Enum):
    PROPOSED = "proposed"
    FUNDED = "funded"          # PayPal authorization holds the money
    MONITORING = "monitoring"
    DECIDED = "decided"
    RELEASED = "released"      # captured -> seller
    REFUNDED = "refunded"      # voided -> buyer
    ESCALATED = "escalated"    # humans decide


class Verdict(str, Enum):
    RELEASE = "release"
    REFUND = "refund"
    INVESTIGATE = "investigate"


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
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    evidence: str = ""
    weight: float = 1.0  # higher = more decisive


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
        }
