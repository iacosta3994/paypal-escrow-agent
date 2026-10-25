"""Adapter boundaries (Wave 1 / A1, 2026-10-24).

The kernel (DealManager, split_settlement) depends on the
MoneyAdapter / EvidenceAdapter interfaces in src/adapters/base.py,
not on concrete clients. These tests pin:

- the shipped adapters satisfy the Protocols;
- DealManager defaults to the shipped adapters (existing fixtures
  untouched) and accepts injected fakes without rewiring;
- the PayPal adapter delegates through the paypal_client MODULE, so
  the suite's long-standing monkeypatch fixtures
  (monkeypatch.setattr(paypal_client, "capture", ...)) keep working;
- a second evidence adapter can be dropped in against the documented
  evidence-dict contract.
"""
from src import paypal_client
from src.adapters import (EvidenceAdapter, MoneyAdapter,
                          PayPalMoneyAdapter, SimulatorEvidenceAdapter)
from src.deal_manager import DealManager
from src.models import DealState, Decision, Verdict
from src.split_settlement import execute_split

from .e2e_support import CAMERA, audit_log, make_terms, no_grok  # noqa: F401


# --- shipped adapters satisfy the interfaces ---

def test_shipped_adapters_satisfy_protocols():
    assert isinstance(PayPalMoneyAdapter(), MoneyAdapter)
    assert isinstance(SimulatorEvidenceAdapter(), EvidenceAdapter)


def test_deal_manager_defaults_to_shipped_adapters():
    mgr = DealManager()
    assert isinstance(mgr.money, PayPalMoneyAdapter)
    assert isinstance(mgr.evidence_adapter, SimulatorEvidenceAdapter)
    assert mgr.use_paypal is False  # sim mode unchanged by default


def test_simulator_evidence_adapter_returns_contract_keys():
    mgr = DealManager()
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    ev = SimulatorEvidenceAdapter().gather(deal, {}, 1)
    for key in ("tracking", "days_since_funding", "authorized_cents",
                "dispute_filed", "dispute_evidence", "buyer_confirmed",
                "receipt_confirmed_at", "seller_completion_rate",
                "messages", "gathered_at"):
        assert key in ev, key


# --- injected fakes: the kernel never notices the swap ---

class FakeMoney:
    """A minimal second MoneyAdapter (records every call)."""

    def __init__(self, authorized_at_creation=True):
        self.calls = []
        self.authorized_at_creation = authorized_at_creation

    def create_authorization(self, amount_cents, currency, description,
                             payment_source=None):
        self.calls.append(("create_authorization", amount_cents))
        return {"order_id": "ORD-FAKE", "status": "CREATED",
                "approve_url": None if self.authorized_at_creation
                else "https://approve.example/ORD-FAKE",
                "authorization_id": "AUTH-FAKE"
                if self.authorized_at_creation else None,
                "authorization_status": "CREATED"}

    def authorize_order(self, order_id):
        self.calls.append(("authorize_order", order_id))
        return {"order_id": order_id, "status": "COMPLETED",
                "approve_url": None, "authorization_id": "AUTH-FAKE",
                "authorization_status": "CREATED"}

    def get_order(self, order_id):
        return {"id": order_id}

    def get_authorization(self, authorization_id):
        self.calls.append(("get_authorization", authorization_id))
        return {"id": authorization_id,
                "create_time": "2026-11-01T00:00:00Z",
                "expiration_time": "2026-11-30T00:00:00Z"}

    def capture(self, authorization_id, amount_cents=None,
                currency="USD", final_capture=False):
        self.calls.append(("capture", authorization_id, amount_cents,
                           final_capture))
        return {"id": "CAP-FAKE", "status": "COMPLETED"}

    def void(self, authorization_id):
        self.calls.append(("void", authorization_id))
        return {"status": "VOIDED"}


def test_injected_money_adapter_drives_full_lifecycle():
    fake = FakeMoney()
    mgr = DealManager(money_adapter=fake)  # injection implies money mode
    assert mgr.use_paypal is True
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    assert deal.paypal_auth_id == "AUTH-FAKE"
    assert deal.paypal_auth_expires_at == "2026-11-30T00:00:00Z"
    mgr.settle(deal, Decision(deal_id=deal.id, verdict=Verdict.RELEASE,
                               confidence=0.97, reasoning="ok",
                               key_evidence=[], tier=0, cost_usd=0.0))
    assert deal.state == DealState.RELEASED
    assert ("capture", "AUTH-FAKE", None, False) in fake.calls


def test_injected_money_adapter_approval_flow():
    fake = FakeMoney(authorized_at_creation=False)
    mgr = DealManager(money_adapter=fake)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    assert deal.state == DealState.AWAITING_APPROVAL
    mgr.complete_funding(deal)
    assert deal.state == DealState.FUNDED
    assert ("authorize_order", "ORD-FAKE") in fake.calls


