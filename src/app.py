"""Dashboard + party views.

Two audiences, two surfaces:

- The OPERATOR dashboard at ``/`` (plus ``/api/decisions``): the
  decision feed over the audit log, exactly as before — stateless,
  no deal state, no party actions.
- The PARTY pages at ``/deal/<deal_id>?party=buyer|seller``: one
  party's view of one deal — terms, counterparty, a status timeline
  built from the deal's own records, what happens next, that party's
  notification-outbox notices, and action forms for ONLY the actions
  :func:`allowed_actions` permits that party in the deal's current
  state. The same function enforces the POST endpoint server-side,
  so a rendered form is never the only guard.

Run: python -m src.app  (after `pip install -r requirements.txt`)
"""
import html

from flask import (Flask, current_app, jsonify, redirect,
                   render_template_string, request)

from . import audit, config, disputes, notifications, webhooks
from .deal_manager import DealManager
from .models import DealState, DealTerms

app = Flask(__name__)

# The app's manager: sim mode (no PayPal calls from the web layer).
# Routes resolve it per-request via app.config["DEAL_MANAGER"] so
# tests can swap in a fresh manager; the webhook blueprint below is
# bound to this instance at import.
MANAGER = DealManager(use_paypal=False)
app.config["DEAL_MANAGER"] = MANAGER


def _seed_demo_deals(manager: DealManager) -> None:
    """Seed the three demo deals (guarded: a manager that already
    holds deals is left alone, so re-import never duplicates).

    (a) a seller-proposed $500 camera awaiting buyer acceptance;
    (b) a funded $300 keyboard in MONITORING;
    (c) a funded $1,200 laptop with an open dispute and one piece
        of seller tracking-proof evidence.

    Seeding runs BEFORE the notification listener is registered
    (see bottom of module), so it writes no notices.
    """
    if manager.deals:
        return
    base = dict(currency="USD", buyer_email="buyer@example.com",
                seller_email="seller@example.com",
                delivery_zip="47172", window_days=14)
    manager.propose(
        DealTerms(amount_cents=50000,
                  item_description="Used mirrorless camera", **base),
        proposer="seller")
    keyboard = manager.propose(
        DealTerms(amount_cents=30000,
                  item_description="Mechanical keyboard", **base))
    manager.fund(keyboard)
    manager._set_state(keyboard, DealState.MONITORING)
    laptop = manager.propose(
        DealTerms(amount_cents=120000,
                  item_description="Laptop", **base))
    manager.fund(laptop)
    manager.open_dispute(laptop)
    disputes.submit_evidence(
        laptop, "seller", "tracking_proof",
        text="Carrier tracking for the laptop shipment",
        data={"tracking_number": "1ZDEMO789"})


_seed_demo_deals(MANAGER)

PAGE = """
<!doctype html><title>Escrow Agent</title>
<meta http-equiv="refresh" content="10">
<h1>Escrow Agent — live</h1>
<h2>Decisions</h2>
<table border=1 cellpadding=6>
<tr><th>deal</th><th>verdict</th><th>confidence</th><th>tier</th>
<th>cost</th><th>shadow</th><th>agreement</th><th>reasoning</th></tr>
{% for d in decisions %}
<tr><td>{{d.deal_id}}</td><td><b>{{d.verdict}}</b></td><td>{{d.confidence}}</td>
<td>{{d.tier}}</td><td>${{d.cost_usd}}</td><td>{{d.shadow_verdict or '—'}}</td>
<td>{{d.agreement if d.agreement is not none else '—'}}</td><td>{{d.reasoning}}</td></tr>
{% endfor %}
</table>
<p><b>Total AI spend:</b> ${{spend }} · <b>decisions:</b> {{ decisions|length }}</p>
"""


@app.get("/")
def index():
    decisions = audit.read_all()
    return render_template_string(PAGE, decisions=decisions,
                                  spend=audit.total_spend())


@app.get("/api/decisions")
def api_decisions():
    return jsonify(audit.read_all())


# --- party views + notifications (Track L3) ---

TERMINAL_STATES = frozenset({
    DealState.RELEASED, DealState.REFUNDED, DealState.SPLIT_SETTLED,
    DealState.DECLINED, DealState.ESCALATED,
})

_PARTIES = ("buyer", "seller")

_ACTION_ORDER = ("accept", "decline", "confirm", "dispute",
                 "resolve_dispute", "evidence")

_ACTION_LABELS = {
    "accept": "Accept terms",
    "decline": "Decline terms",
    "confirm": "Confirm receipt",
    "dispute": "Open dispute",
    "resolve_dispute": "Resolve dispute",
    "evidence": "Submit evidence",
}


def _manager() -> DealManager:
    return current_app.config["DEAL_MANAGER"]


