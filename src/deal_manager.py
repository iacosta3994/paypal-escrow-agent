"""Deal lifecycle manager: the escrow state machine.

PROPOSED -> FUNDED -> MONITORING -> DECIDED -> RELEASED | REFUNDED | ESCALATED

Funding creates a PayPal AUTHORIZE order (money held). Release captures it to
the seller; refund voids it back to the buyer. Monitoring runs the checklist +
decision engine on gathered evidence.
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

    def fund(self, deal: Deal) -> Deal:
        """Lock the funds with a PayPal authorization (or simulate)."""
        if self.use_paypal:
            auth = paypal_client.create_authorization(
                deal.terms.amount_cents, deal.terms.currency,
                f"Escrow: {deal.terms.item_description}",
            )
            deal.paypal_order_id = auth["order_id"]
            # NOTE: buyer must approve via the approval URL before the
            # authorization can be completed; in the demo harness the
            # simulator stands in for approval + capture.
        deal.state = DealState.FUNDED
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
        inconclusive cycles the deal escalates to humans."""
        if decision.verdict == Verdict.RELEASE:
            if self.use_paypal and deal.paypal_auth_id:
                paypal_client.capture(deal.paypal_auth_id)
            deal.state = DealState.RELEASED
        elif decision.verdict == Verdict.REFUND:
            if self.use_paypal and deal.paypal_auth_id:
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
