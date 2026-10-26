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
- The AGREEMENT surfaces (2026-10-26): ``/propose`` creates a deal
  with an agreed requirements list; every party page shows that
  agreement — requirements, the agreed completion date, live
  status per requirement — from the moment the deal exists; and
  ``/deal/<deal_id>/detail`` renders the per-item checklist view:
  every check with its evidence, Tier 0's result, and Laya's
  per-item shadow read with its agreement marker. The shadow reads
  are display-only: Laya can flag, it can never move money.

Run: python -m src.app  (after `pip install -r requirements.txt`)
"""
import html
import json
from pathlib import Path

from flask import (Flask, current_app, jsonify, redirect,
                   render_template_string, request)

from . import (agreement, audit, config, disputes, milestones as
               milestones_mod, notifications, webhooks)
from .code_delivery import gather_repo, grade_requirements
from .deal_manager import DealManager
from .models import DealState, DealTerms, MilestoneState
from .policy import DEFAULT_POLICY
from .pr_delivery import gather_pr, grade_pr_requirements

app = Flask(__name__)

# The app's manager: sim mode (no PayPal calls from the web layer).
# Routes resolve it per-request via app.config["DEAL_MANAGER"] so
# tests can swap in a fresh manager; the webhook blueprint below is
# bound to this instance at import.
MANAGER = DealManager(use_paypal=False)
app.config["DEAL_MANAGER"] = MANAGER


# ------------------------------------------------------------------
# Atlas Glass — the one design system for every page (2026-10-26).
# Dark, glanceable, calm: deep charcoal background, frosted-glass
# cards, one accent, verdict/status pills readable at a glance.
# Every page renders through _shell() so the surfaces can never
# drift apart visually.

ATLAS_CSS = """
:root{--bg:#0b0e13;--card:rgba(255,255,255,.045);
--border:rgba(255,255,255,.09);--text:#e8eef5;--muted:#93a1b3;
--accent:#6cb8ff;--green:#3fb96f;--red:#ff6b6b;--amber:#ffb340;
--violet:#b18cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
font-size:16px;line-height:1.5}
header.top{display:flex;gap:1.25rem;align-items:baseline;flex-wrap:wrap;
padding:1rem 1.5rem;border-bottom:1px solid var(--border);
background:rgba(255,255,255,.02)}
header.top .brand{font-size:1.25rem;font-weight:700;letter-spacing:.02em}
header.top a{color:var(--muted);text-decoration:none}
header.top a:hover{color:var(--accent)}
main{max-width:62rem;margin:0 auto;padding:1.5rem}
.card{background:var(--card);border:1px solid var(--border);
border-radius:14px;padding:1rem 1.25rem;margin:0 0 1rem;
box-shadow:0 8px 24px rgba(0,0,0,.35)}
h1{font-size:1.6rem;margin:.2rem 0 1rem}
h2{font-size:1.15rem;margin:.2rem 0 .6rem}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid var(--border);padding:.45rem .6rem;
text-align:left;vertical-align:top}
th{color:var(--muted);font-size:.8rem;text-transform:uppercase;
letter-spacing:.05em}
.pill{display:inline-block;padding:.05rem .55rem;border-radius:999px;
font-size:.8rem;font-weight:600;border:1px solid;white-space:nowrap}
.verdict-release,.st-met,.st-pass,.st-released,.st-verified{color:var(--green);border-color:var(--green)}
.verdict-refund,.st-failed,.st-fail,.st-refunded{color:var(--red);border-color:var(--red)}
.verdict-investigate,.st-disputed,.st-unknown{color:var(--amber);border-color:var(--amber)}
.verdict-split{color:var(--violet);border-color:var(--violet)}
.st-pending,.st-manual{color:var(--muted);border-color:var(--muted)}
.st-agree{color:var(--green);border-color:var(--green)}
.st-disagree{color:var(--red);border-color:var(--red)}
.st-unverifiable{color:var(--amber);border-color:var(--amber)}
.st-funded-held,.st-evidence-in{color:var(--accent);border-color:var(--accent)}
.pass{color:var(--green)}.fail{color:var(--red)}.unknown{color:var(--amber)}
.band{font-size:1.2rem;font-weight:600}
a{color:var(--accent)}
input,select,textarea{background:#12161d;color:var(--text);
border:1px solid var(--border);border-radius:8px;padding:.35rem .5rem}
button{background:transparent;color:var(--accent);
border:1px solid var(--accent);border-radius:8px;
padding:.4rem .9rem;cursor:pointer;font-size:.95rem}
button:hover{background:rgba(108,184,255,.12)}
.muted{color:var(--muted)}
.note{color:var(--muted);font-size:.9rem}
.tagline{color:var(--muted);font-size:1.05rem;margin:-.5rem 0 1rem}
code{background:#12161d;padding:.1rem .35rem;border-radius:6px}
"""


def _shell(title: str, body: str, refresh: int | None = None) -> str:
    """The shared page shell: head + Atlas Glass CSS + top nav."""
    refresh_meta = (f'<meta http-equiv="refresh" content="{refresh}">'
                    if refresh else "")
    return f"""<!doctype html><title>{html.escape(title)}</title>
{refresh_meta}<style>{ATLAS_CSS}</style>
<header class="top"><span class="brand">Escrow Agent</span>
<a href="/">Dashboard</a><a href="/propose">Start an agreement</a>
<a href="/grade">Grade a deliverable</a></header>
<main>{body}</main>
"""


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
    def reqs():
        return [agreement.standard_requirement(check)
                for check in agreement.DEFAULT_CHECKS]
    base = dict(currency="USD", buyer_email="buyer@example.com",
                seller_email="seller@example.com",
                delivery_zip="47172", window_days=14)
    # Stable demo tokens (documented in DEMO_SCRIPT.md): the
    # seeded deals' share links must not churn between runs.
    demo_tokens = {"buyer": "demo-link-buyer",
                   "seller": "demo-link-seller"}
    camera = manager.propose(
        DealTerms(amount_cents=50000,
                  item_description="Used mirrorless camera",
                  requirements=reqs(), **base),
        proposer="seller")
    camera.share_tokens = dict(demo_tokens)
    keyboard = manager.propose(
        DealTerms(amount_cents=30000,
                  item_description="Mechanical keyboard",
                  requirements=reqs(), **base))
    keyboard.share_tokens = dict(demo_tokens)
    manager.fund(keyboard)
    manager._set_state(keyboard, DealState.MONITORING)
    from .models import Milestone
    laptop = manager.propose(
        DealTerms(amount_cents=120000,
                  item_description="Laptop",
                  requirements=reqs(), **base),
        milestones=[
            Milestone(id="m1", title="Delivery",
                      requirement="Laptop delivered to the buyer's "
                                  "ZIP",
                      check="delivered", amount_cents=90000,
                      due_date="2026-11-05"),
            Milestone(id="m2", title="Condition verified",
                      requirement="Condition as described — no "
                                  "damage reported",
                      check="condition_clear", amount_cents=30000,
                      due_date="2026-11-09"),
        ])
    laptop.share_tokens = dict(demo_tokens)
    manager.fund(laptop)
    manager.open_dispute(laptop)
    # The open dispute is on the condition milestone.
    laptop.milestones[1].state = MilestoneState.DISPUTED
    disputes.submit_evidence(
        laptop, "seller", "tracking_proof",
        text="Carrier tracking for the laptop shipment",
        data={"tracking_number": "1ZDEMO789"})


_seed_demo_deals(MANAGER)

PAGE = """
<h1>Escrow Agent — live</h1>
<p class="tagline">Money is held, and released only when the agreed
work is verified — every decision shows its work.</p>
<section class="card">
<h2>Deals &amp; agreements</h2>
<table>
<tr><th>deal</th><th>item</th><th>amount</th><th>state</th>
<th>agreed completion</th><th>agreement</th><th>views</th></tr>
{% for d in deals %}
<tr><td>{{d.id}}</td><td>{{d.item}}</td><td>{{d.amount}}</td>
<td><span class="pill st-{{d.state}}">{{d.state}}</span></td>
<td>{{d.completion}}</td>
<td>{{d.requirements}}</td>
<td><a href="{{d.buyer_link}}">buyer</a> ·
<a href="{{d.seller_link}}">seller</a> ·
<a href="/deal/{{d.id}}/detail">checklist &amp; shadow</a></td></tr>
{% endfor %}
</table>
</section>
<section class="card">
<h2>Decisions</h2>
<table>
<tr><th>deal</th><th>verdict</th><th>confidence</th><th>tier</th>
<th>cost</th><th>shadow</th><th>agreement</th><th>reasoning</th></tr>
{% for d in decisions %}
<tr><td><a href="/deal/{{d.deal_id}}/detail">{{d.deal_id}}</a></td>
<td><span class="pill verdict-{{d.verdict}}">{{d.verdict}}</span></td>
<td>{{d.confidence}}</td>
<td>{{d.tier}}</td><td>${{d.cost_usd}}</td><td>{{d.shadow_verdict or '—'}}</td>
<td>{{d.agreement if d.agreement is not none else '—'}}</td><td>{{d.reasoning}}</td></tr>
{% endfor %}
</table>
<p><b>Total AI spend:</b> ${{spend }} · <b>decisions:</b> {{ decisions|length }}</p>
</section>
"""


@app.get("/")
def index():
    decisions = audit.read_all()
    manager = _manager()
    deals = [{
        "id": deal.id,
        "item": deal.terms.item_description,
        "amount": (f"${deal.terms.amount_cents / 100:,.2f} "
                   f"{deal.terms.currency}"),
        "state": deal.state.value.replace("_", "-"),
        "completion": agreement.completion_date(deal),
        "requirements": (f"{len(deal.terms.requirements)} requirements"
                         if deal.terms.requirements
                         else "conditions only"),
        "buyer_link": manager.share_link(deal, "buyer"),
        "seller_link": manager.share_link(deal, "seller"),
    } for deal in manager.deals.values()]
    return _shell("Escrow Agent",
                  render_template_string(PAGE, decisions=decisions,
                                         deals=deals,
                                         spend=audit.total_spend()),
                  refresh=10)


@app.get("/api/decisions")
def api_decisions():
    return jsonify(audit.read_all())


# --- party views + notifications (Track L3) ---

TERMINAL_STATES = frozenset({
    DealState.RELEASED, DealState.REFUNDED, DealState.SPLIT_SETTLED,
    DealState.DECLINED, DealState.ESCALATED, DealState.WITHDRAWN,
})

_PARTIES = ("buyer", "seller")

_ACTION_ORDER = ("accept", "decline", "withdraw", "fund", "confirm",
                 "dispute", "resolve_dispute", "evidence")

_ACTION_LABELS = {
    "accept": "Accept terms",
    "decline": "Decline terms",
    "withdraw": "Withdraw proposal",
    "fund": "Approve payment & fund the hold (demo)",
    "confirm": "Confirm receipt",
    "dispute": "Open dispute",
    "resolve_dispute": "Resolve dispute",
    "evidence": "Submit evidence",
}

_MILESTONE_ACTION_LABELS = {
    "confirm_milestone": "Confirm milestone complete",
    "dispute_milestone": "Dispute this milestone",
    "evidence": "Submit evidence",
    "resolve_milestone_release": "Resolve: release this share",
    "resolve_milestone_refund": "Resolve: refund this share",
    "resolve_milestone_split": "Resolve: split this share",
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
    - withdraw: the proposer, while PROPOSED and the counterparty
      has not accepted yet (after that the terms are locked).
    - fund: the buyer, while PROPOSED with BOTH acceptances on
      record — the buyer's approval is what funds the hold.
    - confirm: the buyer only, while FUNDED or MONITORING, and
      only until receipt is confirmed.
    - dispute: while FUNDED or MONITORING and no dispute is open.
    - resolve_dispute / evidence: while a dispute is open
      (deal-level, or on any milestone for evidence).
    Terminal states (RELEASED / REFUNDED / SPLIT_SETTLED /
    DECLINED / ESCALATED / WITHDRAWN) allow nothing, for either party.
    """
    if party not in _PARTIES:
        return set()
    if deal.state in TERMINAL_STATES:
        return set()
    actions: set[str] = set()
    if (deal.state == DealState.PROPOSED
            and party not in deal.terms_acceptances):
        actions.update(("accept", "decline"))
    if deal.state == DealState.PROPOSED:
        counterparty = "seller" if party == "buyer" else "buyer"
        if (deal.proposed_by == party
                and counterparty not in deal.terms_acceptances):
            actions.add("withdraw")
        if (party == "buyer"
                and all(p in deal.terms_acceptances
                        for p in _PARTIES)):
            actions.add("fund")
    if (party == "buyer"
            and deal.state in (DealState.FUNDED, DealState.MONITORING)
            and deal.buyer_confirmed_at is None):
        actions.add("confirm")
    if (deal.state in (DealState.FUNDED, DealState.MONITORING)
            and not deal.dispute_filed):
        actions.add("dispute")
    if deal.dispute_filed:
        actions.update(("resolve_dispute", "evidence"))
    elif any(m.state.value == "disputed" for m in deal.milestones):
        # Milestone-scoped evidence: a disputed milestone accepts
        # evidence through the deal-level form (tagged on submit).
        actions.add("evidence")
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
        if all(p in deal.terms_acceptances for p in _PARTIES):
            if party == "buyer":
                return ("Both parties have accepted. Your payment "
                        "approval funds the hold — use the fund "
                        "action below.")
            return ("Both parties have accepted. The hold is "
                    "funded by the buyer's payment approval.")
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


def _token_ok(deal, party: str, token) -> bool:
    """Share-link gate: when a deal issued per-party tokens, the
    token matching THIS party is required — a missing or wrong
    token (including the other party's) opens nothing."""
    if not deal.share_tokens:
        return True
    return token is not None and token == deal.share_tokens.get(party)


def _token_field(deal, party: str) -> str:
    token = deal.share_tokens.get(party)
    if not token:
        return ""
    return (f'<input type="hidden" name="k" '
            f'value="{html.escape(token)}">')


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
    if action == "fund":
        extra = ('<span class="note">Demo web layer: funding here '
                 'is simulated. On the real sandbox path the buyer '
                 'approves on PayPal — no API exists for that step; '
                 'the money path itself is proven live.</span> ')
    return (
        f'<form method="post" action="/deal/{deal.id}/action">'
        f'<input type="hidden" name="party" value="{party}">'
        f'<input type="hidden" name="action" value="{action}">'
        f'{_token_field(deal, party)}'
        f'{extra}'
        f'<button type="submit">{_ACTION_LABELS[action]}</button>'
        '</form>')


def _milestone_form(deal, party: str, milestone, action: str) -> str:
    extra = ""
    if action == "evidence":
        options = "".join(
            f'<option value="{kind}">{kind}</option>'
            for kind in ("note", "tracking_proof", "damage_report",
                         "condition_proof"))
        extra = (
            f'<label>Kind <select name="kind">{options}</select>'
            '</label> '
            '<label>Text <input type="text" name="text"></label> '
            '<label>File reference '
            '<input type="text" name="file_ref"></label> '
            '<label>Tracking number '
            '<input type="text" name="tracking_number"></label> ')
    if action == "dispute_milestone":
        extra = ('<label>Reason <input type="text" name="text">'
                 '</label> ')
    if action == "resolve_milestone_split":
        extra = ('<label>Release to seller (cents) '
                 '<input type="text" name="split_release_cents">'
                 '</label> ')
    return (
        f'<form method="post" action="/deal/{deal.id}/action">'
        f'<input type="hidden" name="party" value="{party}">'
        f'<input type="hidden" name="action" value="{action}">'
        f'<input type="hidden" name="milestone_id" '
        f'value="{html.escape(milestone.id)}">'
        f'{_token_field(deal, party)}'
        f'{extra}'
        f'<button type="submit">'
        f'{_MILESTONE_ACTION_LABELS[action]}</button>'
        '</form>')


def _milestone_board(deal, party: str) -> str:
    """The milestone board: every milestone with its share, due
    date, requirement, live status, evidence, and the actions this
    party may take on it right now."""
    if not deal.milestones:
        return ""
    esc = html.escape
    cards = []
    for m in deal.milestones:
        allowed = milestones_mod.allowed_milestone_actions(
            deal, party, m)
        forms = "".join(
            _milestone_form(deal, party, m, action)
            for action in ("confirm_milestone", "dispute_milestone",
                           "evidence", "resolve_milestone_release",
                           "resolve_milestone_refund",
                           "resolve_milestone_split")
            if action in allowed)
        records = milestones_mod.milestone_evidence(deal, m)
        evidence_html = ""
        if records:
            items = "".join(
                f"<li>{esc(r['submitted_at'])}: {esc(r['party'])} "
                f"— {esc(r['kind'])}"
                + (f" — {esc(r['text'])}" if r.get("text") else "")
                + "</li>" for r in records)
            evidence_html = f"<ul>{items}</ul>"
        confirmed = (f" · buyer confirmed "
                     f"{esc(m.buyer_confirmed_at)}"
                     if m.buyer_confirmed_at else "")
        settled = (f" · released {m.released_cents} of "
                   f"{m.amount_cents} cents · settled "
                   f"{esc(m.settled_at)}" if m.settled_at else "")
        cards.append(
            f'<div class="card"><h3>{esc(m.title)} — '
            f'<span class="pill st-{m.state.value}">'
            f'{esc(m.state.value)}</span></h3>'
            f'<p>Share: ${m.amount_cents / 100:,.2f} · '
            f'Due: {esc(m.due_date)}{confirmed}{settled}</p>'
            f'<p>{esc(m.requirement)}</p>'
            f'{evidence_html}{forms}</div>')
    return "<h2>Milestones</h2>" + "".join(cards)


def _share_section(deal, party: str) -> str:
    """Share links + the waiting-for-counterparty state. Your link
    is your identity for this deal; the proposer's page carries the
    counterparty's link to send while acceptance is outstanding."""
    if not deal.share_tokens:
        return ""
    esc = html.escape
    manager = _manager()
    own = manager.share_link(deal, party)
    lines = [
        "<h2>Share links</h2>",
        f"<p>Your link — it is your identity for this deal, keep "
        f"it private:<br><code>{esc(own)}</code></p>",
    ]
    counterparty = "seller" if party == "buyer" else "buyer"
    if (deal.proposed_by == party
            and deal.state == DealState.PROPOSED
            and counterparty not in deal.terms_acceptances):
        link = manager.share_link(deal, counterparty)
        lines.append(
            f"<p><b>Waiting for counterparty.</b> Your agreement "
            f"is ready — send this link to your {counterparty}:"
            f"<br><code>{esc(link)}</code></p>")
    return "\n".join(lines)


def _agreement_section(deal) -> str:
    """The agreement, as both parties accepted it: the requirements
    list, the agreed completion date, each party's acceptance, and
    every requirement's live status against the latest evidence.
    Identical for both parties — the agreement is shared truth."""
    esc = html.escape
    summary = agreement.acceptance_summary(deal)
    def accepted(party: str) -> str:
        ts = summary[party]
        return f"accepted {esc(ts)}" if ts else "not yet accepted"
    lines = [
        f"<p><b>Release conditions:</b> "
        f"{esc(deal.terms.release_conditions)}</p>",
        f"<p><b>Delivery window:</b> {deal.terms.window_days} days — "
        f"<b>agreed completion date: "
        f"{esc(agreement.completion_date(deal))}</b></p>",
        f"<p><b>Proposed by:</b> "
        f"{esc(summary['proposed_by'] or 'pre-agreed')} · "
        f"<b>Buyer:</b> {accepted('buyer')} · "
        f"<b>Seller:</b> {accepted('seller')}</p>",
    ]
    statuses = agreement.agreement_status(deal)
    if statuses:
        rows = "".join(
            f"<li><span class=\"pill st-{esc(s['status'])}\">"
            f"<b>{esc(s['status'])}</b></span> — {esc(s['text'])}"
            f" <i>({esc(s['evidence'])})</i></li>" for s in statuses)
        lines.append(f"<ul>{rows}</ul>")
    else:
        lines.append("<p>No itemized requirements on this deal; "
                     "the release conditions above are the agreement.</p>")
    lines.append(
        f'<p><a href="/deal/{esc(deal.id)}/detail">Per-item checklist '
        f"&amp; shadow sensor view</a></p>")
    return "\n".join(lines)


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
    if "evidence" in actions and not deal.dispute_filed:
        # Evidence here is milestone-scoped (a milestone dispute is
        # open); the milestone board renders those forms, tagged
        # per milestone — a deal-level evidence form would only
        # 400 against the closed deal-level dispute gate.
        actions = actions - {"evidence"}
    forms = "".join(_action_form(deal, party, action)
                    for action in _ACTION_ORDER if action in actions)
    if not forms:
        forms = "<p>No actions are available to you right now.</p>"
    held = ""
    if deal.funded_at:
        held = (f"<p><b>Money held</b> since {esc(deal.funded_at)}"
                + (f" · authorization expires "
                   f"{esc(deal.paypal_auth_expires_at)}"
                   if deal.paypal_auth_expires_at else "")
                + ".</p>")
    outcomes = ""
    if deal.state == DealState.PROPOSED:
        outcomes = """
<section class="card">
<h2>What happens at each outcome</h2>
<ul>
<li><b>Release</b> — the agreed work is verified; the held money
captures to the seller (per milestone, when the deal has
milestones).</li>
<li><b>Refund</b> — the seller fails to perform; the hold voids
back to the buyer.</li>
<li><b>Investigate</b> — the evidence conflicts or is too thin;
the agent refuses to guess, and the case escalates with the full
evidence pack for a human decision.</li>
</ul>
<p class="note">Terms lock at acceptance: once both parties have
accepted, any change means a new proposal.</p>
</section>
"""
    share = _share_section(deal, party)
    share_card = (f'<section class="card">{share}</section>'
                  if share else "")
    board = _milestone_board(deal, party)
    board_card = (f'<section class="card">{board}</section>'
                  if board else "")
    body = f"""
<h1>{esc(deal.terms.item_description)}</h1>
<section class="card">
<p><b>Amount:</b> {amount}</p>
<p><b>State:</b> <span class="pill st-{esc(deal.state.value)}">{esc(deal.state.value)}</span></p>
<p><b>You are the {esc(party)}</b> ({esc(own_email)}).
<b>Counterparty ({esc(counterparty)}):</b> {esc(counterparty_email)}</p>
{held}
</section>
{share_card}
<section class="card">
<h2>The agreement</h2>
{_agreement_section(deal)}
</section>
{board_card}
{outcomes}
<section class="card">
<h2>Status timeline</h2>
<ul>{timeline}</ul>
<h2>What happens next</h2>
<p>{esc(_next_step(deal, party))}</p>
</section>
<section class="card">
<h2>Your notifications</h2>
<ul>{notice_items}</ul>
<h2>Your actions</h2>
{forms}
</section>
"""
    return _shell(f"Deal {deal.id} — {party} view", body)


@app.get("/deal/<deal_id>")
def deal_page(deal_id: str):
    deal = _manager().deals.get(deal_id)
    if deal is None:
        return "unknown deal", 404
    party = request.args.get("party")
    if party not in _PARTIES:
        return "party must be 'buyer' or 'seller'", 400
    if not _token_ok(deal, party, request.args.get("k")):
        return ("this party link is invalid or incomplete — "
                "open the exact share link you were sent"), 403
    return _party_page(deal, party)


_MILESTONE_ACTIONS = ("confirm_milestone", "dispute_milestone",
                      "resolve_milestone_release",
                      "resolve_milestone_refund",
                      "resolve_milestone_split")


def _back_to_deal(deal, party: str):
    return redirect(_manager().share_link(deal, party))


def _run_milestone_action(manager, deal, party, milestone, action):
    """Execute one milestone-scoped action (already authorized by
    allowed_milestone_actions). Mirrors the deal-level handler:
    domain ValueErrors surface as 400, no-state-change events emit
    their notices explicitly."""
    if action == "confirm_milestone":
        manager.confirm_milestone(deal, milestone.id, party)
        notifications.emit(
            deal, "milestone_confirmed",
            f"Buyer confirmed milestone '{milestone.title}' on "
            f"deal {deal.id} ({deal.terms.item_description}).")
    elif action == "dispute_milestone":
        manager.dispute_milestone(
            deal, milestone.id, party,
            text=request.form.get("text", ""))
        notifications.emit(
            deal, "milestone_disputed",
            f"{party} opened a dispute on milestone "
            f"'{milestone.title}' of deal {deal.id} "
            f"({deal.terms.item_description}).")
    elif action == "evidence":
        data: dict = {}
        tracking_number = request.form.get("tracking_number")
        if tracking_number:
            data["tracking_number"] = tracking_number
        kind = request.form.get("kind") or "note"
        milestones_mod.submit_milestone_evidence(
            deal, milestone, party, kind,
            text=request.form.get("text", ""),
            file_ref=request.form.get("file_ref") or None,
            data=data)
        notifications.emit(
            deal, "evidence_submitted",
            f"{party} submitted {kind} evidence on milestone "
            f"'{milestone.title}' of deal {deal.id} "
            f"({deal.terms.item_description}).")
    elif action.startswith("resolve_milestone_"):
        outcome = action.rsplit("_", 1)[1]
        split_raw = request.form.get("split_release_cents")
        split = None
        if split_raw:
            try:
                split = int(split_raw)
            except ValueError:
                raise ValueError(
                    f"split release {split_raw!r} is not a whole "
                    f"number of cents")
        manager.resolve_milestone(
            deal, milestone.id, party, outcome,
            split_release_cents=split)
        notifications.emit(
            deal, "milestone_resolved",
            f"Dispute on milestone '{milestone.title}' of deal "
            f"{deal.id} resolved as {outcome} "
            f"({deal.terms.item_description}).")
    return _back_to_deal(deal, party)


@app.post("/deal/<deal_id>/action")
def deal_action(deal_id: str):
    manager = _manager()
    deal = manager.deals.get(deal_id)
    if deal is None:
        return "unknown deal", 404
    party = request.form.get("party", "")
    if party not in _PARTIES:
        return "party must be 'buyer' or 'seller'", 400
    token = request.form.get("k") or request.args.get("k")
    if not _token_ok(deal, party, token):
        return ("this party link is invalid or incomplete — "
                "actions need the exact share link you were "
                "sent"), 403
    action = request.form.get("action", "")
    milestone_id = request.form.get("milestone_id", "")
    milestone = None
    if milestone_id:
        try:
            milestone = milestones_mod.get_milestone(deal, milestone_id)
        except ValueError as exc:
            return str(exc), 400
    # Milestone-scoped actions are gated by the milestone permission
    # function (the same one that rendered their forms).
    if action in _MILESTONE_ACTIONS or \
            (action == "evidence" and milestone is not None
             and not deal.dispute_filed):
        if milestone is None:
            return "milestone action needs a milestone", 400
        if action not in milestones_mod.allowed_milestone_actions(
                deal, party, milestone):
            return (f"action {action!r} is not available to "
                    f"{party} on milestone {milestone.title!r} "
                    f"right now"), 403
        try:
            return _run_milestone_action(
                manager, deal, party, milestone, action)
        except ValueError as exc:
            return str(exc), 400
    if action not in allowed_actions(deal, party):
        return (f"action {action!r} is not available to {party} "
                f"on this deal right now"), 403
    try:
        if action == "accept":
            manager.accept_terms(deal, party)
        elif action == "decline":
            manager.decline_terms(deal, party)
        elif action == "withdraw":
            manager.withdraw(deal, party)
        elif action == "fund":
            manager.fund(deal)
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
    return _back_to_deal(deal, party)


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


# ------------------------------------------------------------------
# /propose + per-item detail — the agreement surfaces (2026-10-26).
#
# /propose is where a deal's agreement is MADE: a party states the
# terms and the requirements list both sides will be held to. The
# detail view is where the agreement is READ BACK against evidence:
# every checklist item, Tier 0's result, and the shadow sensor's
# per-item read. Neither surface adjudicates or moves money.

PROPOSE_PAGE = """
<h1>Propose a deal</h1>
<p>The requirements list below becomes the agreement: both parties
accept it, and the escrow agent verifies the deal against it.
On submit you get a share link for your counterparty — the deal
waits, unaccepted and unfunded, until they open it and agree.</p>
{% if error %}<p><strong>Error:</strong> {{ error }}</p>{% endif %}
<form method="post" id="propose-form">
<section class="card">
<p><label>You are the
<select name="proposer">
<option value="seller" {{ 'selected' if form.proposer=='seller' }}>seller</option>
<option value="buyer" {{ 'selected' if form.proposer=='buyer' }}>buyer</option>
</select></label></p>
<p><label>Item or task description <input name="item_description" size="48"
value="{{ form.item_description }}"></label></p>
<p><label>Total amount (e.g. 500.00) <input name="amount" id="amount" size="12"
value="{{ form.amount }}"></label>
<label>Currency <input name="currency" size="4"
value="{{ form.currency }}"></label></p>
<p><label>Buyer email <input name="buyer_email" size="28"
value="{{ form.buyer_email }}"></label>
<label>Seller email <input name="seller_email" size="28"
value="{{ form.seller_email }}"></label></p>
<p><label>Delivery ZIP <input name="delivery_zip" size="10"
value="{{ form.delivery_zip }}"></label>
<label>Delivery window (days) <input name="window_days" size="4"
value="{{ form.window_days }}"></label></p>
<p><label>Release conditions <input name="release_conditions" size="60"
value="{{ form.release_conditions }}"></label></p>
</section>
<section class="card">
<p><b>Structure:</b>
<label><input type="radio" name="structure" value="lump"
{{ 'checked' if form.structure!='milestones' }}
onclick="setStructure('lump')"> Single payment</label>
<label><input type="radio" name="structure" value="milestones"
{{ 'checked' if form.structure=='milestones' }}
onclick="setStructure('milestones')"> Milestones</label></p>
<div id="lump-fields">
<fieldset><legend>Agreed requirements — verified by the checklist</legend>
{% for check, text in standards %}
<p><label><input type="checkbox" name="req_check" value="{{ check }}"
{{ 'checked' if check in form.req_checks }}> {{ text }}</label></p>
{% endfor %}
</fieldset>
<p>Additional requirements (one per line; judged by the parties,
not machine-checked):<br>
<textarea name="extra_requirements" rows="3" cols="70">{{ form.extra_requirements }}</textarea></p>
</div>
<div id="milestone-fields" style="display:none">
<p>Each milestone settles independently when its requirement is
verified. Shares must sum to the total amount.</p>
<div id="ms-rows"></div>
<p><button type="button" onclick="addMilestone()">Add milestone</button>
<span id="ms-sum" class="note"></span></p>
</div>
</section>
<p><button type="submit" id="submit-btn">Propose deal</button></p>
</form>
<script>
const CHECKS = {{ checks_json }};
function msRow() {
  const div = document.createElement('div');
  div.className = 'ms-row';
  const opts = CHECKS.map(c =>
    `<option value="${c[0]}">${c[1]}</option>`).join('');
  div.innerHTML =
    `<p><label>Title <input name="ms_title" size="24"></label>
     <label>Share (amount) <input name="ms_amount" size="9"
       class="ms-amount" oninput="checkSum()"></label>
     <label>Due <input name="ms_due" type="date"></label>
     <label>Verified by <select name="ms_check">
       <option value="">manual — buyer confirmation</option>
       ${opts}</select></label>
     <button type="button" onclick="this.closest('.ms-row').remove();
       checkSum()">Remove</button><br>
     <label>Completion requirement <input name="ms_requirement"
       size="60"></label></p>`;
  return div;
}
function addMilestone() {
  document.getElementById('ms-rows').appendChild(msRow());
  checkSum();
}
function checkSum() {
  const total = parseFloat(
    document.getElementById('amount').value) || 0;
  let sum = 0;
  document.querySelectorAll('.ms-amount').forEach(i => {
    sum += parseFloat(i.value) || 0; });
  const el = document.getElementById('ms-sum');
  const btn = document.getElementById('submit-btn');
  if (document.querySelector(
        'input[name="structure"][value="milestones"]').checked) {
    el.textContent = ` — shares sum to ${sum.toFixed(2)} of ` +
      `${total.toFixed(2)}` +
      (Math.round(sum * 100) === Math.round(total * 100)
        ? ' ✓' : ' — must equal the total');
    btn.disabled =
      Math.round(sum * 100) !== Math.round(total * 100);
  } else {
    el.textContent = '';
    btn.disabled = false;
  }
}
function setStructure(which) {
  document.getElementById('lump-fields').style.display =
    which === 'lump' ? '' : 'none';
  document.getElementById('milestone-fields').style.display =
    which === 'milestones' ? '' : 'none';
  if (which === 'milestones' &&
      !document.querySelector('.ms-row')) addMilestone();
  checkSum();
}
document.getElementById('amount').addEventListener('input', checkSum);
setStructure('{{ form.structure }}' === 'milestones'
  ? 'milestones' : 'lump');
</script>
"""

_PROPOSE_FIELDS = ("proposer", "item_description", "amount", "currency",
                   "buyer_email", "seller_email", "delivery_zip",
                   "window_days", "release_conditions",
                   "extra_requirements")


def _render_propose(form, error, status=200):
    checks_json = json.dumps(
        [[check, text] for check, text
         in agreement.STANDARD_REQUIREMENTS])
    body = render_template_string(
        PROPOSE_PAGE, form=form, error=error,
        standards=agreement.STANDARD_REQUIREMENTS,
        checks_json=checks_json)
    return _shell("Propose a deal — Escrow Agent", body), status


@app.get("/propose")
def propose_page():
    form = {"proposer": "seller", "item_description": "",
            "amount": "", "currency": "USD",
            "buyer_email": "buyer@example.com",
            "seller_email": "seller@example.com",
            "delivery_zip": "", "window_days": "14",
            "release_conditions":
                "delivery confirmed to buyer address",
            "extra_requirements": "", "structure": "lump",
            "req_checks": list(agreement.DEFAULT_CHECKS)}
    return _render_propose(form, None)


def _parse_milestones(form, amount_cents: int):
    """Build (milestones, requirements) from the milestone editor's
    parallel fields. Shares are validated against the deal total by
    DealManager.propose (validate_milestones); parsing and shape
    errors raise ValueError here."""
    from .models import Milestone
    titles = request.form.getlist("ms_title")
    amounts = request.form.getlist("ms_amount")
    dues = request.form.getlist("ms_due")
    checks = request.form.getlist("ms_check")
    reqs = request.form.getlist("ms_requirement")
    known = dict(agreement.STANDARD_REQUIREMENTS)
    milestones, requirements = [], []
    for i, title in enumerate(titles):
        title = title.strip()
        amount_raw = amounts[i].strip() if i < len(amounts) else ""
        if not title and not amount_raw:
            continue  # an untouched editor row
        try:
            share = round(float(amount_raw) * 100)
        except ValueError:
            raise ValueError(
                f"milestone {title or i + 1!r} share "
                f"{amount_raw!r} is not a number")
        due = dues[i].strip() if i < len(dues) else ""
        if not due:
            raise ValueError(
                f"milestone {title!r} needs a due date")
        check = checks[i].strip() if i < len(checks) else ""
        if check and check not in known:
            raise ValueError(
                f"milestone {title!r} verifier {check!r} is not a "
                f"checklist item")
        requirement = (reqs[i].strip() if i < len(reqs) else "") \
            or title
        milestones.append(Milestone(
            id=f"m{len(milestones) + 1}", title=title,
            requirement=requirement, amount_cents=share,
            due_date=due, check=check or None))
        requirements.append(
            {"text": requirement, "check": check or None})
    if not milestones:
        raise ValueError(
            "milestone structure needs at least one milestone")
    return milestones, requirements


@app.post("/propose")
def propose_create():
    form = {k: (request.form.get(k) or "") for k in _PROPOSE_FIELDS}
    form["req_checks"] = request.form.getlist("req_check")
    form["structure"] = request.form.get("structure") or "lump"
    error = None
    try:
        proposer = form["proposer"]
        if proposer not in _PARTIES:
            raise ValueError("proposer must be 'buyer' or 'seller'")
        if not form["item_description"].strip():
            raise ValueError("item description is required")
        try:
            amount_cents = round(float(form["amount"]) * 100)
        except ValueError:
            raise ValueError(
                f"amount {form['amount']!r} is not a number")
        if amount_cents <= 0:
            raise ValueError("amount must be positive")
        try:
            window_days = int(form["window_days"])
        except ValueError:
            raise ValueError(
                f"window {form['window_days']!r} is not a whole "
                f"number of days")
        if window_days <= 0:
            raise ValueError("delivery window must be positive")
        for field in ("buyer_email", "seller_email", "delivery_zip"):
            if not form[field].strip():
                raise ValueError(f"{field.replace('_', ' ')} "
                                 f"is required")
        milestones = None
        if form["structure"] == "milestones":
            milestones, requirements = _parse_milestones(
                form, amount_cents)
        else:
            known = dict(agreement.STANDARD_REQUIREMENTS)
            requirements = [agreement.standard_requirement(check)
                            for check in form["req_checks"]
                            if check in known]
            requirements += [agreement.manual_requirement(
                line.strip())
                for line in form["extra_requirements"].splitlines()
                if line.strip()]
        terms = DealTerms(
            amount_cents=amount_cents,
            currency=form["currency"].strip() or "USD",
            item_description=form["item_description"].strip(),
            buyer_email=form["buyer_email"].strip(),
            seller_email=form["seller_email"].strip(),
            delivery_zip=form["delivery_zip"].strip(),
            window_days=window_days,
            release_conditions=(form["release_conditions"].strip()
                                or "delivery confirmed to buyer address"),
            requirements=requirements)
    except ValueError as exc:
        error = str(exc)
    if error is not None:
        return _render_propose(form, error, status=400)
    manager = _manager()
    try:
        deal = manager.propose(terms, proposer=proposer,
                               milestones=milestones,
                               share_links=True)
    except ValueError as exc:
        return _render_propose(form, str(exc), status=400)
    return redirect(manager.share_link(deal, proposer))


def _detail_page(deal, record) -> str:
    """Per-item checklist view for one deal: every checklist item
    with its evidence, Tier 0's result, and — where the shadow
    sensor read the item — Laya's per-item read and the agreement
    marker. Rows come from the latest audited decision's
    shadow_items when present, extended with any checklist items
    the forest does not read (from the deal's latest monitor pass).
    """
    esc = html.escape
    shadow_by_check = {}
    if record and record.get("shadow_items"):
        shadow_by_check = {item["check"]: item
                           for item in record["shadow_items"]}
    rows = []
    seen = set()
    sources = [(r.name, r.status.value, r.evidence)
               for r in deal.last_check_results]
    for check, status, evidence in sources:
        seen.add(check)
        item = shadow_by_check.get(check)
        rows.append((check, status, evidence,
                     item["laya_read"] if item else "—",
                     item["agreement"] if item else "—"))
    for check, item in shadow_by_check.items():
        if check not in seen:
            rows.append((check, item["tier0_status"],
                         item["evidence"], item["laya_read"],
                         item["agreement"]))
    row_by_check = {r[0]: r for r in rows}

    def items_table(subset) -> str:
        body = "".join(
            f"<tr><td>{esc(check)}</td>"
            f"<td><span class=\"pill st-{esc(status)}\">"
            f"<b>{esc(status)}</b></span></td>"
            f"<td>{esc(evidence)}</td><td>{esc(read)}</td>"
            f"<td><span class=\"pill st-{esc(marker)}\">"
            f"{esc(marker)}</span></td></tr>"
            for check, status, evidence, read, marker in subset)
        return ("<table>"
                "<tr><th>checklist item</th><th>Tier 0 result</th>"
                "<th>evidence</th><th>Laya shadow read</th>"
                "<th>agreement</th></tr>" + body + "</table>")

    if deal.milestones:
        # One coherent surface: each milestone carries its own
        # checklist item; unassigned items group at deal level.
        used = set()
        sections = []
        for m in deal.milestones:
            head = (f"<h3>{esc(m.title)} — "
                    f"<span class=\"pill st-{m.state.value}\">"
                    f"{esc(m.state.value)}</span></h3>"
                    f"<p>Share: ${m.amount_cents / 100:,.2f} · "
                    f"Due: {esc(m.due_date)}</p>"
                    f"<p>{esc(m.requirement)}</p>")
            if m.check is None:
                confirmed = (f"buyer confirmed "
                             f"{esc(m.buyer_confirmed_at)}"
                             if m.buyer_confirmed_at
                             else "awaiting buyer confirmation")
                inner = (f"<p><b>Manual judgment</b> — {confirmed}. "
                         f"No checklist item reads this milestone; "
                         f"the shadow sensor has no item here.</p>")
            elif m.check in row_by_check:
                used.add(m.check)
                inner = items_table([row_by_check[m.check]])
            else:
                inner = ("<p>No monitoring pass has recorded this "
                         "milestone's item yet.</p>")
            sections.append(
                f'<section class="card">{head}{inner}</section>')
        rest = [r for r in rows if r[0] not in used]
        if rest:
            sections.append(
                '<section class="card"><h3>Deal-level checklist '
                "items</h3>" + items_table(rest) + "</section>")
        table = "".join(sections)
    elif rows:
        table = items_table(rows)
    else:
        table = ("<p>No monitoring pass has been recorded for this "
                 "deal yet — there is no checklist evidence or shadow "
                 "read to show.</p>")
    verdict_line = ""
    if record:
        verdict_line = (
            f"<p><b>Latest decision:</b> "
            f"<span class=\"pill verdict-{esc(record['verdict'])}\">"
            f"{esc(record['verdict'])}</span> "
            f"(tier {record['tier']}, confidence "
            f"{record['confidence']}) — {esc(record['reasoning'])}</p>")
    manager = _manager()
    body = f"""
<h1>{esc(deal.terms.item_description)} — per-item view</h1>
<section class="card">
<p><b>State:</b> {esc(deal.state.value)} ·
<a href="{esc(manager.share_link(deal, 'buyer'))}">buyer view</a> ·
<a href="{esc(manager.share_link(deal, 'seller'))}">seller view</a></p>
<h2>The agreement</h2>
{_agreement_section(deal)}
</section>
<section class="card">
<h2>Checklist items &amp; shadow reads</h2>
{verdict_line}
</section>
{table}
<section class="card">
<p>The shadow sensor (Laya) is advisory only: its reads are logged
next to Tier 0's results so a dispute can be judged item by item —
it can flag, it can never move money or change a verdict.</p>
</section>
"""
    return _shell(f"Deal {deal.id} — checklist & shadow", body)


@app.get("/deal/<deal_id>/detail")
def deal_detail(deal_id: str):
    deal = _manager().deals.get(deal_id)
    records = [r for r in audit.read_all()
               if r.get("deal_id") == deal_id]
    if deal is None:
        if not records:
            return "unknown deal", 404
        # Audit-only deal (not held by this manager): render from
        # the latest record's shadow items alone.
        record = records[-1]
        items = record.get("shadow_items") or []
        esc = html.escape
        rows = "".join(
            f"<tr><td>{esc(i['check'])}</td>"
            f"<td><b>{esc(i['tier0_status'])}</b></td>"
            f"<td>{esc(i['evidence'])}</td>"
            f"<td>{esc(i['laya_read'])}</td>"
            f"<td>{esc(i['agreement'])}</td></tr>" for i in items)
        body = f"""
<h1>Deal {esc(deal_id)} — per-item view (audit record)</h1>
<section class="card">
<p><b>Latest decision:</b> {esc(record['verdict'])} — {esc(record['reasoning'])}</p>
<table>
<tr><th>checklist item</th><th>Tier 0 result</th><th>evidence</th>
<th>Laya shadow read</th><th>agreement</th></tr>{rows}</table>
</section>
"""
        return _shell(f"Deal {deal_id} — checklist & shadow", body)
    record = records[-1] if records else None
    return _detail_page(deal, record)


# ------------------------------------------------------------------
# /grade — the requirements-list workbench (2026-10-25).
#
# The generalized pattern on one page: state the agreement as a
# requirements list (JSON), point it at a deliverable — a local repo
# or a GitHub pull request — and read back the audit: every item
# graded with its evidence, the integer completion fraction, and the
# policy band that fraction falls in. ADVISORY ONLY: this page moves
# no money and settles nothing; it is the audit half of the system,
# usable standalone for any work with a requirements list.

GRADE_PAGE = """
<h1>Grade a deliverable</h1>
<section class="card">
<p>The agreement is the audit list: state the requirements, point at
the work, read the grade. Repo kinds: file_exists, module_exists,
symbol_exists, tests_pass, readme_section, manual_judgment.
PR kinds: files_include, files_exclude, check_passes,
checklist_item, manual_judgment.</p>
{% if error %}<p class="fail"><strong>Error:</strong> {{ error }}</p>{% endif %}
<form method="post">
<p><label>Target:
<select name="target_type">
<option value="repo" {{ 'selected' if form.target_type=='repo' }}>Local repo / deliverable folder</option>
<option value="pr" {{ 'selected' if form.target_type=='pr' }}>GitHub pull request</option>
</select></label></p>
<p><label>Repo path <input name="repo_path" size="60" value="{{ form.repo_path }}"></label></p>
<p><label>PR owner <input name="pr_owner" size="16" value="{{ form.pr_owner }}"></label>
<label>repo <input name="pr_repo" size="16" value="{{ form.pr_repo }}"></label>
<label>number <input name="pr_number" size="6" value="{{ form.pr_number }}"></label>
(GitHub token read from the GITHUB_TOKEN environment variable.)</p>
<p>Requirements (JSON list of {id, kind, weight, target, description}):<br>
<textarea name="requirements" rows="10">{{ form.requirements }}</textarea></p>
<p>Manual verdicts (optional JSON object, e.g. {"review": true}):<br>
<textarea name="manual_verdicts" rows="2">{{ form.manual_verdicts }}</textarea></p>
<p><button type="submit">Grade it</button></p>
</form>
{% if grade %}
<h2>Result — {{ subject }}</h2>
<p class="band">{{ grade.fraction_percent }}% complete — policy band: {{ band }}</p>
<p>satisfied {{ grade.satisfied_weight }} · failed {{ grade.failed_weight }}
· unknown {{ grade.unknown_weight }} · total {{ grade.total_weight }}
{% if grade.decisive_unknown %}<br><span class="unknown">Decisive unknown:
{{ grade.ungradeable_reason }}</span>{% endif %}</p>
<table><tr><th>Requirement</th><th>Kind</th><th>Weight</th>
<th>Status</th><th>Evidence</th></tr>
{% for r in grade.per_requirement %}
<tr><td>{{ r.id }}</td><td>{{ r.kind }}</td><td>{{ r.weight }}</td>
<td class="{{ r.status }}">{{ r.status }}</td><td>{{ r.evidence }}</td></tr>
{% endfor %}</table>
{% endif %}
</section>
"""

_GRADE_EXAMPLE = json.dumps([
    {"id": "readme", "kind": "readme_section", "weight": 1,
     "target": "Quickstart"},
    {"id": "license", "kind": "file_exists", "weight": 2,
     "target": "LICENSE"},
    {"id": "review", "kind": "manual_judgment", "weight": 2,
     "description": "A human has reviewed the work"},
], indent=1)


def _policy_band(grade) -> str:
    if grade.decisive_unknown:
        return "INVESTIGATE (an unjudged item decides the outcome)"
    if grade.ungradeable_reason:
        return f"UNGRADEABLE ({grade.ungradeable_reason})"
    bp = grade.fraction_basis_points
    if bp >= round(DEFAULT_POLICY.code_release_threshold * 10000):
        return "RELEASE"
    if bp >= round(DEFAULT_POLICY.code_split_min_fraction * 10000):
        return "SPLIT at the measured fraction"
    return "REFUND"


@app.get("/grade")
def grade_page():
    form = {"target_type": "repo", "repo_path": "", "pr_owner": "",
            "pr_repo": "", "pr_number": "",
            "requirements": _GRADE_EXAMPLE, "manual_verdicts": ""}
    return _shell("Grade a deliverable — escrow audit",
                  render_template_string(GRADE_PAGE, form=form,
                                         grade=None, error=None,
                                         subject="", band=""))


@app.post("/grade")
def grade_run():
    form = {k: (request.form.get(k) or "") for k in
            ("target_type", "repo_path", "pr_owner", "pr_repo",
             "pr_number", "requirements", "manual_verdicts")}
    grade = subject = band = error = None
    try:
        reqs = json.loads(form["requirements"] or "[]")
        verdicts = json.loads(form["manual_verdicts"] or "{}")
        if form["target_type"] == "pr":
            ev = gather_pr(form["pr_owner"], form["pr_repo"],
                           int(form["pr_number"]))
            subject = (f"PR {form['pr_owner']}/{form['pr_repo']}"
                       f"#{form['pr_number']} — {ev.title}")
            grade = grade_pr_requirements(ev, reqs,
                                          manual_verdicts=verdicts)
        else:
            root = Path(form["repo_path"]).expanduser()
            if not root.is_dir():
                raise ValueError(f"not a directory: {root}")
            ev = gather_repo(root, source=f"grade-page:{root}")
            subject = f"repo at {root}"
            grade = grade_requirements(ev, reqs,
                                       manual_verdicts=verdicts)
        band = _policy_band(grade)
        grade = grade.to_dict()
    except Exception as exc:  # bad JSON, bad path, API failure: say so
        error = f"{type(exc).__name__}: {exc}"
    return _shell("Grade a deliverable — escrow audit",
                  render_template_string(GRADE_PAGE, form=form,
                                         grade=grade, error=error,
                                         subject=subject or "",
                                         band=band or ""))



# Seeding is done above; only NOW register the notification listener
# (so the seeded deals wrote no notices) and the PayPal webhook
# blueprint against the app's manager.
MANAGER.on_transition(notifications.transition_listener)
webhooks.register_webhooks(app, MANAGER)


if __name__ == "__main__":
    app.run(port=config.DASHBOARD_PORT, debug=True)
