"""The adapter contracts the kernel depends on.

These are typing.Protocols: an adapter is any object with the right
methods — no inheritance, no registration. The kernel receives an
adapter by constructor injection and defaults to the shipped
implementation, so existing call sites (and every existing test
fixture) keep working without rewiring.

====================================================================
MoneyAdapter — everything the kernel needs from a money mover
====================================================================

The kernel (DealManager + split_settlement) uses exactly six
operations, each mirroring one function the PayPal client already
exposed. Amounts are integer cents everywhere; currency is an
ISO code string. Methods return plain dicts in the shapes below —
an implementation for another processor must translate its native
responses into these shapes, because kernel code reads these keys
and nothing else:

  create_authorization(amount_cents, currency, description,
                       payment_source=None) -> {
      "order_id": str,
      "status": str | None,
      "approve_url": str | None,        # buyer-approval URL, if the
                                        # funding source needs one
      "authorization_id": str | None,   # set iff funds are ALREADY
                                        # held at creation (e.g.
                                        # card-funded); None means
                                        # the deal waits in
                                        # AWAITING_APPROVAL
      "authorization_status": str | None,
  }
  authorize_order(order_id) -> same summary shape, with
      "authorization_id" set. Raises (HTTPError in the PayPal impl)
      when the buyer has not approved yet — DealManager deliberately
      lets that propagate and leaves the deal AWAITING_APPROVAL.
  get_order(order_id) -> the processor's raw order record (dict).
      Kernel code does not read this today; it is the read-back /
      source-of-truth surface tests and operators use.
  get_authorization(authorization_id) -> the processor's raw
      authorization record (dict). The kernel reads ONLY
      "create_time" and "expiration_time" from it (authorization
      aging), and treats any exception as "unknown — fall back".
  capture(authorization_id, amount_cents=None, currency="USD",
          final_capture=False) -> the processor's raw capture
      record (dict); split settlement reads only "id" from it.
      amount_cents=None means "capture the full held amount".
  void(authorization_id) -> dict; releases the remaining hold back
      to the buyer. Kernel code reads nothing from the result.

====================================================================
EvidenceAdapter — everything the kernel needs from the world
====================================================================

One method:

  gather(deal, tracking, days_since_funding, messages=None) -> dict

``deal`` is a models.Deal, ``days_since_funding`` an int, ``messages``
an optional list of party-message strings. ``tracking`` is the raw
tracking record for THIS pass (see below) — an adapter may source it
itself and ignore the argument, but the simulator adapter passes it
through, and dispute-submitted tracking proof is merged over it
(carrier/adapters' data wins; proof fills only missing fields).

The returned evidence dict is the contract the checklist consumes.
A second adapter (e.g. the planned GitHub code-deliverable adapter)
must return the same keys with the same meanings; keys it cannot
supply for its deal type should be present with neutral values
(None / False / []), never omitted — the checklist treats a missing
key and a neutral value alike today, but the audit trail should not
have to guess which happened.

  "tracking": dict — carrier-shaped tracking record, subkeys all
      optional: "number" (str), "carrier" (str), "pickup_scan"
      (ISO timestamp str), "in_transit" (bool), "delivered" (bool),
      "delivered_at" (ISO str), "delivered_zip" (str),
      "stalled_days" (number), "exception" (str),
      "items_expected" / "items_delivered" (ints, partial shipment).
      A non-shipment adapter maps its delivery facts onto these
      names (e.g. a merged PR is the "delivered" event) or returns
      {} and lets its own facts ride in its checks' inputs.
  "days_since_funding": int — echoed window clock for within_window.
  "authorized_cents": int — the amount actually held; MUST come
      from the money side / deal state, never a constant, or the
      amount_match check is unreachable (a real bug this project
      already had once).
  "dispute_filed": bool — from live deal state.
  "dispute_evidence": list[dict] — party-submitted records, each
      {"party", "kind", "text", "data", ...}; kinds the kernel
      understands today: "tracking_proof", "damage_report",
      "settlement_offer" (whose data carries split_release_cents).
  "buyer_confirmed": bool, "receipt_confirmed_at": str | None —
      the buyer's own receipt confirmation; the buyer is the address
      authority in the decision rules.
  "seller_completion_rate": float | None — 0..1 prior completion.
  "messages": list[str] — party messages scanned for condition
      reports ("damaged", "broken", ...).
  "gathered_at": ISO timestamp str.

The kernel-assembled fields (dispute state, buyer confirmation,
authorized amount) come from the Deal in the current implementation;
a future adapter that gathers from an external system wholesale
still owns returning ALL keys above for its deals.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class MoneyAdapter(Protocol):
    """See module docstring for the full contract and dict shapes."""

    def create_authorization(self, amount_cents: int, currency: str,
                             description: str,
                             payment_source: dict | None = None) -> dict:
        ...

    def authorize_order(self, order_id: str) -> dict:
        ...

    def get_order(self, order_id: str) -> dict:
        ...

    def get_authorization(self, authorization_id: str) -> dict:
        ...

    def capture(self, authorization_id: str,
                amount_cents: int | None = None, currency: str = "USD",
                final_capture: bool = False) -> dict:
        ...

    def void(self, authorization_id: str) -> dict:
        ...


@runtime_checkable
class EvidenceAdapter(Protocol):
    """See module docstring for the evidence-dict contract."""

    def gather(self, deal, tracking: dict, days_since_funding: int,
               messages: list[str] | None = None) -> dict:
        ...
