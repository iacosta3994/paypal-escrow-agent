"""Party notification outbox: one email-shaped record per party
per event, appended to a JSONL file at ``config.NOTIFICATIONS_PATH``.

The outbox is append-only and part of the audit trail by design:
every state change (via :func:`transition_listener`, registered on
the DealManager's transition listeners) and every explicit party
event the UI emits lands here, per party, so each party's page can
show exactly what they were told and when. ``audit.log.jsonl``
deliberately stays decisions-only — the dashboard feed, spend
totals, and idempotency consumers read it as a decision log, and
inflating it with notices would corrupt those semantics. No SMTP:
records are email-shaped (deal, party, address, event, message,
timestamp) so a real mailer could drain the outbox unchanged.
"""
import json
from datetime import datetime, timezone

from . import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_value(state) -> str:
    return state.value if hasattr(state, "value") else str(state)


def emit(deal, event: str, message: str) -> list[dict]:
    """Append ONE notice per party for `event` on `deal` to the
    outbox and return the records written (buyer first, then
    seller). Each record carries the party's email from the deal
    terms."""
    at = _now_iso()
    records = [
        {"deal_id": deal.id, "party": party, "email": email,
         "event": event, "message": message, "at": at}
        for party, email in (("buyer", deal.terms.buyer_email),
                             ("seller", deal.terms.seller_email))
    ]
    with open(config.NOTIFICATIONS_PATH, "a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    return records


def for_deal(deal_id: str) -> list[dict]:
    """All notices for one deal, in file (emission) order. A
    missing outbox file reads as no notices."""
    try:
        with open(config.NOTIFICATIONS_PATH) as f:
            records = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return [r for r in records if r.get("deal_id") == deal_id]


def for_party(deal_id: str, party: str) -> list[dict]:
    """One party's notices for one deal, in file order."""
    return [r for r in for_deal(deal_id) if r.get("party") == party]


def transition_listener(deal, old_state, new_state) -> list[dict]:
    """DealManager ``on_transition`` callback: every real state
    change emits one ``state_change`` notice per party naming the
    states the deal moved between."""
    message = (f"Deal {deal.id} ({deal.terms.item_description}): "
               f"{_state_value(old_state)} → {_state_value(new_state)}")
    return emit(deal, "state_change", message)
