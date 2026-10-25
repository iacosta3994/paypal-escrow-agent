#!/usr/bin/env python3
"""Held-out benchmark generator (workstream A4, frozen 2026-10-24).

Copyright 2026 Brian Acosta. Licensed under the Apache License,
Version 2.0 (see LICENSE in the repo root).

Generates eval/benchmark_cases.json: 48 deal cases that are NOT in
(and do not near-duplicate) the golden corpus, test_decisions, or
the complexity ladder. Every case is specified by construction —
item, amount, ZIP, carrier, tracking shape, dispute state, seller
rate — together with its expected verdict and the eight determinate
ground-truth facts (the build_state() vocabulary) for per-question
sensor scoring.

Before anything is written, every case is run through the REAL
decision path (gather_evidence -> run_checklist -> build_state ->
adjudicate, with the Grok tier stubbed unavailable exactly as the
test suite's no_grok fixture does, and the shadow forest stubbed
out — the shadow never influences a verdict when Grok is off: both
fallthrough branches return INVESTIGATE). The generator REFUSES to
write a case whose constructed facts or verdict differ from its
specification, so a frozen file can never silently disagree with
the build that froze it.

Reproducibility: all surface randomness (tracking numbers) comes
from random.Random(SEED) consumed in spec order; re-running this
script reproduces benchmark_cases.json byte-for-byte. The script
refuses to overwrite an existing cases file whose content differs
from what it would generate (pass --force only if you deliberately
intend to invalidate every prior comparison).

    ~/laya-scratch-venv/bin/python eval/generate_benchmark.py
"""
import hashlib
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import decision_engine  # noqa: E402
from src.checklist import run_checklist  # noqa: E402
from src.disputes import submit_evidence  # noqa: E402
from src.evidence import gather_evidence  # noqa: E402
from src.laya_forest import build_state  # noqa: E402
from src.models import Deal, DealTerms  # noqa: E402

SEED = 20261024
OUT = REPO / "eval" / "benchmark_cases.json"
SIDECAR = REPO / "eval" / "benchmark_cases.sha256"

# Ground-truth fact patterns (build_state vocabulary). CONDITION_OK
# is UNKNOWN on a clean case by construction: condition_clear only
# ever returns FAIL or UNKNOWN, never PASS.
CLEAN = {"SHIPPED": "YES", "DELIVERED": "YES", "ADDRESS_MATCH": "YES",
         "WITHIN_WINDOW": "YES", "AMOUNT_MATCH": "YES",
         "CONDITION_OK": "UNKNOWN", "BUYER_COMPLAINT": "NO",
         "SELLER_HISTORY_GOOD": "YES"}


def F(**over):
    f = dict(CLEAN)
    f.update(over)
    return f


def delivered(zip_mode="AGREED", **extra):
    """A delivered tracking dict; generator fills number/zip."""
    t = {"number": None, "carrier": extra.pop("carrier", "UPS"),
         "pickup_scan": "2026-11-02T10:00:00Z", "delivered": True,
         "delivered_at": "2026-11-05T14:22:00Z",
         "delivered_zip": zip_mode}
    t.update(extra)
    return t


