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
import secrets
import uuid
from datetime import datetime, timezone

from . import audit, auth_expiry, milestones as milestones_mod
from .adapters import PayPalMoneyAdapter, SimulatorEvidenceAdapter
from .checklist import run_checklist
from .decision_engine import adjudicate
from .models import (Deal, DealState, DealTerms, Decision,
                     MilestoneState, Verdict)
from .policy import DEFAULT_POLICY, Policy


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DealManager:
    def __init__(self, use_paypal: bool = False, *,
                 money_adapter=None, evidence_adapter=None,
                 policy: Policy | None = None):
        """Kernel entry point.

        Adapters (Wave 1 / A1): the manager depends on the
        MoneyAdapter / EvidenceAdapter interfaces from
        src.adapters.base, never on a concrete client. Defaults keep
        every pre-A1 call site identical: the PayPal adapter (which
        delegates to src/paypal_client.py) and the simulator-backed
        evidence adapter. Injecting a money_adapter implies money
        mode (use_paypal becomes True): plain injection means "move
        real money through this adapter", and an injected adapter
        with use_paypal left False would otherwise be dead wiring.

        policy (Wave 1 / A2): the operator-judgment thresholds from
        src/policy.py; DEFAULT_POLICY reproduces prior behavior.
        """
        self.money = money_adapter or PayPalMoneyAdapter()
        self.evidence_adapter = (evidence_adapter
                                 or SimulatorEvidenceAdapter())
        self.policy = policy or DEFAULT_POLICY
        self.use_paypal = use_paypal or money_adapter is not None
        self.deals: dict[str, Deal] = {}
        self._transition_listeners = []

    def _mark_milestones_funded(self, deal: Deal) -> None:
        """Funding holds every milestone's share: PENDING milestones
        become FUNDED_HELD."""
        for m in deal.milestones:
            if m.state == MilestoneState.PENDING:
                m.state = MilestoneState.FUNDED_HELD

    def _stamp_authorization(self, deal: Deal) -> None:
        """Record when the hold was placed, and read the money
        adapter's own created/expires timestamps back onto the deal
        (best effort: the authorization object is the source of truth
        for its expiry, and expiry surfacing falls back to funded_at
        when the read fails)."""
        deal.funded_at = _now_iso()
        if deal.paypal_auth_id:
            try:
                auth = self.money.get_authorization(deal.paypal_auth_id)
            except Exception:
                return
            deal.paypal_auth_created_at = auth.get("create_time")
            deal.paypal_auth_expires_at = auth.get("expiration_time")

    def on_transition(self, cb) -> None:
        """Register a transition listener: cb(deal, old_state,
        new_state), called only when a deal's state actually changes."""
        self._transition_listeners.append(cb)

    def _set_state(self, deal: Deal, new_state: DealState) -> None:
        old_state = deal.state
        deal.state = new_state
        if old_state != new_state:
            for cb in list(self._transition_listeners):
                cb(deal, old_state, new_state)

    def propose(self, terms: DealTerms, proposer: str | None = None,
                *, milestones: list | None = None,
                share_links: bool = False) -> Deal:
        if proposer not in (None, "buyer", "seller"):
            raise ValueError(
                f"proposer must be 'buyer', 'seller', or None, "
                f"got {proposer!r}")
        if milestones:
            milestones_mod.validate_milestones(
                terms.amount_cents, milestones)
        deal = Deal(id=f"deal-{uuid.uuid4().hex[:8]}", terms=terms)
        deal.proposed_by = proposer
        deal.milestones = list(milestones or [])
        if share_links:
            # Demo-grade identity: an unguessable token per party;
            # holding your link IS being that party (README states
            # the limitation plainly — production would use auth).
            deal.share_tokens = {
                party: secrets.token_urlsafe(18)
                for party in ("buyer", "seller")}
        if proposer is not None:
            # The proposer authored the terms, so proposing IS their
            # acceptance; the counterparty must still accept.
            deal.terms_acceptances[proposer] = _now_iso()
        self.deals[deal.id] = deal
        return deal

    def share_link(self, deal: Deal, party: str) -> str:
        """The party's shareable link to this deal (with its token
        when the deal issued share links)."""
        token = deal.share_tokens.get(party)
        link = f"/deal/{deal.id}?party={party}"
        return f"{link}&k={token}" if token else link

    def withdraw(self, deal: Deal, party: str) -> Deal:
        """Withdraw an unaccepted proposal (proposer only, and only
        before the counterparty accepts): the deal ends WITHDRAWN
        with no money moved. Once both parties have accepted, the
        terms are locked — a change means a new proposal."""
        if party not in ("buyer", "seller"):
            raise ValueError(
                f"party must be 'buyer' or 'seller', got {party!r}")
        if deal.state != DealState.PROPOSED:
            raise ValueError(
                f"cannot withdraw a deal in state {deal.state}")
        if deal.proposed_by != party:
            raise ValueError("only the proposer can withdraw")
        counterparty = "seller" if party == "buyer" else "buyer"
        if counterparty in deal.terms_acceptances:
            raise ValueError(
                "cannot withdraw: the counterparty has already "
                "accepted — the terms are locked")
        self._set_state(deal, DealState.WITHDRAWN)
        return deal

    def accept_terms(self, deal: Deal, party: str) -> Deal:
        """Record a party's acceptance of the proposed terms. Only
        valid while the deal is PROPOSED; re-accepting is a no-op
        that keeps the original timestamp."""
        if party not in ("buyer", "seller"):
            raise ValueError(
                f"party must be 'buyer' or 'seller', got {party!r}")
        if deal.state != DealState.PROPOSED:
            raise ValueError(
                f"cannot accept terms in state {deal.state}")
        if party not in deal.terms_acceptances:
            deal.terms_acceptances[party] = _now_iso()
        return deal

    def decline_terms(self, deal: Deal, party: str) -> Deal:
        """Decline the proposed terms: the deal ends DECLINED
        (terminal) with no money moved. Only valid from PROPOSED."""
        if party not in ("buyer", "seller"):
            raise ValueError(
                f"party must be 'buyer' or 'seller', got {party!r}")
        if deal.state != DealState.PROPOSED:
            raise ValueError(
                f"cannot decline terms in state {deal.state}")
        self._set_state(deal, DealState.DECLINED)
        return deal

    def confirm_receipt(self, deal: Deal, party: str = "buyer") -> Deal:
        """Record the buyer's confirmation of receipt. Buyer only,
        only once the deal is FUNDED or MONITORING; repeat calls are
        a no-op keeping the first timestamp. No state change."""
        if party != "buyer":
            raise ValueError(
                f"only the buyer can confirm receipt, got {party!r}")
        if deal.state not in (DealState.FUNDED, DealState.MONITORING):
            raise ValueError(
                f"cannot confirm receipt in state {deal.state}")
        if deal.buyer_confirmed_at is None:
            deal.buyer_confirmed_at = _now_iso()
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
        if deal.state != DealState.PROPOSED:
            raise ValueError(
                f"cannot fund deal in state {deal.state}")
        if deal.proposed_by is not None:
            # Party-proposed deal: BOTH parties must have accepted
            # the terms before any money moves. (Manager-proposed
            # deals, proposed_by None, are pre-agreed by construction
            # and fund exactly as before.)
            for party in ("buyer", "seller"):
                if party not in deal.terms_acceptances:
                    raise ValueError(
                        f"cannot fund: {party} has not accepted the terms")
        if self.use_paypal:
            auth = self.money.create_authorization(
                deal.terms.amount_cents, deal.terms.currency,
                f"Escrow: {deal.terms.item_description}",
                payment_source=payment_source,
            )
            deal.paypal_order_id = auth["order_id"]
            deal.paypal_approve_url = auth.get("approve_url")
            # FIX (2026-10-24): the authorization id used to be dropped
            # here, which made every later capture/void unreachable.
            deal.paypal_auth_id = auth.get("authorization_id")
            self._set_state(
                deal, DealState.FUNDED if deal.paypal_auth_id
                else DealState.AWAITING_APPROVAL)
            if deal.paypal_auth_id:
                self._stamp_authorization(deal)
                self._mark_milestones_funded(deal)
        else:
            deal.funded_at = _now_iso()
            self._set_state(deal, DealState.FUNDED)
            self._mark_milestones_funded(deal)
        return deal

    def complete_funding(self, deal: Deal) -> Deal:
        """Authorize a buyer-approved order: the money becomes held.

        Only valid from AWAITING_APPROVAL. If the buyer has not
        approved yet, PayPal answers 422 ORDER_NOT_APPROVED and the
        HTTPError propagates with the deal left in AWAITING_APPROVAL.
        """
        if not self.use_paypal:
            if deal.state == DealState.AWAITING_APPROVAL:
                deal.funded_at = _now_iso()
                self._set_state(deal, DealState.FUNDED)
                self._mark_milestones_funded(deal)
            return deal
        if deal.state != DealState.AWAITING_APPROVAL:
            raise ValueError(
                f"cannot complete funding in state {deal.state}")
        auth = self.money.authorize_order(deal.paypal_order_id)
        deal.paypal_auth_id = auth["authorization_id"]
        self._stamp_authorization(deal)
        self._set_state(deal, DealState.FUNDED)
        self._mark_milestones_funded(deal)
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

    def check_auth_expiry(self, deal: Deal,
                          now: datetime | None = None) -> dict | None:
        """Surface an aging authorization (see src/auth_expiry.py):
        WARN at 20 days records an audit flag; ESCALATE/EXPIRED at
        25/29 records an audit escalation and escalates the deal.
        Each level is recorded once per deal. Returns the status."""
        status = auth_expiry.expiry_status(deal, now, policy=self.policy)
        if status is None or status["level"] == "ok":
            return status
        age = status["age_days"]
        left = status["days_left"]
        if status["level"] == "warn" and not deal.auth_expiry_warned:
            deal.auth_expiry_warned = True
            audit.record(Decision(
                deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                confidence=0.0,
                reasoning=f"Authorization aging: the held funds' "
                          f"authorization is {age} days old with "
                          f"~{left} days before it expires "
                          f"(29-day limit). Re-authorize or escalate "
                          f"before it lapses.",
                key_evidence=[f"authorization age: {age} days",
                              f"days to expiry: ~{left}"],
                tier=0, cost_usd=0.0))
        elif (status["level"] in ("escalate", "expired")
              and not deal.auth_expiry_escalated):
            deal.auth_expiry_warned = True
            deal.auth_expiry_escalated = True
            audit.record(Decision(
                deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                confidence=0.0,
                reasoning=f"Authorization expiry "
                          f"{'has passed' if status['level'] == 'expired' else 'imminent'}: "
                          f"authorization is {age} days old, "
                          f"~{left} days left. Escalated to humans: "
                          f"re-authorize or settle now — an expired "
                          f"authorization leaves nothing to capture "
                          f"or void.",
                key_evidence=[f"authorization age: {age} days",
                              f"days to expiry: ~{left}"],
                tier=0, cost_usd=0.0))
            self._set_state(deal, DealState.ESCALATED)
        return status

    def monitor(self, deal: Deal, tracking: dict,
                days_since_funding: int,
                messages: list[str] | None = None,
                now: datetime | None = None):
        """Run one monitoring pass: gather evidence, checklist,
        adjudicate — then surface authorization aging (S5)."""
        if deal.state not in (DealState.FUNDED, DealState.MONITORING):
            raise ValueError(f"cannot monitor deal in state {deal.state}")
        self._set_state(deal, DealState.MONITORING)
        evidence = self.evidence_adapter.gather(
            deal, tracking, days_since_funding, messages)
        results = run_checklist(deal, evidence, policy=self.policy)
        deal.last_check_results = results
        if deal.milestones:
            # Milestone deals adjudicate PER MILESTONE (each with
            # its own audited decision); there is no whole-deal
            # verdict to settle — settlement is per share.
            decisions = self._run_milestone_flow(deal, results)
            self.check_auth_expiry(deal, now)
            summary = decisions[-1] if decisions else Decision(
                deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                confidence=0.0,
                reasoning="Milestone monitoring pass: no milestone "
                          "changed state on this evidence.",
                key_evidence=[], tier=0, cost_usd=0.0)
            return summary, results
        decision = adjudicate(deal, results, policy=self.policy)
        audit.record(decision)
        self.check_auth_expiry(deal, now)
        return decision, results

    # --- milestones (2026-10-26): per-milestone tracking + settlement ---

    def _run_milestone_flow(self, deal: Deal, results) -> list:
        """One monitoring pass over a milestone deal's open
        milestones: UNKNOWN evidence marks EVIDENCE_IN, a decisive
        Tier 0 evaluation settles the share, a condition FAIL opens
        a milestone dispute (INVESTIGATE, audited once, at the
        transition). Returns the decisions recorded this pass."""
        decisions = []
        by_name = {r.name: r for r in results}
        for m in deal.milestones:
            if m.state in milestones_mod.SETTLED_STATES \
                    or m.state == MilestoneState.DISPUTED:
                continue
            result = by_name.get(m.check) if m.check else None
            if (result is not None
                    and result.status.value == "unknown"
                    and m.state == MilestoneState.FUNDED_HELD):
                m.state = MilestoneState.EVIDENCE_IN
            verdict, reasoning = milestones_mod.evaluate_milestone(
                deal, m, results)
            if verdict == Verdict.INVESTIGATE:
                m.state = MilestoneState.DISPUTED
                d = Decision(
                    deal_id=deal.id, verdict=Verdict.INVESTIGATE,
                    confidence=0.0, reasoning=reasoning,
                    key_evidence=[result.evidence] if result else [],
                    tier=0, cost_usd=0.0)
                audit.record(d)
                decisions.append(d)
            elif verdict in (Verdict.RELEASE, Verdict.REFUND):
                decisions.append(self.settle_milestone(
                    deal, m.id,
                    "release" if verdict == Verdict.RELEASE
                    else "refund",
                    reasoning=reasoning))
        return decisions

    def settle_milestone(self, deal: Deal, milestone_id: str,
                         outcome: str, *,
                         split_release_cents: int | None = None,
                         reasoning: str = "") -> Decision:
        """Settle ONE milestone's share. outcome: "release" (full
        share to the seller), "refund" (share back to the buyer),
        or "split" (split_release_cents to the seller, the rest of
        the share back to the buyer).

        Money (PayPal mode): a release captures the share with a
        NON-FINAL partial capture — the proven split path's first
        leg — leaving the remaining authorization held for the
        other milestones. Refunded amounts are decided here and
        voided when the authorization closes (see
        _complete_milestone_deal): a PayPal authorization can be
        voided only once. Sim mode moves no money, exactly like
        the rest of the web layer.
        """
        m = milestones_mod.get_milestone(deal, milestone_id)
        if deal.state not in (DealState.FUNDED, DealState.MONITORING,
                              DealState.DECIDED):
            raise ValueError(
                f"cannot settle a milestone in state {deal.state}")
        if m.state in milestones_mod.SETTLED_STATES:
            raise ValueError(
                f"milestone '{m.title}' is already settled "
                f"({m.state.value}); a settled milestone never "
                f"reopens")
        if outcome == "release":
            released = m.amount_cents
            verdict = Verdict.RELEASE
        elif outcome == "refund":
            released = 0
            verdict = Verdict.REFUND
        elif outcome == "split":
            if (not isinstance(split_release_cents, int)
                    or isinstance(split_release_cents, bool)
                    or not 0 < split_release_cents < m.amount_cents):
                raise ValueError(
                    f"split settlement of milestone '{m.title}' "
                    f"needs split_release_cents with 0 < x < "
                    f"{m.amount_cents}, got {split_release_cents!r}")
            released = split_release_cents
            verdict = Verdict.SPLIT
        else:
            raise ValueError(f"unknown milestone outcome {outcome!r}")
        if self.use_paypal and not deal.paypal_auth_id:
            raise RuntimeError(
                f"cannot settle milestone on deal {deal.id}: no "
                f"PayPal authorization recorded — money would not "
                f"move")
        if self.use_paypal and released > 0:
            capture = self.money.capture(
                deal.paypal_auth_id, released, deal.terms.currency,
                final_capture=False)
            if isinstance(capture, dict):
                deal.capture_id = (capture.get("capture_id")
                                   or deal.capture_id)
        m.state = MilestoneState.VERIFIED
        m.released_cents = released
        m.settled_at = _now_iso()
        m.state = MilestoneState.RELEASED if released > 0 \
            else MilestoneState.REFUNDED
        decision = Decision(
            deal_id=deal.id, verdict=verdict, confidence=0.95,
            reasoning=reasoning or (
                f"Milestone '{m.title}' settled as "
                f"{outcome}: {released} of {m.amount_cents} cents "
                f"of the share released to the seller."),
            key_evidence=[f"milestone {m.id} ({m.title}): "
                          f"{released}/{m.amount_cents} cents "
                          f"released"],
            tier=0, cost_usd=0.0,
            split_release_cents=released if verdict == Verdict.SPLIT
            else None)
        audit.record(decision)
        self._complete_milestone_deal(deal)
        return decision

    def _complete_milestone_deal(self, deal: Deal) -> None:
        """When every milestone is settled, close the deal at the
        aggregate: all shares released -> RELEASED, none ->
        REFUNDED, a mix -> SPLIT_SETTLED at the measured fraction.
        In PayPal mode the authorization's remainder (the refunded
        shares) is voided here — the one void the rail allows."""
        if not deal.milestones:
            return
        if any(m.state not in milestones_mod.SETTLED_STATES
               for m in deal.milestones):
            return
        if deal.state in (DealState.RELEASED, DealState.REFUNDED,
                           DealState.SPLIT_SETTLED):
            return
        released = sum(m.released_cents or 0 for m in deal.milestones)
        total = deal.terms.amount_cents
        if self.use_paypal and released < total:
            self.money.void(deal.paypal_auth_id)
        if released == total:
            self._set_state(deal, DealState.RELEASED)
        elif released == 0:
            self._set_state(deal, DealState.REFUNDED)
        else:
            deal.split_released_cents = released
            deal.split_voided_cents = total - released
            self._set_state(deal, DealState.SPLIT_SETTLED)

    def confirm_milestone(self, deal: Deal, milestone_id: str,
                          party: str = "buyer") -> Decision | None:
        """Buyer confirmation of one milestone's completion.
        Confirmation is evidence: for a manual milestone it is the
        decisive evidence and the share settles; for a check
        milestone it is recorded and the milestone flow re-runs on
        the latest evidence."""
        if party != "buyer":
            raise ValueError(
                f"only the buyer can confirm a milestone, "
                f"got {party!r}")
        m = milestones_mod.get_milestone(deal, milestone_id)
        if deal.state not in (DealState.FUNDED, DealState.MONITORING):
            raise ValueError(
                f"cannot confirm a milestone in state {deal.state}")
        if m.buyer_confirmed_at is None:
            m.buyer_confirmed_at = _now_iso()
        if m.check is None:
            verdict, reasoning = milestones_mod.evaluate_milestone(
                deal, m, deal.last_check_results)
            if verdict == Verdict.RELEASE:
                return self.settle_milestone(
                    deal, m.id, "release", reasoning=reasoning)
            return None
        if deal.last_check_results:
            decisions = self._run_milestone_flow(
                deal, deal.last_check_results)
            return decisions[-1] if decisions else None
        return None

    def dispute_milestone(self, deal: Deal, milestone_id: str,
                          party: str, text: str = "") -> dict:
        """Open a dispute on ONE milestone (either party)."""
        if party not in ("buyer", "seller"):
            raise ValueError(
                f"party must be 'buyer' or 'seller', got {party!r}")
        m = milestones_mod.get_milestone(deal, milestone_id)
        if deal.state not in (DealState.FUNDED, DealState.MONITORING):
            raise ValueError(
                f"cannot dispute a milestone in state {deal.state}")
        if m.state in milestones_mod.SETTLED_STATES \
                or m.state == MilestoneState.DISPUTED:
            raise ValueError(
                f"milestone '{m.title}' cannot be disputed from "
                f"state {m.state.value}")
        return milestones_mod.open_milestone_dispute(
            deal, m, party, text)

    def resolve_milestone(self, deal: Deal, milestone_id: str,
                          party: str, outcome: str, *,
                          split_release_cents: int | None = None,
                          ) -> Decision:
        """Resolve a DISPUTED milestone as release / refund / split,
        with the resolution and its reasoning on the audit record."""
        m = milestones_mod.get_milestone(deal, milestone_id)
        if m.state != MilestoneState.DISPUTED:
            raise ValueError(
                f"milestone '{m.title}' is not disputed "
                f"({m.state.value})")
        reasoning = (
            f"Dispute on milestone '{m.title}' resolved by {party} "
            f"as {outcome}: release "
            f"{m.amount_cents if outcome == 'release' else split_release_cents if outcome == 'split' else 0} "
            f"of {m.amount_cents} cents to the seller"
            + (f", voiding the remaining "
               f"{m.amount_cents - (split_release_cents or 0)} cents "
               f"of the share back to the buyer."
               if outcome == "split" else "."))
        return self.settle_milestone(
            deal, milestone_id, outcome,
            split_release_cents=split_release_cents,
            reasoning=reasoning)

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
        if deal.state in (DealState.RELEASED, DealState.REFUNDED,
                           DealState.SPLIT_SETTLED, DealState.DECLINED,
                           DealState.WITHDRAWN):
            raise ValueError(
                f"deal {deal.id} is already settled ({deal.state.value}); "
                f"refusing to settle again as {decision.verdict.value}")
        if decision.verdict in (Verdict.RELEASE, Verdict.REFUND,
                                Verdict.SPLIT):
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
                self.money.capture(deal.paypal_auth_id)
            self._set_state(deal, DealState.RELEASED)
        elif decision.verdict == Verdict.REFUND:
            if self.use_paypal:
                self.money.void(deal.paypal_auth_id)
            self._set_state(deal, DealState.REFUNDED)
        elif decision.verdict == Verdict.SPLIT:
            if decision.split_release_cents is None:
                raise ValueError(
                    f"cannot split-settle deal {deal.id}: decision "
                    "carries no split_release_cents")
            from .split_settlement import execute_split
            result = execute_split(deal, decision, self.use_paypal,
                                   money_adapter=self.money)
            deal.capture_id = result["capture_id"]
            deal.split_released_cents = result["released_cents"]
            deal.split_voided_cents = result["voided_cents"]
            self._set_state(deal, DealState.SPLIT_SETTLED)
        else:
            self._inconclusive = getattr(self, "_inconclusive", {})
            n = self._inconclusive.get(deal.id, 0) + 1
            self._inconclusive[deal.id] = n
            # An expiry escalation (auth_expiry) is sticky: an
            # inconclusive adjudication must not resurrect it.
            self._set_state(
                deal, DealState.ESCALATED
                if n >= self.policy.investigate_escalation_cycles or deal.state == DealState.ESCALATED
                else DealState.MONITORING)
        return deal

    def escalate(self, deal: Deal) -> Deal:
        """Explicit human handoff, e.g. from the dashboard."""
        self._set_state(deal, DealState.ESCALATED)
        return deal

    def run_cycle(self, deal: Deal, tracking: dict,
                  days_since_funding: int,
                  messages: list[str] | None = None) -> Deal:
        """One full monitor+settle pass. Returns the deal in its new state."""
        decision, _ = self.monitor(deal, tracking, days_since_funding,
                                   messages)
        if deal.milestones:
            # Settlement already happened per milestone inside
            # monitor(); there is no whole-deal verdict to settle.
            return deal
        return self.settle(deal, decision)