def allowed_actions(deal, party: str) -> set[str]:
    """THE permission function for the party surface: the actions
    `party` may take on `deal` right now. Used both to render the
    party page's forms and to enforce POST /deal/<id>/action, so
    the two can never drift.

    - accept / decline: terms still PROPOSED and this party has not
      already accepted (the proposer accepts by proposing).
    - confirm: the buyer only, while FUNDED or MONITORING, and
      only until receipt is confirmed.
    - dispute: while FUNDED or MONITORING and no dispute is open.
    - resolve_dispute / evidence: while a dispute is open.
    Terminal states (RELEASED / REFUNDED / SPLIT_SETTLED /
    DECLINED / ESCALATED) allow nothing, for either party.
    """
    if party not in _PARTIES:
        return set()
    if deal.state in TERMINAL_STATES:
        return set()
    actions: set[str] = set()
    if (deal.state == DealState.PROPOSED
            and party not in deal.terms_acceptances):
        actions.update(("accept", "decline"))
    if (party == "buyer"
            and deal.state in (DealState.FUNDED, DealState.MONITORING)
            and deal.buyer_confirmed_at is None):
        actions.add("confirm")
    if (deal.state in (DealState.FUNDED, DealState.MONITORING)
            and not deal.dispute_filed):
        actions.add("dispute")
    if deal.dispute_filed:
        actions.update(("resolve_dispute", "evidence"))
    return actions


def _timeline(deal) -> list[str]:
    """Status timeline entries built from what the deal itself
    records: proposal + acceptance timestamps, funding, buyer
    approval / receipt confirmation, the dispute evidence timeline,
    and the current state."""
    entries = [f"Deal created at {deal.created_at}."]
    if deal.proposed_by:
        entries.append(f"Terms proposed by {deal.proposed_by}.")
    else:
        entries.append("Terms proposed (pre-agreed).")
    for party in _PARTIES:
        ts = deal.terms_acceptances.get(party)
        if ts:
            entries.append(f"{party} accepted terms at {ts}.")
    if deal.funded_at:
        entries.append(
            f"Funded at {deal.funded_at} — money held in escrow.")
    if deal.buyer_approved_at:
        entries.append(
            f"Buyer approved the payment at {deal.buyer_approved_at}.")
    if deal.buyer_confirmed_at:
        entries.append(
            f"Buyer confirmed receipt at {deal.buyer_confirmed_at}.")
    if deal.dispute_filed:
        entries.append("A dispute is open on this deal.")
    for record in disputes.timeline(deal):
        entry = (f"{record['submitted_at']}: {record['party']} "
                 f"submitted {record['kind']} evidence")
        if record.get("text"):
            entry += f" — {record['text']}"
        if record.get("file_ref"):
            entry += f" (file: {record['file_ref']})"
        data = record.get("data") or {}
        if data.get("tracking_number"):
            entry += f" — tracking number {data['tracking_number']}"
        if data.get("split_release_cents") is not None:
            entry += (f" — split release "
                      f"{data['split_release_cents']} cents")
        entries.append(entry + ".")
    entries.append(f"Current state: {deal.state.value}.")
    return entries


def _next_step(deal, party: str) -> str:
    """The 'what happens next' line for this state + party. Worded
    without naming the other party's actions, so a page never
    advertises steps its viewer cannot take."""
    if deal.state in TERMINAL_STATES:
        return "This deal is closed; no further action is needed."
    if deal.state == DealState.PROPOSED:
        if party not in deal.terms_acceptances:
            return ("Your response to the proposed terms is needed "
                    "before this deal can be funded.")
        return "Waiting for the other party to respond to the terms."
    if deal.state == DealState.AWAITING_APPROVAL:
        return "Funding is in progress — payment approval is pending."
    if deal.state in (DealState.FUNDED, DealState.MONITORING):
        if deal.dispute_filed:
            return ("A dispute is open on this deal; it stays open "
                    "until it is resolved.")
        if party == "buyer" and deal.buyer_confirmed_at is not None:
            return ("Receipt is on record; the money stays held "
                    "while delivery is monitored.")
        return ("The money is held in escrow while delivery is "
                "monitored.")
    if deal.state == DealState.DECIDED:
        return "A decision has been recorded; settlement is next."
    return "The escrow agent is monitoring this deal."


def _action_form(deal, party: str, action: str) -> str:
    extra = ""
    if action == "evidence":
        options = "".join(
            f'<option value="{kind}">{kind}</option>'
            for kind in ("note", "tracking_proof", "damage_report",
                         "condition_proof", "settlement_offer"))
        extra = (
            f'<label>Kind <select name="kind">{options}</select>'
            '</label> '
            '<label>Text <input type="text" name="text"></label> '
            '<label>File reference '
            '<input type="text" name="file_ref"></label> '
            '<label>Tracking number '
            '<input type="text" name="tracking_number"></label> '
            '<label>Split release (cents) '
            '<input type="text" name="split_release_cents"></label> ')
    return (
        f'<form method="post" action="/deal/{deal.id}/action">'
        f'<input type="hidden" name="party" value="{party}">'
        f'<input type="hidden" name="action" value="{action}">'
        f'{extra}'
        f'<button type="submit">{_ACTION_LABELS[action]}</button>'
        '</form>')


