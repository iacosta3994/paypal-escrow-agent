"""Cross-track integration: the joins no single track owned.

- Track 5 dispute evidence -> the coordinator's Tier 0 dispute-split
  rule in decision_engine -> Track 2's split execution in settle().
- Track 1 lifecycle transitions -> Track 3's notification outbox,
  through the on_transition listener seam.
- Track 4's webhook handler completing a Track 1 lifecycle funding.

Simulated money throughout (the live sandbox split is proven in
test_split_settlement.py); these tests prove the seams fit.
"""
from src import config, disputes, notifications, webhooks
from src.deal_manager import DealManager
from src.models import DealState, Verdict

from .e2e_support import LAPTOP, ZIP, audit_log, make_terms  # noqa: F401

DELIVERED = {
    "number": "1ZINT001", "carrier": "UPS",
    "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True,
    "delivered": True, "delivered_at": "2026-11-05T14:22:00Z",
    "delivered_zip": ZIP,
}


def _funded_laptop(manager: DealManager):
    deal = manager.propose(make_terms(LAPTOP), proposer="seller")
    manager.accept_terms(deal, "buyer")
    manager.fund(deal)
    return deal


def test_dispute_offer_settles_split_end_to_end(audit_log):
    manager = DealManager(use_paypal=False)
    deal = _funded_laptop(manager)
    manager.open_dispute(deal)
    disputes.submit_evidence(deal, "buyer", "damage_report",
                             text="dented corner, screen flickers")
    disputes.submit_evidence(deal, "buyer", "settlement_offer",
                             data={"split_release_cents": 90000})
    decision, _ = manager.monitor(deal, DELIVERED, days_since_funding=3)
    assert decision.verdict == Verdict.SPLIT
    assert decision.split_release_cents == 90000
    assert decision.tier == 0
    manager.settle(deal, decision)
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.split_released_cents == 90000
    assert deal.split_voided_cents == 30000


def test_same_dispute_without_offer_stays_investigate(audit_log):
    manager = DealManager(use_paypal=False)
    deal = _funded_laptop(manager)
    manager.open_dispute(deal)
    disputes.submit_evidence(deal, "buyer", "damage_report",
                             text="dented corner, screen flickers")
    decision, _ = manager.monitor(deal, DELIVERED, days_since_funding=3)
    assert decision.verdict == Verdict.INVESTIGATE
    manager.settle(deal, decision)
    assert deal.state == DealState.MONITORING


def test_offer_without_delivery_does_not_split(audit_log):
    # The buyer agreed a split, but nothing was ever delivered:
    # money must not move toward the seller on an undelivered deal.
    manager = DealManager(use_paypal=False)
    deal = _funded_laptop(manager)
    manager.open_dispute(deal)
    disputes.submit_evidence(deal, "buyer", "settlement_offer",
                             data={"split_release_cents": 90000})
    in_transit = {"number": "1ZINT001", "carrier": "UPS",
                  "pickup_scan": "2026-11-02T10:00:00Z",
                  "in_transit": True}
    decision, _ = manager.monitor(deal, in_transit, days_since_funding=3)
    assert decision.verdict != Verdict.SPLIT


def test_full_lifecycle_emits_notices_per_transition(audit_log, tmp_path,
                                                    monkeypatch):
    outbox = tmp_path / "notifications.jsonl"
    monkeypatch.setattr(config, "NOTIFICATIONS_PATH", str(outbox))
    manager = DealManager(use_paypal=False)
    manager.on_transition(notifications.transition_listener)
    deal = manager.propose(make_terms(LAPTOP), proposer="seller")
    manager.accept_terms(deal, "buyer")       # no state change
    manager.fund(deal)                        # PROPOSED -> FUNDED
    manager.confirm_receipt(deal)             # no state change
    manager.run_cycle(deal, DELIVERED, days_since_funding=3)
    assert deal.state == DealState.RELEASED
    records = notifications.for_deal(deal.id)
    changes = [r for r in records if r["event"] == "state_change"]
    # FUNDED, MONITORING, RELEASED: 3 transitions x 2 parties.
    assert len(changes) == 6
    assert {r["party"] for r in changes} == {"buyer", "seller"}
    assert len(notifications.for_party(deal.id, "buyer")) == 3


def test_webhook_approval_completes_lifecycle_funding(audit_log, tmp_path,
                                                      monkeypatch):
    outbox = tmp_path / "notifications.jsonl"
    monkeypatch.setattr(config, "NOTIFICATIONS_PATH", str(outbox))
    manager = DealManager(use_paypal=False)
    manager.on_transition(notifications.transition_listener)
    deal = manager.propose(make_terms(LAPTOP), proposer="seller")
    manager.accept_terms(deal, "buyer")
    # Stage the buyer-funded wait state (sim funding skips it; the
    # PayPal path parks here until the buyer approves — see fund()).
    deal.paypal_order_id = "ORDER-INT-1"
    deal.state = DealState.AWAITING_APPROVAL
    result = webhooks.handle_event(manager, {
        "id": "WH-INT-1", "event_type": "CHECKOUT.ORDER.APPROVED",
        "create_time": "2026-11-01T12:00:00Z", "resource_type": "order",
        "resource": {"id": "ORDER-INT-1", "status": "APPROVED"},
    })
    assert result["status"] == "applied"
    assert result["action"] == "funding_completed"
    assert deal.state == DealState.FUNDED
    assert deal.buyer_approved_at == "2026-11-01T12:00:00Z"
    # The funding transition notified both parties via the listener.
    changes = [r for r in notifications.for_deal(deal.id)
               if r["event"] == "state_change"]
    assert len(changes) == 2
