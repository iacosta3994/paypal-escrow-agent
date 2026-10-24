"""Demo harness: run a scripted escrow deal end-to-end (no PayPal calls).

Usage: python -m src.simulator [clean_delivery|never_ships|ambiguous]
"""
import sys
import uuid

from . import audit
from .checklist import run_checklist
from .decision_engine import adjudicate
from .evidence import TrackingSimulator, gather_evidence
from .models import Deal, DealState, DealTerms, Verdict
from . import paypal_client  # noqa: F401  (wired in week 1)


SCENARIO_DEALS = {
    "clean_delivery": (50000, "Used mirrorless camera"),
    "never_ships": (30000, "Mechanical keyboard"),
    "ambiguous": (120000, "Laptop"),
}


def run_demo(scenario: str) -> None:
    amount_cents, item = SCENARIO_DEALS.get(scenario, (50000, "Used mirrorless camera"))
    terms = DealTerms(
        amount_cents=amount_cents, currency="USD",
        item_description=item,
        buyer_email="buyer@example.com", seller_email="seller@example.com",
        delivery_zip="47172", window_days=14,
    )
    deal = Deal(id=f"demo-{uuid.uuid4().hex[:8]}", terms=terms,
                state=DealState.FUNDED)
    sim = TrackingSimulator(scenario)
    dollars = amount_cents / 100
    print(f"deal {deal.id} [{scenario}] FUNDED — ${dollars:,.2f} held\n")
    for day in (1, 3, 6, 9):
        tracking = sim.advance(3 if day > 1 else 1)
        ev = gather_evidence(deal, tracking, days_since_funding=day)
        results = run_checklist(deal, ev)
        decision = adjudicate(deal, results)
        audit.record(decision)
        print(f"day {day}: {decision.verdict.value} "
              f"(tier {decision.tier}, ${decision.cost_usd:.4f}, "
              f"conf {decision.confidence:.2f})")
        print(f"  reasoning: {decision.reasoning}")
        for r in results:
            print(f"    [{r.status.value:7}] {r.name}: {r.evidence}")
        print()
        if decision.verdict in (Verdict.RELEASE, Verdict.REFUND):
            deal.state = (DealState.RELEASED
                          if decision.verdict == Verdict.RELEASE
                          else DealState.REFUNDED)
            print(f"-> {deal.state.value.upper()}: "
                  f"{f'capture ${dollars:,.2f} to seller' if decision.verdict == Verdict.RELEASE else f'void: ${dollars:,.2f} back to buyer'}")
            break
        # INVESTIGATE keeps monitoring; a later day may resolve it.
    print(f"\ntotal AI spend this run: ${audit.total_spend()}")


if __name__ == "__main__":
    run_demo(sys.argv[1] if len(sys.argv) > 1 else "clean_delivery")
