"""Deal lifecycle manager: the escrow state machine.

PROPOSED -> AWAITING_APPROVAL -> FUNDED -> MONITORING -> DECIDED
         -> RELEASED | REFUNDED | ESCALATED

Funding creates a PayPal AUTHORIZE order. Buyer-funded orders wait in
AWAITING_APPROVAL until the buyer approves and complete_funding()
authorizes (money held); card-funded orders authorize at creation.
Release captures the held authorization to the seller; refund voids it
back to the buyer. Monitoring runs the checklist + decision engine on
gathered evidence.
"""
import uuid

from . import audit, paypal_client
from .checklist import run_checklist
from .decision_engine import adjudicate
from .evidence import gather_evidence
from .models import Deal, DealState, DealTerms, Verdict


class DealManager:
    def __init__(self, use_paypal: bool = False):
        self.use_paypal = use_paypal
        self.deals: dict[str, Deal] = {}

    def propose(self, terms: DealTerms) -> Deal:
        deal = Deal(id=f"deal-{uuid.uuid4().hex[:8]}", terms=terms)
        self.deals[deal.id] = deal
        return deal

    def fund(self, deal: Deal,
             payment_source: dict | None = None) -> Deal:
        """Create the PayPal order for this deal (or simulate funding).

        Buyer-funded (no payment_source): the deal lands in
        AWAITING_APPROVAL with deal.paypal_approve_url set; no money is
        held until complete_funding() runs after buyer approval.
        Card-funded (payment_source given): PayPal authorizes at
        creation, so the deal is FUNDED with the money held.
        """
        if self.use_paypal:
            auth = paypal_client.create_authorization(
                deal.terms.amount_cents, deal.terms.currency,
                f"Escrow: {deal.terms.item_description}",
                payment_source=payment_source,
            )
            deal.paypal_order_id = auth["order_id"]
            deal.paypal_approve_url = auth.get("approve_url")
            # FIX (2026-10-24): the authorization id used to be dropped
            # here, which made every later capture/void unreachable.
            deal.paypal_auth_id = auth.get("authorization_id")
            deal.state = (DealState.FUNDED if deal.paypal_auth_id
                          else DealState.AWAITING_APPROVAL)
        else:
            deal.state = DealState.FUNDED
        return deal

    def complete_funding(self, deal: Deal) -> Deal:
        """Authorize a buyer-approved order: the money becomes held.

        Only valid from AWAITING_APPROVAL. If the buyer has not
        approved yet, PayPal answers 422 ORDER_NOT_APPROVED and the
        HTTPError propagates with the deal left in AWAITING_APPROVAL.
        """
        if not self.use_paypal:
            if deal.state == DealState.AWAITING_APPROVAL:
                deal.state = DealState.FUNDED
            return deal
        if deal.state != DealState.AWAITING_APPROVAL:
            raise ValueError(
                f"cannot complete funding in state {deal.state}")
        auth = paypal_client.authorize_order(deal.paypal_order_id)
        deal.paypal_auth_id = auth["authorization_id"]
        deal.state = DealState.FUNDED
        return deal

    def open_dispute(self, deal: Deal) -> Deal:
        """Record a genuinely-open dispute on the deal. Evidence
        gathering reads this flag, so the checklist's no_dispute check
        can actually fail through the product flow."""
        deal.dispute_filed = True
        return deal

    def resolve_dispute(self, deal: Deal) -> Deal:
        deal.dispute_filed = False
        return deal

    def monitor(self, deal: Deal, tracking: dict,
                days_since_funding: int,
                messages: list[str] | None = None):
        """Run one monitoring pass: gather evidence, checklist, adjudicate."""
        if deal.state not in (DealState.FUNDED, DealState.MONITORING):
            raise ValueError(f"cannot monitor deal in state {deal.state}")
        deal.state = DealState.MONITORING
        evidence = gather_evidence(deal, tracking, days_since_funding,
                                   messages)
        results = run_checklist(deal, evidence)
        decision = adjudicate(deal, results)
        audit.record(decision)
        return decision, results

    def settle(self, deal: Deal, decision) -> Deal:
        """Execute the verdict. INVESTIGATE keeps monitoring; after 3
        inconclusive cycles the deal escalates to humans.

        Settlement is guarded (2026-10-24): a settled deal refuses any
        further settle with ValueError, and a PayPal deal cannot settle
        without a recorded authorization — previously settle() flipped
        the state to RELEASED/REFUNDED even when no capture/void could
        possibly run, and happily re-settled a RELEASED deal as
        REFUNDED.
        """
        if deal.state in (DealState.RELEASED, DealState.REFUNDED):
            raise ValueError(
                f"deal {deal.id} is already settled ({deal.state.value}); "
                f"refusing to settle again as {decision.verdict.value}")
        if decision.verdict in (Verdict.RELEASE, Verdict.REFUND):
            if deal.state not in (DealState.FUNDED, DealState.MONITORING,
                                  DealState.DECIDED):
                raise ValueError(
                    f"cannot settle deal in state {deal.state}")
            if self.use_paypal and not deal.paypal_auth_id:
                raise RuntimeError(
                    f"cannot settle deal {deal.id}: no PayPal "
                    "authorization recorded — money would not move")
        if decision.verdict == Verdict.RELEASE:
            if self.use_paypal:
                paypal_client.capture(deal.paypal_auth_id)
            deal.state = DealState.RELEASED
        elif decision.verdict == Verdict.REFUND:
            if self.use_paypal:
                paypal_client.void(deal.paypal_auth_id)
            deal.state = DealState.REFUNDED
        else:
            self._inconclusive = getattr(self, "_inconclusive", {})
            n = self._inconclusive.get(deal.id, 0) + 1
            self._inconclusive[deal.id] = n
            deal.state = (DealState.ESCALATED if n >= 3
                          else DealState.MONITORING)
        return deal

    def escalate(self, deal: Deal) -> Deal:
        """Explicit human handoff, e.g. from the dashboard."""
        deal.state = DealState.ESCALATED
        return deal

    def run_cycle(self, deal: Deal, tracking: dict,
                  days_since_funding: int,
                  messages: list[str] | None = None) -> Deal:
        """One full monitor+settle pass. Returns the deal in its new state."""
        decision, _ = self.monitor(deal, tracking, days_since_funding,
                                   messages)
        return self.settle(deal, decision)