def test_split_settlement_runs_through_injected_adapter():
    fake = FakeMoney()
    mgr = DealManager(money_adapter=fake)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    result = execute_split(
        deal, Decision(deal_id=deal.id, verdict=Verdict.SPLIT,
                       confidence=0.9, reasoning="partial",
                       key_evidence=[], tier=0, cost_usd=0.0,
                       split_release_cents=20000),
        use_paypal=True, money_adapter=fake)
    assert result["capture_id"] == "CAP-FAKE"
    assert result["released_cents"] == 20000
    assert result["voided_cents"] == CAMERA[0] - 20000
    assert ("capture", "AUTH-FAKE", 20000, False) in fake.calls
    assert ("void", "AUTH-FAKE") in fake.calls


# --- monkeypatch compatibility: delegation is through the module ---

def test_paypal_adapter_delegates_dynamically_to_module(monkeypatch):
    seen = []
    monkeypatch.setattr(paypal_client, "capture",
                        lambda auth_id, **k: seen.append(auth_id)
                        or {"id": "CAP-PATCHED"})
    out = PayPalMoneyAdapter().capture("AUTH-9")
    assert out == {"id": "CAP-PATCHED"} and seen == ["AUTH-9"]


def test_full_capture_keeps_bare_call_shape(monkeypatch):
    """Regression (found in the final full-suite run): the pre-A1
    kernel called paypal_client.capture(auth_id) with NO kwargs for
    a full capture, and suite doubles exist with exactly that
    one-argument signature (test_idempotency's counting spy). The
    adapter must not inject default kwargs into that call."""
    monkeypatch.setattr(paypal_client, "create_authorization",
                        lambda *a, **k: {
                            "order_id": "ORD-B", "status": "CREATED",
                            "approve_url": None,
                            "authorization_id": "AUTH-B"})
    monkeypatch.setattr(paypal_client, "get_authorization",
                        lambda auth_id: {"id": auth_id})
    seen = []

    def one_arg_capture(auth_id):  # narrow on purpose
        seen.append(auth_id)
        return {"id": "CAP-B"}

    monkeypatch.setattr(paypal_client, "capture", one_arg_capture)
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, Decision(deal_id=deal.id, verdict=Verdict.RELEASE,
                               confidence=0.97, reasoning="ok",
                               key_evidence=[], tier=0, cost_usd=0.0))
    assert seen == ["AUTH-B"]


def test_manager_money_path_still_honors_module_patches(monkeypatch):
    """The exact fixture style test_auth_expiry / test_idempotency
    have always used — patch functions on paypal_client, construct
    DealManager(use_paypal=True) — must keep working post-A1."""
    monkeypatch.setattr(paypal_client, "create_authorization",
                        lambda *a, **k: {
                            "order_id": "ORD-P", "status": "CREATED",
                            "approve_url": None,
                            "authorization_id": "AUTH-P"})
    monkeypatch.setattr(paypal_client, "get_authorization",
                        lambda auth_id: {"id": auth_id})
    captured = []
    monkeypatch.setattr(paypal_client, "capture",
                        lambda auth_id, **k: captured.append(auth_id)
                        or {"id": "CAP-P"})
    mgr = DealManager(use_paypal=True)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    mgr.settle(deal, Decision(deal_id=deal.id, verdict=Verdict.RELEASE,
                               confidence=0.97, reasoning="ok",
                               key_evidence=[], tier=0, cost_usd=0.0))
    assert captured == ["AUTH-P"]
    assert deal.state == DealState.RELEASED


# --- a second evidence adapter drops in unchanged ---

class FixedEvidence:
    """EvidenceAdapter that ignores tracking and reports a clean
    delivery — the shape a GitHub/code-deliverable adapter takes:
    same gather() signature, same evidence-dict contract."""

    def __init__(self):
        self.calls = 0

    def gather(self, deal, tracking, days_since_funding, messages=None):
        self.calls += 1
        return {
            "tracking": {"number": "GH-1", "carrier": "github",
                         "pickup_scan": "2026-11-02T10:00:00Z",
                         "delivered": True,
                         "delivered_at": "2026-11-05T14:22:00Z",
                         "delivered_zip": deal.terms.delivery_zip},
            "days_since_funding": days_since_funding,
            "authorized_cents": deal.terms.amount_cents,
            "dispute_filed": False, "dispute_evidence": [],
            "buyer_confirmed": False, "receipt_confirmed_at": None,
            "seller_completion_rate": 0.95, "messages": messages or [],
            "gathered_at": "2026-11-05T15:00:00Z",
        }


def test_injected_evidence_adapter_drives_monitor(audit_log, no_grok):
    fake = FixedEvidence()
    mgr = DealManager(evidence_adapter=fake)
    deal = mgr.propose(make_terms(CAMERA))
    mgr.fund(deal)
    decision, _ = mgr.monitor(deal, tracking={}, days_since_funding=3)
    assert fake.calls == 1
    assert decision.verdict == Verdict.RELEASE
