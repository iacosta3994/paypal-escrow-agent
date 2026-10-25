"""Policy-as-data: every operator-judgment threshold in ONE place.

Before this module (Wave 1 / A2, 2026-10-24), the numbers that decide
how patient, how suspicious, and how escalatory the escrow agent is
were scattered as literals across src/:

  - the default delivery window (14 days)        models.DealTerms
  - authorization-expiry warn/escalate ages      auth_expiry (20 / 25,
    lifetime 29)                                  auth_expiry.py
  - inconclusive cycles before escalation (3)     deal_manager.settle
  - the carrier stall threshold (5 days)          checklist
  - the seller completion-rate threshold (0.9)    checklist
  - the Tier 2 minimum confidence (0.75) and the  decision_engine
    shadow-routing bounds (<=2 unknowns, <=1 fail)

They now live in the frozen :class:`Policy` dataclass below.
``DEFAULT_POLICY`` reproduces the previously-hardcoded behavior
EXACTLY — tests/test_policy.py pins every default to its old literal
and proves an override changes behavior in the expected place without
a code edit.

How to override, in order of specificity:

  1. Constructor injection — pass ``policy=Policy(...)`` (or
     ``DEFAULT_POLICY.with_overrides(...)``) to DealManager, or pass
     ``policy=`` to run_checklist / adjudicate / expiry_status.
  2. Environment — ``ESCROW_POLICY_<FIELD>`` variables (e.g.
     ``ESCROW_POLICY_AUTH_WARN_AT_DAYS=18``), read once at import in
     the same style as src/config.py.

What is deliberately NOT policy (structural, left in code):

  - state and verdict names (models.DealState / Verdict);
  - API endpoints, PayPal's URL shapes, request/response plumbing;
  - the split-settlement bounds ``0 < split < amount``: that is an
    arithmetic invariant of a split (both legs must be non-empty and
    sum to the deal amount), not a tunable fraction — there is no
    separate min/max fraction anywhere in the current code, so none
    was invented here;
  - the per-verdict confidence VALUES a Tier 0 rule reports (0.95,
    0.97, ...): those are emitted decision data pinned by the golden
    corpus, not thresholds that gate behavior;
  - check weights: they travel with each CheckResult as product IP
    in checklist.py, and no behavior branches on their magnitudes
    outside the (untouched) Laya sensor.
"""
from dataclasses import dataclass, fields, replace

from . import config


def _env_overrides() -> dict:
    """ESCROW_POLICY_<FIELD>=value for each Policy field, coerced to
    the field's declared type. Reads via config.get, config.py's own
    env accessor, so policy and settings share one style."""
    out: dict = {}
    for f in fields(Policy):
        raw = config.get(f"ESCROW_POLICY_{f.name.upper()}")
        if raw == "":
            continue
        if f.type is int:
            out[f.name] = int(raw)
        elif f.type is float:
            out[f.name] = float(raw)
        else:
            out[f.name] = raw
    return out


@dataclass(frozen=True)
class Policy:
    """Operator-judgment thresholds. Defaults = pre-A2 literals."""

    # Delivery window applied when deal terms don't state one.
    default_window_days: int = 14

    # Authorization expiry (src/auth_expiry.py): PayPal authorizations
    # live 29 days; warn the operator at 20, escalate to humans at 25.
    auth_expiry_days: int = 29
    auth_warn_at_days: int = 20
    auth_escalate_at_days: int = 25

    # Inconclusive (INVESTIGATE) monitoring cycles a deal may run
    # before DealManager.settle escalates it to humans.
    investigate_escalation_cycles: int = 3

    # Checklist thresholds (src/checklist.py): a tracking label with
    # no scan for this many days is stalled; a seller at or above
    # this prior completion rate passes seller_history.
    stall_days: int = 5
    seller_completion_threshold: float = 0.9

    # Decision-engine routing (src/decision_engine.py): a Tier 2
    # verdict below this confidence is forced to INVESTIGATE, and a
    # Tier-0-unsettled case stays on the shadow-sensor path (rather
    # than spending Tier 2) while unknowns/fails are within bounds.
    tier2_min_confidence: float = 0.75
    shadow_max_unknowns: int = 2
    shadow_max_fails: int = 1

    # Code-deliverable deals (Wave 2 / B, src/code_delivery/):
    # ADDITIVE keys with defaults — no physical-goods behavior reads
    # them. A graded code delivery at or above the release threshold
    # releases in full; at or above the split minimum (and below the
    # release threshold) it splits at the measured fraction; below
    # the split minimum it is treated as nothing delivered (refund).
    code_release_threshold: float = 0.9
    code_split_min_fraction: float = 0.2

    # -- derived remaining-time thresholds (auth expiry) --
    @property
    def auth_days_left_warn(self) -> int:
        """Warn when this many (or fewer) days remain: 29 - 20 = 9."""
        return self.auth_expiry_days - self.auth_warn_at_days

    @property
    def auth_days_left_escalate(self) -> int:
        """Escalate when this many (or fewer) remain: 29 - 25 = 4."""
        return self.auth_expiry_days - self.auth_escalate_at_days

    def with_overrides(self, **changes) -> "Policy":
        """A copy of this policy with named fields replaced."""
        return replace(self, **changes)

    @classmethod
    def from_env(cls) -> "Policy":
        """Defaults, overridden by ESCROW_POLICY_* env variables."""
        return cls(**_env_overrides())


# Read once at import, like config.py's module-level settings.
DEFAULT_POLICY = Policy.from_env()