def in_transit(**extra):
    t = {"number": None, "carrier": extra.pop("carrier", "UPS"),
         "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True}
    t.update(extra)
    return t


# Each spec: construction inputs + expected verdict + expected facts.
# authorized_cents None means "matches the deal amount".
SPECS = [
    # ---------------- RELEASE (8) ----------------
    dict(id="rel_clean_standard", category="clean_release",
         desc="textbook clean delivery, mid-size amount",
         item="vintage film lens 50mm", amount_cents=18750,
         authorized_cents=None, delivery_zip="60614", window_days=14,
         days_since_funding=4, tracking=delivered(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.97,
         facts=F(), expected_verdict="release"),
    dict(id="rel_buyer_confirmed_no_scan", category="clean_release",
         desc="in transit, no delivery scan, but buyer confirmed receipt",
         item="espresso grinder (prosumer)", amount_cents=43200,
         authorized_cents=None, delivery_zip="73301", window_days=14,
         days_since_funding=6, tracking=in_transit(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=True, seller_completion_rate=0.95,
         facts=F(ADDRESS_MATCH="UNKNOWN"), expected_verdict="release"),
    dict(id="rel_buyer_confirmed_wrong_zip", category="clean_release",
         desc="delivered to a neighbouring ZIP, buyer confirms receipt "
              "(buyer is the address authority)",
         item="professional camera rig", amount_cents=259900,
         authorized_cents=None, delivery_zip="20001", window_days=14,
         days_since_funding=5, tracking=delivered("20002"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=True, seller_completion_rate=0.98,
         facts=F(ADDRESS_MATCH="NO"), expected_verdict="release"),
    dict(id="rel_bad_seller_history", category="clean_release",
         desc="clean delivery; poor seller history is a distractor "
              "(non-decisive fact)",
         item="smartwatch Series 9", amount_cents=8999,
         authorized_cents=None, delivery_zip="98101", window_days=14,
         days_since_funding=3,
         tracking=delivered(carrier="DHL"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.62,
         facts=F(SELLER_HISTORY_GOOD="NO"), expected_verdict="release"),
    dict(id="rel_benign_messages", category="clean_release",
         desc="clean delivery with friendly buyer chatter (no damage "
              "phrases)",
         item="board game collection (6 titles)", amount_cents=15675,
         authorized_cents=None, delivery_zip="30303", window_days=14,
         days_since_funding=4, tracking=delivered(carrier="FedEx"),
         messages=["Package arrived, thanks so much!",
                   "Leaving a five-star review tonight"],
         dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.99,
         facts=F(), expected_verdict="release"),
    dict(id="rel_multi_item_full", category="clean_release",
         desc="all 3 of 3 items delivered (control for partial cases)",
         item="cast iron cookware set (3 pc)", amount_cents=62100,
         authorized_cents=None, delivery_zip="80202", window_days=14,
         days_since_funding=5,
         tracking=delivered(carrier="USPS", items_expected=3,
                            items_delivered=3),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(), expected_verdict="release"),
    dict(id="rel_no_pickup_scan_but_delivered", category="clean_release",
         desc="delivery scan present but no pickup scan: shipped fact "
              "UNKNOWN, decisive checks still pass",
         item="electric guitar (used)", amount_cents=34750,
         authorized_cents=None, delivery_zip="55401", window_days=14,
         days_since_funding=4,
         tracking={"number": None, "carrier": "OnTrac",
                   "delivered": True,
                   "delivered_at": "2026-11-05T09:10:00Z",
                   "delivered_zip": "AGREED"},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(SHIPPED="UNKNOWN"), expected_verdict="release"),
    dict(id="rel_last_day_of_window", category="clean_release",
         desc="delivered on the final day of a 10-day window",
         item="standing desk with motor", amount_cents=128400,
         authorized_cents=None, delivery_zip="19103", window_days=10,
         days_since_funding=10, tracking=delivered(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.97,
         facts=F(), expected_verdict="release"),

    # ---------------- REFUND (9) ----------------
    dict(id="ref_stalled_label_6d", category="clean_refund",
         desc="label created, no pickup in 6 days (stall threshold 5)",
         item="carbon kayak paddle", amount_cents=21500,
         authorized_cents=None, delivery_zip="37201", window_days=14,
         days_since_funding=6,
         tracking={"number": None, "carrier": "UPS", "stalled_days": 6},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.93,
         facts=F(SHIPPED="NO", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN"),
         expected_verdict="refund"),
    dict(id="ref_stalled_label_12d_bad_history", category="clean_refund",
         desc="label stalled 12 days and seller history is poor",
         item="air compressor 6 gal", amount_cents=27450,
         authorized_cents=None, delivery_zip="37212", window_days=14,
         days_since_funding=12,
         tracking={"number": None, "carrier": "FedEx",
                   "stalled_days": 12},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.71,
         facts=F(SHIPPED="NO", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="refund"),
    dict(id="ref_never_shipped_window_blown", category="clean_refund",
         desc="no tracking at all and the 14-day window is gone",
         item="e-bike battery pack 52V", amount_cents=76400,
         authorized_cents=None, delivery_zip="64108", window_days=14,
         days_since_funding=19, tracking={},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", WITHIN_WINDOW="NO"),
         expected_verdict="refund"),
    dict(id="ref_late_delivered_tight_window", category="clean_refund",
         desc="delivered, but day 13 of a 10-day window",
         item="turntable with preamp", amount_cents=54325,
         authorized_cents=None, delivery_zip="97205", window_days=10,
         days_since_funding=13, tracking=delivered(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(WITHIN_WINDOW="NO"), expected_verdict="refund"),
    dict(id="ref_late_delivered_day21", category="clean_refund",
         desc="delivered on day 21 of a 14-day window; weak seller",
         item="leather armchair", amount_cents=98000,
         authorized_cents=None, delivery_zip="85004", window_days=14,
         days_since_funding=21, tracking=delivered(carrier="FedEx"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.68,
         facts=F(WITHIN_WINDOW="NO", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="refund"),
    dict(id="ref_amount_under_by_250", category="clean_refund",
         desc="clean delivery but authorized 250 cents under the deal",
         item="3D printer (enclosed)", amount_cents=40000,
         authorized_cents=39750, delivery_zip="02108", window_days=14,
         days_since_funding=4, tracking=delivered(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.97,
         facts=F(AMOUNT_MATCH="NO"), expected_verdict="refund"),
    dict(id="ref_amount_overcharge", category="clean_refund",
         desc="clean delivery but authorized 1200 cents over the deal",
         item="gaming monitor 34in ultrawide", amount_cents=67800,
         authorized_cents=69000, delivery_zip="48201", window_days=14,
         days_since_funding=4, tracking=delivered(carrier="FedEx"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(AMOUNT_MATCH="NO"), expected_verdict="refund"),
    dict(id="ref_amount_mismatch_in_transit", category="clean_refund",
         desc="amount mismatch is a hard fail even mid-transit",
         item="ergonomic office chair", amount_cents=31200,
         authorized_cents=26200, delivery_zip="29401", window_days=14,
         days_since_funding=3, tracking=in_transit(carrier="USPS"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.94,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN",
                 AMOUNT_MATCH="NO"),
         expected_verdict="refund"),
    dict(id="ref_both_no_stalled_and_late", category="clean_refund",
         desc="mixed valence: never shipped AND window blown "
              "(the forest's known both-NO blind spot; Tier 0 owns it)",
         item="drone with 4K camera", amount_cents=45999,
         authorized_cents=None, delivery_zip="83702", window_days=14,
         days_since_funding=17,
         tracking={"number": None, "carrier": "UPS", "stalled_days": 8},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.92,
         facts=F(SHIPPED="NO", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", WITHIN_WINDOW="NO"),
         expected_verdict="refund"),

    # ---------------- INVESTIGATE (18) ----------------
    dict(id="inv_in_transit_progressing", category="investigate",
         desc="picked up and moving, nothing decisive yet",
         item="sewing machine (computerised)", amount_cents=18999,
         authorized_cents=None, delivery_zip="06511", window_days=14,
         days_since_funding=3, tracking=in_transit(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN"),
         expected_verdict="investigate"),
    dict(id="inv_label_awaiting_pickup", category="investigate",
         desc="label created 2 days ago, awaiting pickup scan",
         item="wine fridge (24 bottle)", amount_cents=27450,
         authorized_cents=None, delivery_zip="70112", window_days=14,
         days_since_funding=2,
         tracking={"number": None, "carrier": "FedEx",
                   "stalled_days": 2},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN"),
         expected_verdict="investigate"),
    dict(id="inv_stalled_4_near_boundary", category="investigate",
         desc="stalled 4 days: one day under the hard-fail threshold",
         item="telescope with equatorial mount", amount_cents=66000,
         authorized_cents=None, delivery_zip="23219", window_days=14,
         days_since_funding=4,
         tracking={"number": None, "carrier": "UPS", "stalled_days": 4},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.79,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="investigate"),
    dict(id="inv_no_tracking_within_window", category="investigate",
         desc="no tracking submitted yet, window still open",
         item="cordless drill driver kit", amount_cents=38900,
         authorized_cents=None, delivery_zip="94110", window_days=14,
         days_since_funding=2, tracking={},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.94,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN"),
         expected_verdict="investigate"),
    dict(id="inv_address_mismatch_far_zip", category="investigate",
         desc="delivered to 90210; the deal's ZIP is in Miami",
         item="electric scooter (folding)", amount_cents=51200,
         authorized_cents=None, delivery_zip="33101", window_days=14,
         days_since_funding=5, tracking=delivered("90210"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(ADDRESS_MATCH="NO"), expected_verdict="investigate"),
    dict(id="inv_delivered_no_zip", category="investigate",
         desc="delivery scan with no delivered ZIP recorded",
         item="acoustic guitar (solid top)", amount_cents=22800,
         authorized_cents=None, delivery_zip="40203", window_days=14,
         days_since_funding=4,
         tracking={"number": None, "carrier": "USPS",
                   "pickup_scan": "2026-11-02T10:00:00Z",
                   "delivered": True,
                   "delivered_at": "2026-11-05T11:40:00Z"},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(ADDRESS_MATCH="UNKNOWN"),
         expected_verdict="investigate"),
    dict(id="inv_damaged_carrier_exception", category="investigate",
         desc="delivered; carrier exception reports a crushed box",
         item="home server NAS (4 bay)", amount_cents=84500,
         authorized_cents=None, delivery_zip="30318", window_days=14,
         days_since_funding=4,
         tracking=delivered(exception="outer box crushed, contents "
                                       "rattling"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.97,
         facts=F(CONDITION_OK="NO"), expected_verdict="investigate"),
    dict(id="inv_damaged_buyer_message", category="investigate",
         desc="delivered; buyer message reports a broken screen",
         item="OLED television 55in", amount_cents=119900,
         authorized_cents=None, delivery_zip="10001", window_days=14,
         days_since_funding=4, tracking=delivered(carrier="FedEx"),
         messages=["It arrived but the screen is broken, not working "
                   "at all"],
         dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(CONDITION_OK="NO"), expected_verdict="investigate"),
    dict(id="inv_wrong_item_message", category="investigate",
         desc="delivered; buyer says the wrong item was sent",
         item="instant camera bundle", amount_cents=67400,
         authorized_cents=None, delivery_zip="78704", window_days=14,
         days_since_funding=4, tracking=delivered(),
         messages=["This is not what I ordered - wrong item in the "
                   "box"],
         dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(CONDITION_OK="NO"), expected_verdict="investigate"),
    dict(id="inv_dispute_open_clean_delivered", category="investigate",
         desc="clean delivery but a dispute is open: no auto-release",
         item="espresso machine (dual boiler)", amount_cents=45800,
         authorized_cents=None, delivery_zip="60657", window_days=14,
         days_since_funding=4, tracking=delivered(),
         messages=[], dispute_filed=True, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(BUYER_COMPLAINT="YES"), expected_verdict="investigate"),
    dict(id="inv_dispute_open_in_transit", category="investigate",
         desc="dispute opened while the parcel is still moving",
         item="winter parka (expedition)", amount_cents=29750,
         authorized_cents=None, delivery_zip="80014", window_days=14,
         days_since_funding=3, tracking=in_transit(carrier="FedEx"),
         messages=[], dispute_filed=True, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.93,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN",
                 BUYER_COMPLAINT="YES"),
         expected_verdict="investigate"),
    dict(id="inv_damage_report_persists", category="investigate",
         desc="buyer damage report filed in a dispute that was then "
              "resolved: the condition fact stays on the record",
         item="ultrawide monitor 49in", amount_cents=73300,
         authorized_cents=None, delivery_zip="19107", window_days=14,
         days_since_funding=5, tracking=delivered(),
         messages=[], dispute_filed=False,
         dispute_evidence=[{"party": "buyer", "kind": "damage_report",
                            "text": "corner dented, screen flickers",
                            "data": {}}],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(CONDITION_OK="NO"), expected_verdict="investigate"),
    dict(id="inv_partial_with_dispute", category="investigate",
         desc="2 of 4 items delivered but a dispute is open: the "
              "partial split is blocked",
         item="dining chair set of 4", amount_cents=88000,
         authorized_cents=None, delivery_zip="55402", window_days=14,
         days_since_funding=6,
         tracking=delivered(items_expected=4, items_delivered=2),
         messages=[], dispute_filed=True, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.94,
         facts=F(DELIVERED="UNKNOWN", BUYER_COMPLAINT="YES"),
         expected_verdict="investigate"),
    dict(id="inv_partial_with_damage", category="investigate",
         desc="1 of 2 items delivered and the parcel shows water "
              "damage: condition dispute outranks the split",
         item="vinyl record collection (40 LPs)", amount_cents=52400,
         authorized_cents=None, delivery_zip="48226", window_days=14,
         days_since_funding=5,
         tracking=delivered(items_expected=2, items_delivered=1,
                            exception="water damage to outer packaging"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(DELIVERED="UNKNOWN", CONDITION_OK="NO"),
         expected_verdict="investigate"),
    dict(id="inv_settlement_offer_not_delivered", category="investigate",
         desc="buyer offers a split but nothing has been delivered: "
              "a settlement needs delivery first",
         item="road bicycle (carbon fork)", amount_cents=95000,
         authorized_cents=None, delivery_zip="97209", window_days=14,
         days_since_funding=4, tracking=in_transit(),
         messages=[], dispute_filed=True,
         dispute_evidence=[{"party": "buyer",
                            "kind": "settlement_offer", "text": "",
                            "data": {"split_release_cents": 38000}}],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN",
                 BUYER_COMPLAINT="YES"),
         expected_verdict="investigate"),
    dict(id="inv_buyer_confirmed_but_damaged", category="investigate",
         desc="buyer confirmed receipt, then reported damage",
         item="stand mixer (tilt head)", amount_cents=38600,
         authorized_cents=None, delivery_zip="37212", window_days=14,
         days_since_funding=5, tracking=delivered(),
         messages=["arrived damaged, handle snapped off"],
         dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=True, seller_completion_rate=0.97,
         facts=F(CONDITION_OK="NO"), expected_verdict="investigate"),
    dict(id="inv_seller_tracking_proof_in_transit", category="investigate",
         desc="carrier shows nothing; seller's dispute tracking proof "
              "supplies the pickup scan (carrier data would win, "
              "but there is none)",
         item="embroidery machine", amount_cents=61700,
         authorized_cents=None, delivery_zip="64110", window_days=14,
         days_since_funding=3, tracking={},
         messages=[], dispute_filed=True,
         dispute_evidence=[{"party": "seller", "kind": "tracking_proof",
                            "text": "",
                            "data": {"tracking_number": "SELLER-PROOF",
                                     "carrier": "UPS",
                                     "pickup_scan":
                                         "2026-11-03T08:15:00Z"}}],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN",
                 BUYER_COMPLAINT="YES"),
         expected_verdict="investigate"),
    dict(id="inv_delivered_zip_only_no_scan", category="investigate",
         desc="a delivered ZIP is recorded but there is no delivery "
              "scan and no buyer confirmation",
         item="patio furniture set", amount_cents=44900,
         authorized_cents=None, delivery_zip="85016", window_days=14,
         days_since_funding=4,
         tracking={"number": None, "carrier": "FedEx",
                   "pickup_scan": "2026-11-02T10:00:00Z",
                   "in_transit": True, "delivered_zip": "AGREED"},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(DELIVERED="UNKNOWN"), expected_verdict="investigate"),

    # ---------------- SPLIT (7) ----------------
    dict(id="split_partial_1_of_3", category="split",
         desc="1 of 3 items delivered, odd-cent amount floors",
         item="nesting side tables (set of 3)", amount_cents=9999,
         authorized_cents=None, delivery_zip="29403", window_days=14,
         days_since_funding=5,
         tracking=delivered(items_expected=3, items_delivered=1),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(DELIVERED="UNKNOWN"), expected_verdict="split",
         expected_split_release_cents=3333),
    dict(id="split_partial_2_of_5", category="split",
         desc="2 of 5 items delivered",
         item="storage cube set (5 units)", amount_cents=45750,
         authorized_cents=None, delivery_zip="48226", window_days=14,
         days_since_funding=6,
         tracking=delivered(carrier="USPS", items_expected=5,
                            items_delivered=2),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(DELIVERED="UNKNOWN"), expected_verdict="split",
         expected_split_release_cents=18300),
    dict(id="split_partial_3_of_4", category="split",
         desc="3 of 4 items delivered",
         item="modular sofa sections (set of 4)", amount_cents=72000,
         authorized_cents=None, delivery_zip="97209", window_days=14,
         days_since_funding=6,
         tracking=delivered(items_expected=4, items_delivered=3),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.97,
         facts=F(DELIVERED="UNKNOWN"), expected_verdict="split",
         expected_split_release_cents=54000),
    dict(id="split_dispute_settlement_damaged", category="split",
         desc="damaged but kept: buyer's accepted split on an open "
              "dispute outranks the condition INVESTIGATE",
         item="vintage guitar amplifier", amount_cents=64000,
         authorized_cents=None, delivery_zip="40204", window_days=14,
         days_since_funding=5,
         tracking=delivered(exception="package visibly damaged"),
         messages=[], dispute_filed=True,
         dispute_evidence=[{"party": "buyer",
                            "kind": "settlement_offer",
                            "text": "keep it for a partial refund",
                            "data": {"split_release_cents": 28800}}],
         buyer_confirmed=False, seller_completion_rate=0.94,
         facts=F(CONDITION_OK="NO", BUYER_COMPLAINT="YES"),
         expected_verdict="split", expected_split_release_cents=28800),
    dict(id="split_dispute_settlement_clean", category="split",
         desc="open dispute settled by the buyer's split offer; "
              "condition never reported",
         item="handmade quilt (queen)", amount_cents=35000,
         authorized_cents=None, delivery_zip="40204", window_days=14,
         days_since_funding=5, tracking=delivered(carrier="USPS"),
         messages=[], dispute_filed=True,
         dispute_evidence=[{"party": "buyer",
                            "kind": "settlement_offer", "text": "",
                            "data": {"split_release_cents": 17500}}],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(BUYER_COMPLAINT="YES"), expected_verdict="split",
         expected_split_release_cents=17500),
    dict(id="split_settlement_buyer_confirmed", category="split",
         desc="split offer with delivery proven by buyer confirmation "
              "only (no carrier scan)",
         item="antique desk lamp (brass)", amount_cents=42000,
         authorized_cents=None, delivery_zip="06510", window_days=14,
         days_since_funding=5, tracking=in_transit(),
         messages=[], dispute_filed=True,
         dispute_evidence=[{"party": "buyer",
                            "kind": "settlement_offer", "text": "",
                            "data": {"split_release_cents": 21000}}],
         buyer_confirmed=True, seller_completion_rate=0.95,
         facts=F(ADDRESS_MATCH="UNKNOWN", BUYER_COMPLAINT="YES"),
         expected_verdict="split", expected_split_release_cents=21000),
    dict(id="split_partial_last_day", category="split",
         desc="1 of 2 items delivered on the final window day",
         item="two-seat kayak", amount_cents=50500,
         authorized_cents=None, delivery_zip="83706", window_days=14,
         days_since_funding=14,
         tracking=delivered(items_expected=2, items_delivered=1),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.96,
         facts=F(DELIVERED="UNKNOWN"), expected_verdict="split",
         expected_split_release_cents=25250),

    # ---------------- UNKNOWN-heavy / mixed valence (6) --------------
    dict(id="unk_empty_tracking_day0_bad_history", category="unknown_heavy",
         desc="fresh deal, no tracking yet, poor seller history",
         item="cordless drill driver", amount_cents=15800,
         authorized_cents=None, delivery_zip="33101", window_days=14,
         days_since_funding=0, tracking={},
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.66,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="investigate"),
    dict(id="unk_number_only_day1", category="unknown_heavy",
         desc="tracking number exists, nothing else, day 1",
         item="air fryer oven", amount_cents=26900,
         authorized_cents=None, delivery_zip="19147", window_days=14,
         days_since_funding=1,
         tracking={"number": None, "carrier": "USPS"},
         messages=["Seller said it should arrive this week"],
         dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.87,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="investigate"),
    dict(id="mix_delivered_dispute_bad_history", category="mixed_valence",
         desc="delivered clean, but dispute open and seller history "
              "poor: two non-decisive negatives",
         item="home gym power rack", amount_cents=81400,
         authorized_cents=None, delivery_zip="55402", window_days=14,
         days_since_funding=5, tracking=delivered(),
         messages=[], dispute_filed=True, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.58,
         facts=F(BUYER_COMPLAINT="YES", SELLER_HISTORY_GOOD="NO"),
         expected_verdict="investigate"),
    dict(id="mix_in_transit_customs_exception", category="mixed_valence",
         desc="in transit with a customs exception: condition fact "
              "is NO before anything is delivered",
         item="commercial espresso machine", amount_cents=109000,
         authorized_cents=None, delivery_zip="98109", window_days=14,
         days_since_funding=7,
         tracking=in_transit(carrier="DHL",
                             exception="held at customs inspection"),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.95,
         facts=F(DELIVERED="UNKNOWN", ADDRESS_MATCH="UNKNOWN",
                 CONDITION_OK="NO"),
         expected_verdict="investigate"),
    dict(id="mix_buyer_confirmed_amount_mismatch", category="mixed_valence",
         desc="buyer confirmed and delivery is clean, but the "
              "authorization is 1 cent over: hard fail wins",
         item="electric fireplace insert", amount_cents=56000,
         authorized_cents=56001, delivery_zip="60614", window_days=14,
         days_since_funding=5, tracking=delivered(),
         messages=[], dispute_filed=False, dispute_evidence=[],
         buyer_confirmed=True, seller_completion_rate=0.97,
         facts=F(AMOUNT_MATCH="NO"), expected_verdict="refund"),
    dict(id="mix_stalled3_with_dispute", category="mixed_valence",
         desc="label stalled 3 days (under threshold) with a dispute "
              "already open",
         item="camping tent (4 person)", amount_cents=19300,
         authorized_cents=None, delivery_zip="80203", window_days=14,
         days_since_funding=3,
         tracking={"number": None, "carrier": "FedEx",
                   "stalled_days": 3},
         messages=[], dispute_filed=True, dispute_evidence=[],
         buyer_confirmed=False, seller_completion_rate=0.94,
         facts=F(SHIPPED="UNKNOWN", DELIVERED="UNKNOWN",
                 ADDRESS_MATCH="UNKNOWN", BUYER_COMPLAINT="YES"),
         expected_verdict="investigate"),
]


def _tracking_number(rng: random.Random, carrier: str) -> str:
    digits = "0123456789"
    alnum = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    if carrier == "UPS":
        return "1Z" + "".join(rng.choice(alnum) for _ in range(16))
    if carrier == "FedEx":
        return "".join(rng.choice(digits) for _ in range(12))
    if carrier == "USPS":
        return "9400" + "".join(rng.choice(digits) for _ in range(16))
    if carrier == "DHL":
        return "".join(rng.choice(digits) for _ in range(10))
    if carrier == "OnTrac":
        return "C" + "".join(rng.choice(digits) for _ in range(14))
    return "TRK" + "".join(rng.choice(digits) for _ in range(12))


def materialize(case: dict):
    """Build (deal, evidence, results) for a stored case dict via the
    real evidence/checklist path. The benchmark runner implements the
    same mapping and cross-checks build_state against the stored
    ground-truth facts on every run."""
    terms = DealTerms(
        amount_cents=case["amount_cents"], currency="USD",
        item_description=case["item"],
        buyer_email="buyer@example.com",
        seller_email="seller@example.com",
        delivery_zip=case["delivery_zip"],
        window_days=case["window_days"])
    deal = Deal(id=case["id"], terms=terms)
    if case.get("buyer_confirmed"):
        deal.buyer_confirmed_at = "2026-11-05T12:00:00Z"
    if case.get("dispute_evidence"):
        deal.dispute_filed = True  # submissions require an open dispute
        for rec in case["dispute_evidence"]:
            submit_evidence(deal, rec["party"], rec["kind"],
                            text=rec.get("text", ""),
                            data=dict(rec.get("data") or {}))
    deal.dispute_filed = bool(case.get("dispute_filed"))
    evidence = gather_evidence(deal, case["tracking"],
                               case["days_since_funding"],
                               case.get("messages"))
    # Deterministic pins: production fills seller_completion_rate
    # from a simulator RNG and authorized_cents from the PayPal
    # authorization read-back; the benchmark fixes both per case
    # (mirrors the golden corpus, which feeds amount cases at the
    # evidence level for the same reason).
    evidence["seller_completion_rate"] = case["seller_completion_rate"]
    if case.get("authorized_cents") is not None:
        evidence["authorized_cents"] = case["authorized_cents"]
    return deal, evidence, run_checklist(deal, evidence)


def build_cases() -> list[dict]:
    rng = random.Random(SEED)
    cases = []
    for spec in SPECS:
        case = dict(spec)
        case["facts"] = dict(spec["facts"])
        tracking = dict(spec["tracking"])
        if tracking:
            carrier = tracking.get("carrier", "UPS")
            num = _tracking_number(rng, carrier)
            if tracking.get("number") is None:
                tracking["number"] = num
            if tracking.get("delivered_zip") == "AGREED":
                tracking["delivered_zip"] = spec["delivery_zip"]
            # A seller tracking proof names its own number; the RNG
            # number belongs to the carrier record only.
            for rec in case["dispute_evidence"]:
                data = rec.get("data") or {}
                if data.get("tracking_number") == "SELLER-PROOF":
                    data["tracking_number"] = (
                        "9Z" + "".join(rng.choice("0123456789")
                                       for _ in range(10)))
        case["tracking"] = tracking
        cases.append(case)
    return cases


def self_check(cases: list[dict]) -> None:
    """Refuse to freeze anything the current build disagrees with."""
    def _no_grok(deal, results):
        raise ImportError("Grok disabled for benchmark generation")

    decision_engine._tier2_grok = _no_grok
    decision_engine._tier1_shadow = lambda deal, results: None
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    triples = [(c["amount_cents"], c["item"], c["delivery_zip"])
               for c in cases]
    assert len(triples) == len(set(triples)), "duplicate case triples"
    assert 40 <= len(cases) <= 60, f"{len(cases)} cases out of range"
    for case in cases:
        deal, _ev, results = materialize(case)
        facts = build_state(deal, results)
        assert facts == case["facts"], (
            f"{case['id']}: constructed facts {facts} != spec "
            f"{case['facts']}")
        decision = decision_engine.adjudicate(deal, results)
        assert decision.verdict.value == case["expected_verdict"], (
            f"{case['id']}: verdict {decision.verdict.value} != "
            f"expected {case['expected_verdict']}")
        if case["expected_verdict"] == "split":
            assert (decision.split_release_cents
                    == case["expected_split_release_cents"]), (
                f"{case['id']}: split cents "
                f"{decision.split_release_cents} != "
                f"{case['expected_split_release_cents']}")


def main() -> int:
    force = "--force" in sys.argv
    cases = build_cases()
    self_check(cases)
    payload = {
        "meta": {
            "benchmark": "held-out escrow adjudication benchmark",
            "version": 1,
            "generated": "2026-10-24",
            "frozen": True,
            "seed": SEED,
            "generator": "eval/generate_benchmark.py",
            "case_count": len(cases),
            "copyright": "Copyright 2026 Brian Acosta",
            "license": "Apache License 2.0",
            "do_not_edit": "FROZEN held-out set. Do not edit these "
                           "cases; regeneration invalidates every "
                           "comparison against the 2026-10-24 "
                           "baseline.",
        },
        "cases": cases,
    }
    text = json.dumps(payload, indent=2) + "\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    if OUT.exists() and OUT.read_text() != text and not force:
        print("REFUSING to overwrite: existing benchmark_cases.json "
              "differs from a fresh generation. The frozen set "
              "would change and prior comparisons would be "
              "invalidated. Pass --force only deliberately.")
        return 1
    OUT.write_text(text)
    SIDECAR.write_text(f"{digest}  benchmark_cases.json\n")
    by_verdict: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    for c in cases:
        by_verdict[c["expected_verdict"]] = (
            by_verdict.get(c["expected_verdict"], 0) + 1)
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    print(f"wrote {OUT} ({len(cases)} cases)")
    print(f"sha256 {digest}")
    print(f"by expected verdict: {by_verdict}")
    print(f"by category: {by_cat}")
    print("self-check: all cases reproduce their spec facts and "
          "expected verdicts through the real decision path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
