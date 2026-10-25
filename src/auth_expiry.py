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

EXPIRY_DAYS = 29          # PayPal authorization lifetime
WARN_AT_DAYS = 20         # flag: re-authorize or escalate
ESCALATE_AT_DAYS = 25     # escalate to humans
_DAYS_LEFT_WARN = EXPIRY_DAYS - WARN_AT_DAYS        # 9
_DAYS_LEFT_ESCALATE = EXPIRY_DAYS - ESCALATE_AT_DAYS  # 4


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


def expiry_status(deal: Deal, now: datetime | None = None) -> dict | None:
    """{age_days, days_left, level, expires_at} for a live deal, or
    None when expiry doesn't apply (settled, or nothing held yet).

    level: "ok" | "warn" | "escalate" | "expired".
    """
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
        elif days_left <= _DAYS_LEFT_ESCALATE:
            level = "escalate"
        elif days_left <= _DAYS_LEFT_WARN:
            level = "warn"
        else:
            level = "ok"
    else:
        if age >= EXPIRY_DAYS:
            level = "expired"
        elif age >= ESCALATE_AT_DAYS:
            level = "escalate"
        elif age >= WARN_AT_DAYS:
            level = "warn"
        else:
            level = "ok"
        days_left = EXPIRY_DAYS - age
    return {"age_days": None if age is None else round(age, 2),
            "days_left": round(days_left, 2),
            "level": level,
            "expires_at": deal.paypal_auth_expires_at}
