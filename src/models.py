"""Core data models."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .policy import DEFAULT_POLICY


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
    WITHDRAWN = "withdrawn"    # terminal: proposer withdrew pre-acceptance


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
    # Default from policy (src/policy.py); 14 unless overridden.
    window_days: int = field(
        default_factory=lambda: DEFAULT_POLICY.default_window_days)
    release_conditions: str = "delivery confirmed to buyer address"
    # The agreed requirements list (agreement surface, 2026-10-26):
    # the concrete conditions both parties accept at proposal time.
    # Each entry is {"text": str, "check": str | None} — "check"
    # names the checklist item that verifies the condition, or None
    # for a manual-judgment condition no check can read. The list IS
    # the agreement: src/agreement.py renders its live status.
    requirements: list = field(default_factory=list)


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
    # Latest checklist results from the most recent monitor() pass
    # (display state for the agreement + per-item views; the audit
    # log remains the record of decisions).
    last_check_results: list = field(default_factory=list)
    # Milestone structure (2026-10-26): empty for single-lump deals,
    # which behave exactly as before. When present, the deal tracks
    # and settles per milestone (src/milestones.py).
    milestones: list = field(default_factory=list)
    # Per-party share-link tokens (demo-grade identity): the party
    # page + action POSTs require ?k=<token> matching the party when
    # tokens are present. Empty = tokenless (manager-created deals);
    # the web propose flow always issues them.
    share_tokens: dict = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MilestoneState(str, Enum):
    PENDING = "pending"            # proposed, not yet funded
    FUNDED_HELD = "funded-held"    # money held, work in progress
    EVIDENCE_IN = "evidence-in"    # evidence arrived, not yet decisive
    VERIFIED = "verified"          # Tier 0 verified; settling
    RELEASED = "released"          # terminal: share captured to seller
    REFUNDED = "refunded"          # terminal: share returned to buyer
    DISPUTED = "disputed"          # contested; resolution is explicit


@dataclass
class Milestone:
    """One independently tracked + settled slice of a deal.

    A milestone is a title, a completion requirement in plain words,
    the checklist item that verifies it (``check``; None = manual
    judgment, verified by buyer confirmation), its share of the deal
    total in cents, and its due date. Shares across a deal's
    milestones must sum to the deal total (validated at proposal).
    ``released_cents`` is set at settlement: the full share for a
    release, 0 for a refund, in between for a split resolution.
    """
    id: str
    title: str
    requirement: str
    amount_cents: int
    due_date: str  # ISO date the parties agreed to
    check: str | None = None
    state: MilestoneState = MilestoneState.PENDING
    buyer_confirmed_at: str | None = None
    released_cents: int | None = None
    settled_at: str | None = None


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
    # Per-item shadow reads (additive audit field, 2026-10-26): one
    # entry per forest fact — the checklist item it maps to, Tier 0's
    # result, Laya's per-item read (approve/disapprove/unknown) and
    # the agreement marker. Display + audit only; never adjudication.
    shadow_items: list | None = None
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
            "shadow_items": self.shadow_items,
            "split_release_cents": self.split_release_cents,
        }