def _party_page(deal, party: str) -> str:
    esc = html.escape
    counterparty = "seller" if party == "buyer" else "buyer"
    counterparty_email = (deal.terms.seller_email if party == "buyer"
                          else deal.terms.buyer_email)
    own_email = (deal.terms.buyer_email if party == "buyer"
                 else deal.terms.seller_email)
    amount = (f"${deal.terms.amount_cents / 100:,.2f} "
              f"{esc(deal.terms.currency)}")
    timeline = "".join(f"<li>{esc(entry)}</li>"
                       for entry in _timeline(deal))
    notices = notifications.for_party(deal.id, party)
    if notices:
        notice_items = "".join(
            f"<li>{esc(n['at'])}: [{esc(n['event'])}] "
            f"{esc(n['message'])}</li>" for n in notices)
    else:
        notice_items = "<li>No notifications yet.</li>"
    actions = allowed_actions(deal, party)
    forms = "".join(_action_form(deal, party, action)
                    for action in _ACTION_ORDER if action in actions)
    if not forms:
        forms = "<p>No actions are available to you right now.</p>"
    return f"""<!doctype html><title>Deal {esc(deal.id)} — {esc(party)} view</title>
<h1>{esc(deal.terms.item_description)}</h1>
<p><b>Amount:</b> {amount}</p>
<p><b>State:</b> {esc(deal.state.value)}</p>
<p><b>You are the {esc(party)}</b> ({esc(own_email)}).
<b>Counterparty ({esc(counterparty)}):</b> {esc(counterparty_email)}</p>
<h2>Status timeline</h2>
<ul>{timeline}</ul>
<h2>What happens next</h2>
<p>{esc(_next_step(deal, party))}</p>
<h2>Your notifications</h2>
<ul>{notice_items}</ul>
<h2>Your actions</h2>
{forms}
"""


@app.get("/deal/<deal_id>")
def deal_page(deal_id: str):
    deal = _manager().deals.get(deal_id)
    if deal is None:
        return "unknown deal", 404
    party = request.args.get("party")
    if party not in _PARTIES:
        return "party must be 'buyer' or 'seller'", 400
    return _party_page(deal, party)


@app.post("/deal/<deal_id>/action")
def deal_action(deal_id: str):
    manager = _manager()
    deal = manager.deals.get(deal_id)
    if deal is None:
        return "unknown deal", 404
    party = request.form.get("party", "")
    if party not in _PARTIES:
        return "party must be 'buyer' or 'seller'", 400
    action = request.form.get("action", "")
    if action not in allowed_actions(deal, party):
        return (f"action {action!r} is not available to {party} "
                f"on this deal right now"), 403
    try:
        if action == "accept":
            manager.accept_terms(deal, party)
        elif action == "decline":
            manager.decline_terms(deal, party)
        elif action == "confirm":
            manager.confirm_receipt(deal, party)
            # Confirmation changes no state, so the transition
            # listener stays silent — notify explicitly.
            notifications.emit(
                deal, "receipt_confirmed",
                f"Buyer confirmed receipt for deal {deal.id} "
                f"({deal.terms.item_description}).")
        elif action == "dispute":
            manager.open_dispute(deal)
        elif action == "resolve_dispute":
            manager.resolve_dispute(deal)
        elif action == "evidence":
            data: dict = {}
            tracking_number = request.form.get("tracking_number")
            if tracking_number:
                data["tracking_number"] = tracking_number
            split_raw = request.form.get("split_release_cents")
            if split_raw:
                try:
                    data["split_release_cents"] = int(split_raw)
                except ValueError:
                    # Leave the raw value for the domain layer to
                    # reject with its own message (-> 400 below).
                    data["split_release_cents"] = split_raw
            kind = request.form.get("kind") or "note"
            disputes.submit_evidence(
                deal, party, kind,
                text=request.form.get("text", ""),
                file_ref=request.form.get("file_ref") or None,
                data=data)
            # Evidence changes no state either — notify explicitly.
            notifications.emit(
                deal, "evidence_submitted",
                f"{party} submitted {kind} evidence on deal "
                f"{deal.id} ({deal.terms.item_description}).")
    except ValueError as exc:
        return str(exc), 400
    return redirect(f"/deal/{deal.id}?party={party}")


@app.get("/api/deal/<deal_id>/notices")
def api_deal_notices(deal_id: str):
    if deal_id not in _manager().deals:
        return jsonify({"error": "unknown deal"}), 404
    party = request.args.get("party")
    if party is not None and party not in _PARTIES:
        return jsonify(
            {"error": "party must be 'buyer' or 'seller'"}), 400
    if party is None:
        return jsonify(notifications.for_deal(deal_id))
    return jsonify(notifications.for_party(deal_id, party))


# Seeding is done above; only NOW register the notification listener
# (so the seeded deals wrote no notices) and the PayPal webhook
# blueprint against the app's manager.
MANAGER.on_transition(notifications.transition_listener)
webhooks.register_webhooks(app, MANAGER)


if __name__ == "__main__":
    app.run(port=config.DASHBOARD_PORT, debug=True)
