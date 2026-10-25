"""Authorization-expiry surfacing (improvement plan S5, 2026-10-24).

Escrow holds are PayPal authorizations, and authorizations age out:
PayPal fully honors one for ~3 days and it expires at 29. A deal
whose seller goes quiet can sit in MONITORING while its hold quietly
walks toward expiry — and an expired authorization means the escrow
is empty: nothing left to capture or void. Monitoring therefore
tracks authorization age and surfaces it through the audit /
escalation path instead of letting the hold lapse silently:

  age >= 20 days  -> WARN: an audit record flags the deal
                     ("re-authorize or escalate")
  age >= 25 days  -> ESCALATE: audit record + the deal escalates to
                     humans with time still on the clock
  age >= 29 days  -> EXPIRED: same escalation, marked expired

When PayPal's own expiration_time was read back at funding, days
left is computed from it directly (escalate at <= 4 days left, warn
at <= 9 — the same thresholds in remaining-time terms).
"""
from datetime import datetime, timezone

from .models import Deal, DealState
from .policy import DEFAULT_POLICY, Policy

# Back-compat aliases (Wave 1 / A2): these values now live in
# src/policy.py as DEFAULT_POLICY fields; the names below remain so
# existing imports keep working. Treat them as read-only snapshots
# of the default policy, not as the place to tune thresholds.
EXPIRY_DAYS = DEFAULT_POLICY.auth_expiry_days        # 29
WARN_AT_DAYS = DEFAULT_POLICY.auth_warn_at_days      # 20
ESCALATE_AT_DAYS = DEFAULT_POLICY.auth_escalate_at_days  # 25
_DAYS_LEFT_WARN = DEFAULT_POLICY.auth_days_left_warn        # 9
_DAYS_LEFT_ESCALATE = DEFAULT_POLICY.auth_days_left_escalate  # 4


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def auth_age_days(deal: Deal,
                  now: datetime | None = None) -> float | None:
    """Days since the authorization was created, or None if the deal
    carries no usable timestamp (unfunded, or legacy deal)."""
    basis = _parse(deal.paypal_auth_created_at) or _parse(deal.funded_at)
    if basis is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - basis).total_seconds() / 86400.0


def expiry_status(deal: Deal, now: datetime | None = None,
                  policy: Policy | None = None) -> dict | None:
    """{age_days, days_left, level, expires_at} for a live deal, or
    None when expiry doesn't apply (settled, or nothing held yet).

    level: "ok" | "warn" | "escalate" | "expired".
    """
    policy = policy or DEFAULT_POLICY
    if deal.state in (DealState.RELEASED, DealState.REFUNDED):
        return None
    age = auth_age_days(deal, now)
    expires = _parse(deal.paypal_auth_expires_at)
    days_left = None
    if expires is not None:
        now = now or datetime.now(timezone.utc)
        days_left = (expires - now).total_seconds() / 86400.0
    if age is None and days_left is None:
        return None
    if days_left is not None:
        if days_left < 0:
            level = "expired"
        elif days_left <= policy.auth_days_left_escalate:
            level = "escalate"
        elif days_left <= policy.auth_days_left_warn:
            level = "warn"
        else:
            level = "ok"
    else:
        if age >= policy.auth_expiry_days:
            level = "expired"
        elif age >= policy.auth_escalate_at_days:
            level = "escalate"
        elif age >= policy.auth_warn_at_days:
            level = "warn"
        else:
            level = "ok"
        days_left = policy.auth_expiry_days - age
    return {"age_days": None if age is None else round(age, 2),
            "days_left": round(days_left, 2),
            "level": level,
            "expires_at": deal.paypal_auth_expires_at}
