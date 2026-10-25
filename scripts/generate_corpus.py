#!/usr/bin/env python3
"""Corpus growth engine (workstream A5, 2026-10-24).

Copyright 2026 Brian Acosta. Licensed under the Apache License,
Version 2.0 (see LICENSE in the repo root).

Generates adversarial golden-corpus cases PROGRAMMATICALLY across
the dimensions the ladder / golden work showed are fragile:

  valence_mix         facts pointing in opposite directions
  distractor_evidence irrelevant messages / history / dispute records
  carrier_chaos       exception strings, weird statuses, multi-carrier
  amount_edges        1-cent off, exact, large over/under
  window_edges        last-day delivery, day-after, boundary transit
  unknown_density     missing tracking, missing scans, missing history
  partial_deliveries  several fractions, partial + complications
  dispute_timing      disputes before/after delivery, offers, persistence

Every case's expected verdict comes FROM CONSTRUCTION: each spec
builder knows the ground truth it built (the pattern that makes the
case a RELEASE / REFUND / INVESTIGATE / SPLIT, and the exact split
cents where applicable), and the eight determinate ground-truth
facts (the build_state() vocabulary) are derived from the raw spec
inputs by an independent derivation in this file.

Screening (a case is admitted only if ALL hold):
  (a) it runs cleanly through the real decision path
      (gather_evidence -> run_checklist -> build_state -> adjudicate,
      Grok stubbed unavailable and the shadow forest stubbed out,
      exactly as eval/generate_benchmark.py does);
  (b) the system's verdict AND constructed facts match the spec —
      a mismatch on a regular case is a generator-expectation bug
      and ABORTS the run (fix the generator, never enshrine it);
      a mismatch on a case listed in PROBES is triaged in writing
      as a genuine adjudicator finding: it is EXCLUDED from the
      admitted corpus and reported, not silently admitted;
  (c) it is not a duplicate (structural signature) of an admitted
      case, an existing golden-corpus case, or a frozen benchmark
      case (eval/benchmark_cases.json — read-only here; this script
      never writes under eval/).

Output: tests/corpus_growth_cases.json (admitted cases only, plus
screening provenance in meta). tests/test_golden_corpus_growth.py
runs the admitted cases in the suite forever.

Reproducibility: tracking numbers come from random.Random(SEED)
consumed in spec order; re-running reproduces the output
byte-for-byte (aside from nothing — the payload is deterministic).

    ~/laya-scratch-venv/bin/python scripts/generate_corpus.py
"""
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

SEED = 20261025
OUT = REPO / "tests" / "corpus_growth_cases.json"
BENCHMARK = REPO / "eval" / "benchmark_cases.json"

# -------------------------------------------------------------------
# Spec builders. A spec is the benchmark generator's case shape:
# construction inputs + expected verdict (+ split cents) + category.
# -------------------------------------------------------------------


def D(carrier="UPS", **kw):
    """Delivered tracking template; number/zip filled at build time."""
    t = {"number": None, "carrier": carrier,
         "pickup_scan": "2026-11-02T10:00:00Z", "delivered": True,
         "delivered_at": "2026-11-05T14:22:00Z",
         "delivered_zip": "AGREED"}
    t.update(kw)
    return t


def T(carrier="UPS", **kw):
    """In-transit tracking template."""
    t = {"number": None, "carrier": carrier,
         "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True}
    t.update(kw)
    return t


def L(carrier="UPS", stalled=0, **kw):
    """Label-only (stalled) tracking template."""
    t = {"number": None, "carrier": carrier, "stalled_days": stalled}
    t.update(kw)
    return t


def S(id, category, desc, item, amount, expected, tracking, zip,
      authorized=None, window=14, day=3, messages=None,
      dispute=False, dispute_evidence=None, buyer=False, rate=0.95,
      split=None):
    spec = dict(id=id, category=category, desc=desc, item=item,
                amount_cents=amount, authorized_cents=authorized,
                delivery_zip=zip, window_days=window,
                days_since_funding=day, tracking=tracking,
                messages=messages or [], dispute_filed=dispute,
                dispute_evidence=dispute_evidence or [],
                buyer_confirmed=buyer, seller_completion_rate=rate,
                expected_verdict=expected)
    if split is not None:
        spec["expected_split_release_cents"] = split
    return spec


SPECS = []

# ---------------- amount_edges (9) --------------------------------
# Sweep: exact at extreme sizes, +/-1 cent, large over/under, and
# mismatches in states where a weaker engine might let the split or
# the transit status outrank the hard amount fail.
_amount_cases = [
    ("amt_exact_one_cent_deal", 1, 1, D(), "release"),
    ("amt_exact_six_figures", 199999, 199999, D(carrier="FedEx"),
     "release"),
    ("amt_under_by_one_cent", 75000, 74999, D(), "refund"),
    ("amt_over_by_one_cent", 43200, 43201, D(carrier="USPS"), "refund"),
    ("amt_under_forty_percent", 80000, 48000, D(), "refund"),
    ("amt_over_large_delta", 12345, 23445, D(carrier="DHL"), "refund"),
    ("amt_one_cent_off_in_transit", 43210, 43209, T(carrier="USPS"),
     "refund"),
    ("amt_exact_odd_cents", 33333, 33333, D(carrier="OnTrac"),
     "release"),
]
for _id, _amt, _auth, _trk, _exp in _amount_cases:
    SPECS.append(S(
        _id, "amount_edges",
        f"authorized {_auth} vs deal {_amt} cents",
        f"amount-edge item {_id}", _amt, _exp, _trk, "47172",
        authorized=_auth, day=4))
SPECS.append(S(
    "amt_one_cent_off_partial", "amount_edges",
    "partial delivery but authorized 1 cent under: the amount hard "
    "fail outranks the partial split",
    "studio monitor pair", 60000, "refund",
    D(items_expected=2, items_delivered=1), "60614",
    authorized=59999, day=5))

# ---------------- window_edges (9) --------------------------------
_window_cases = [
    # (id, window, day, tracking, expected, split)
    ("win_last_day_short_window", 7, 7, D(), "release", None),
    ("win_day_after_short_window", 7, 8, D(), "refund", None),
    ("win_last_day_long_window", 21, 21, D(carrier="FedEx"),
     "release", None),
    ("win_day_after_long_window", 21, 22, D(), "refund", None),
    ("win_in_transit_on_last_day", 14, 14, T(), "investigate", None),
    ("win_in_transit_day_after", 14, 15, T(carrier="USPS"),
     "refund", None),
    ("win_no_tracking_on_last_day", 14, 14, {}, "investigate", None),
    ("win_no_tracking_day_after", 14, 15, {}, "refund", None),
]
for _id, _win, _day, _trk, _exp, _split in _window_cases:
    SPECS.append(S(
        _id, "window_edges",
        f"day {_day} of a {_win}-day window",
        f"window-edge item {_id}", 31500, _exp, _trk, "30301",
        window=_win, day=_day, rate=0.99 if _id.endswith("day_after")
        and not _trk else 0.95))
SPECS.append(S(
    "win_partial_on_last_day", "window_edges",
    "1 of 4 items delivered on the final day of a 10-day window: "
    "window passes, so the partial split settles",
    "greenhouse panel set (4 pc)", 40004, "split",
    D(carrier="USPS", items_expected=4, items_delivered=1), "97205",
    window=10, day=10, split=10001))

# ---------------- partial_deliveries (9 admitted) -----------------
_partial_cases = [
    # (id, amount, expected_items, delivered_items, carrier, split)
    ("partial_1_of_5", 100000, 5, 1, "UPS", 20000),
    ("partial_2_of_3_odd_cents", 99999, 3, 2, "FedEx", 66666),
    ("partial_3_of_5", 25000, 5, 3, "USPS", 15000),
    ("partial_1_of_6", 59994, 6, 1, "DHL", 9999),
    ("partial_5_of_6", 120006, 6, 5, "OnTrac", 100005),
    ("partial_half_odd_amount_bad_history", 7777, 2, 1, "UPS", 3888),
]
for _id, _amt, _exp_n, _got, _carrier, _split in _partial_cases:
    SPECS.append(S(
        _id, "partial_deliveries",
        f"{_got} of {_exp_n} items delivered, clean otherwise",
        f"partial item {_id}", _amt, "split",
        D(carrier=_carrier, items_expected=_exp_n, items_delivered=_got),
        "80202", day=5, split=_split,
        rate=0.61 if _id.endswith("bad_history") else 0.95))
SPECS.append(S(
    "partial_with_open_dispute", "partial_deliveries",
    "2 of 4 items delivered but a dispute is open: the split is "
    "blocked and the case investigates",
    "office chair set of 4", 31400, "investigate",
    D(items_expected=4, items_delivered=2), "60601", day=6,
    dispute=True))
SPECS.append(S(
    "partial_with_damage_exception", "partial_deliveries",
    "3 of 4 items delivered and the carrier reports a split-open "
    "crate: condition dispute outranks the split",
    "framed print set (4 pc)", 45600, "investigate",
    D(items_expected=4, items_delivered=3,
      exception="crate split open on arrival"), "48201", day=5))
SPECS.append(S(
    "partial_benign_messages", "partial_deliveries",
    "2 of 5 items delivered with status-chatter messages that carry "
    "no condition phrases: the split still settles",
    "garden tool bundle (5 pc)", 68200, "split",
    D(carrier="FedEx", items_expected=5, items_delivered=2), "73301",
    day=6, split=27280,
    messages=["first box arrived, waiting on the rest",
              "thanks for the update"]))

# ---------------- carrier_chaos (9) -------------------------------
SPECS.append(S(
    "chaos_attempted_delivery_exception", "carrier_chaos",
    "in transit; carrier exception says a delivery was attempted — "
    "an exception is a condition fact before anything is delivered",
    "treadmill (folding)", 89900, "investigate",
    T(carrier="FedEx",
      exception="delivery attempted — recipient unavailable"),
    "19103", day=3))
SPECS.append(S(
    "chaos_benign_exception_text_delivered", "carrier_chaos",
    "delivered, but the carrier left an exception string (signed-for "
    "note): ANY exception text is a condition fail in the checklist",
    "sofa (3 seat)", 145000, "investigate",
    D(exception="left with receptionist — signed M. Alvarez"),
    "60614", day=4))
SPECS.append(S(
    "chaos_empty_exception_string", "carrier_chaos",
    "delivered with an EMPTY exception string: falsy, so no "
    "condition problem is recorded and the clean deal releases",
    "rower machine", 76400, "release",
    D(carrier="FedEx", exception=""), "80202", day=4))
SPECS.append(S(
    "chaos_stalled_exactly_five", "carrier_chaos",
    "label stalled exactly 5 days: the stall threshold is >= 5, so "
    "pickup is a hard fail on the boundary day itself",
    "generator (portable)", 54900, "refund",
    L(stalled=5), "37201", day=5))
SPECS.append(S(
    "chaos_stall_counter_but_delivered", "carrier_chaos",
    "stale stall counter (9 days) alongside real pickup AND "
    "delivery scans: the delivery facts decide, the in-transit "
    "check is not decisive",
    "kayak (touring)", 68100, "release",
    D(stalled_days=9), "83702", day=6))
SPECS.append(S(
    "chaos_multi_carrier_handoff", "carrier_chaos",
    "carrier string names a two-carrier handoff; the checklist "
    "never parses carrier identity, and the delivery is clean",
    "paddle board (inflatable)", 45700, "release",
    D(carrier="DHL→UPS"), "29401", day=5))
_no_carrier = D()
del _no_carrier["carrier"]
SPECS.append(S(
    "chaos_no_carrier_field", "carrier_chaos",
    "tracking record with no carrier field at all, delivered clean",
    "air purifier (HEPA)", 32900, "release", _no_carrier, "40203",
    day=4))
SPECS.append(S(
    "chaos_contradictory_statuses", "carrier_chaos",
    "delivered AND in_transit both set, with a transit exception: "
    "the exception makes condition the deciding fact",
    "3D scanner", 112300, "investigate",
    D(in_transit=True, exception="still in transit — arriving late"),
    "55401", day=5))
SPECS.append(S(
    "chaos_explicit_false_statuses", "carrier_chaos",
    "pickup scan present; in_transit and delivered explicitly "
    "False: nothing decisive yet",
    "sewing table", 27400, "investigate",
    {"number": None, "carrier": "UPS",
     "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": False,
     "delivered": False}, "06511", day=4))

# ---------------- unknown_density (7) -----------------------------
SPECS.append(S(
    "unk_nothing_day_zero", "unknown_density",
    "fresh deal: no tracking, day 0, seller history missing "
    "entirely — maximal UNKNOWN density, nothing to decide on",
    "drone gimbal", 41200, "investigate", {}, "33101", day=0,
    rate=None))
SPECS.append(S(
    "unk_number_only_day2", "unknown_density",
    "a tracking number and nothing else, day 2, mediocre seller",
    "instant pot duo", 13400, "investigate",
    {"number": None, "carrier": "DHL"}, "19147", day=2, rate=0.88))
SPECS.append(S(
    "unk_pickup_scan_only", "unknown_density",
    "pickup scan exists; no transit, delivery, or ZIP data at all",
    "camping stove", 9800, "investigate",
    {"number": None, "carrier": "USPS",
     "pickup_scan": "2026-11-02T08:30:00Z"}, "80203", day=3))
SPECS.append(S(
    "unk_delivered_no_zip_no_pickup", "unknown_density",
    "delivery scan with neither a pickup scan nor a delivered ZIP: "
    "shipped and address both stay UNKNOWN, so no release",
    "vinyl turntable", 35600, "investigate",
    {"number": None, "carrier": "FedEx", "delivered": True,
     "delivered_at": "2026-11-05T16:05:00Z"}, "40203", day=4))
SPECS.append(S(
    "unk_in_transit_no_pickup_scan", "unknown_density",
    "in transit with no pickup scan: transit progresses but the "
    "shipped fact itself is UNKNOWN",
    "electric kettle", 7400, "investigate",
    {"number": None, "carrier": "UPS", "in_transit": True},
    "60601", day=3))
SPECS.append(S(
    "unk_missing_seller_history_clean", "unknown_density",
    "clean delivery and seller history missing: history is not a "
    "deciding check, so the deal still releases",
    "chef knife set", 22900, "release", D(carrier="USPS"), "97205",
    day=4, rate=None))
_no_ts = D()
del _no_ts["delivered_at"]
SPECS.append(S(
    "unk_delivered_no_timestamp", "unknown_density",
    "delivery flag set with no delivered_at timestamp: the "
    "checklist keys on the flag, so the clean deal releases",
    "board game (deluxe edition)", 11800, "release", _no_ts,
    "29403", day=4))

# ---------------- distractor_evidence (9 admitted) ----------------
SPECS.append(S(
    "distractor_bad_history_clean_deal", "distractor_evidence",
    "spotless delivery; seller completion 44% — history is a "
    "distractor on a clean deal and must not block release",
    "grand piano moving service", 215000, "release",
    D(), "10019", day=5, rate=0.44))
SPECS.append(S(
    "distractor_good_history_stalled", "distractor_evidence",
    "perfect seller history (100%) on a label stalled 7 days: "
    "history cannot rescue a hard pickup fail",
    "hot tub cover", 46300, "refund", L(carrier="FedEx", stalled=7),
    "85004", day=7, rate=1.0))
SPECS.append(S(
    "distractor_good_history_amount_off", "distractor_evidence",
    "99% seller history and a clean delivery, but authorized 1000 "
    "cents under: the amount hard fail decides",
    "espresso machine (prosumer)", 67000, "refund", D(), "02108",
    day=4, authorized=66000, rate=0.99))
SPECS.append(S(
    "distractor_chatter_clean_deal", "distractor_evidence",
    "clean delivery with three irrelevant buyer messages (no "
    "condition phrases anywhere in them)",
    "patio heater", 38700, "release", D(carrier="FedEx"), "48226",
    day=4,
    messages=["Seller is away next week — thanks for your patience!",
              "Leaving feedback tonight",
              "My neighbour has the same one and loves it"]))
SPECS.append(S(
    "distractor_chatter_in_transit", "distractor_evidence",
    "in transit with content-free buyer chatter: still nothing "
    "decisive, still investigating",
    "yoga studio bundle", 15600, "investigate", T(carrier="USPS"),
    "70112", day=3,
    messages=["any news?", "seller said friday, fingers crossed"]))
SPECS.append(S(
    "distractor_seller_note_closed_dispute", "distractor_evidence",
    "a seller NOTE left in a dispute that has since closed, on a "
    "clean delivery: notes are not condition evidence",
    "antique clock", 54800, "release", D(), "19107", day=5,
    dispute_evidence=[{"party": "seller", "kind": "note",
                       "text": "shipped in two boxes", "data": {}}]))
SPECS.append(S(
    "distractor_condition_proof_not_damage", "distractor_evidence",
    "buyer submitted condition_PROOF (not a damage_report) in a "
    "closed dispute, clean delivery: only damage_report records "
    "touch the condition check",
    "leather sofa", 93200, "release", D(carrier="FedEx"), "60657",
    day=5,
    dispute_evidence=[{"party": "buyer", "kind": "condition_proof",
                       "text": "photos show the item intact",
                       "data": {}}]))
SPECS.append(S(
    "distractor_history_exactly_threshold", "distractor_evidence",
    "seller completion exactly 0.90 — the history check's PASS "
    "boundary — on a clean delivery",
    "telescope (dobsonian)", 58700, "release", D(), "23219", day=4,
    rate=0.90))
SPECS.append(S(
    "distractor_history_just_under_threshold", "distractor_evidence",
    "seller completion 0.89 — one point under the history PASS "
    "boundary — on a clean delivery: history FAIL is not decisive",
    "microscope (binocular)", 44100, "release", D(carrier="USPS"),
    "37212", day=4, rate=0.89))

# ---------------- valence_mix (10) --------------------------------
SPECS.append(S(
    "mix_buyer_confirmed_wrong_zip", "valence_mix",
    "delivered one ZIP over from the agreed ZIP, but the buyer "
    "confirmed receipt: the buyer is the address authority",
    "film camera body", 64000, "release",
    D(delivered_zip="30302"), "30301", day=5, buyer=True))
SPECS.append(S(
    "mix_wrong_zip_unconfirmed", "valence_mix",
    "same one-ZIP-over delivery WITHOUT buyer confirmation: the "
    "address fail blocks release and the case investigates",
    "lens adapter kit", 47300, "investigate",
    D(carrier="FedEx", delivered_zip="80202"), "80201", day=5))
SPECS.append(S(
    "mix_stalled_plus_amount_off", "valence_mix",
    "label stalled 6 days AND authorized 100 cents under: two "
    "independent hard fails, either one refunds",
    "air conditioner (window)", 38000, "refund",
    L(stalled=6), "64108", day=6, authorized=37900))
SPECS.append(S(
    "mix_window_blown_plus_open_dispute", "valence_mix",
    "delivered clean but on day 16 of 14, with a dispute also "
    "open: the window hard fail outranks the dispute's investigate",
    "dining table (oak)", 87500, "refund", D(), "55402", day=16,
    dispute=True))
SPECS.append(S(
    "mix_buyer_confirmed_plus_dispute", "valence_mix",
    "buyer confirmed receipt while a dispute is open: the "
    "no_dispute fail blocks the buyer-confirmation release path",
    "stand mixer (professional)", 52900, "investigate",
    T(), "37212", day=5, dispute=True, buyer=True))
SPECS.append(S(
    "mix_buyer_confirmed_plus_damage_msg", "valence_mix",
    "buyer confirmed receipt AND reported damage in a message: "
    "the condition gate outranks the confirmation",
    "coffee grinder (commercial)", 61400, "investigate",
    D(), "97209", day=5, buyer=True,
    messages=["arrived damaged — the hopper is crushed"]))
SPECS.append(S(
    "mix_good_history_never_shipped_late", "valence_mix",
    "no tracking at all on day 20 with a 99% seller: the blown "
    "window is a hard fail history cannot soften",
    "sauna kit (2 person)", 134500, "refund", {}, "85016", day=20,
    rate=0.99))
SPECS.append(S(
    "mix_exception_plus_amount_off", "valence_mix",
    "delivered with a torn-box exception AND authorized 1 cent "
    "over: the amount hard fail fires before the condition gate, "
    "so this refunds instead of investigating",
    "server rack (12U)", 92600, "refund",
    D(exception="outer box torn"), "48201", day=4, authorized=92601))
SPECS.append(S(
    "mix_settlement_plus_amount_off", "valence_mix",
    "buyer's split offer on an open dispute, delivered — but the "
    "authorization is 1 cent under: hard fails precede the "
    "settlement split rule",
    "vintage amplifier", 45000, "refund", D(), "40204", day=5,
    authorized=44999, dispute=True,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 20000}}]))
SPECS.append(S(
    "mix_settlement_plus_window_blown", "valence_mix",
    "buyer's split offer, delivered on day 15 of 14: the window "
    "hard fail precedes the settlement split rule",
    "handwoven rug (large)", 45001, "refund", D(carrier="USPS"),
    "06510", day=15, dispute=True,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 20000}}]))

# ---------------- dispute_timing (10) -----------------------------
SPECS.append(S(
    "dispute_before_delivery", "dispute_timing",
    "dispute opened while the parcel is still in transit: no "
    "auto-anything until delivery facts exist",
    "winter tyre set (4)", 92500, "investigate",
    T(carrier="FedEx"), "20001", day=2, dispute=True))
SPECS.append(S(
    "dispute_after_delivery", "dispute_timing",
    "dispute opened after a clean delivery: the open dispute "
    "blocks the otherwise-clean release",
    "home theatre projector", 92501, "investigate",
    D(carrier="FedEx"), "20002", day=4, dispute=True))
SPECS.append(S(
    "dispute_closed_damage_report_persists", "dispute_timing",
    "damage report filed during a dispute that later closed: the "
    "condition fact persists on the record after closure",
    "gaming pc (prebuilt)", 148000, "investigate",
    D(), "60601", day=5,
    dispute_evidence=[{"party": "buyer", "kind": "damage_report",
                       "text": "screen flickers after a drop in "
                               "transit", "data": {}}]))
SPECS.append(S(
    "dispute_closed_tracking_proof_fills_pickup", "dispute_timing",
    "carrier shows nothing; the seller's dispute tracking proof "
    "(filed before the dispute closed) supplies number + pickup "
    "scan — shipped becomes known, delivery does not",
    "industrial sewing machine", 61701, "investigate", {}, "64110",
    day=3,
    dispute_evidence=[{"party": "seller", "kind": "tracking_proof",
                       "text": "",
                       "data": {"tracking_number": "SELLER-PROOF",
                                "carrier": "UPS",
                                "pickup_scan":
                                    "2026-11-03T08:15:00Z"}}]))
SPECS.append(S(
    "settlement_after_delivery_split", "dispute_timing",
    "clean delivery, open dispute, buyer's accepted split offer: "
    "the party-agreed settlement settles at the offered cents",
    "handmade quilt (king)", 64001, "split", D(), "40204", day=5,
    dispute=True, split=28800,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "keep it for a partial refund",
                       "data": {"split_release_cents": 28800}}]))
SPECS.append(S(
    "settlement_before_delivery", "dispute_timing",
    "buyer offers a split while the parcel is still in transit: a "
    "settlement needs delivery first, so this investigates",
    "road frame (titanium)", 50001, "investigate",
    T(), "97209", day=4, dispute=True,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 15000}}]))
SPECS.append(S(
    "settlement_buyer_confirmed_no_scan", "dispute_timing",
    "split offer where delivery is proven by buyer confirmation "
    "only (no carrier delivery scan): confirmation counts as "
    "delivery for the settlement rule",
    "brass floor lamp", 42002, "split", T(), "06510", day=5,
    dispute=True, buyer=True, split=21001,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 21001}}]))
SPECS.append(S(
    "settlement_max_offer_edge", "dispute_timing",
    "split offer at amount-1 cent — the largest valid offer — on "
    "a clean delivered dispute",
    "cellular modem router", 30001, "split",
    D(carrier="USPS"), "33101", day=5, dispute=True, split=30000,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 30000}}]))
SPECS.append(S(
    "settlement_one_cent_offer_edge", "dispute_timing",
    "split offer at 1 cent — the smallest valid offer — on a "
    "clean delivered dispute",
    "phone case bundle", 25001, "split", D(), "19147", day=5,
    dispute=True, split=1,
    dispute_evidence=[{"party": "buyer", "kind": "settlement_offer",
                       "text": "",
                       "data": {"split_release_cents": 1}}]))
SPECS.append(S(
    "dispute_after_window_never_shipped", "dispute_timing",
    "dispute opened late on a deal that never shipped and blew "
    "its window: hard fails refund despite the open dispute",
    "solar panel kit", 78800, "refund", L(stalled=8), "80203",
    day=16, dispute=True))

# ---------------- PROBES (screened, expected to be EXCLUDED) ------
# Each probe's expected verdict is what the system SHOULD do by the
# corpus's own stated principles (never release on ambiguity; a
# split needs every other decisive fact known-good). If the system
# disagrees, the probe is triaged below as a genuine adjudicator
# finding and EXCLUDED from the admitted corpus — never enshrined.
PROBES = {
    "probe_partial_wrong_zip": (
        "GENUINE FINDING (excluded): the Tier 0 partial-split rule "
        "guards amount/no_dispute/condition but never checks "
        "address_match, so a partial delivery to a WRONG ZIP "
        "split-releases the delivered fraction. Expected "
        "INVESTIGATE (address is a deciding check everywhere else "
        "in the corpus); the engine returns SPLIT."),
    "probe_partial_no_delivered_zip": (
        "GENUINE FINDING (excluded): same partial-split guard gap "
        "for an UNKNOWN address — a partial delivery with no "
        "delivered ZIP split-releases without the address ever "
        "being verified. Expected INVESTIGATE; engine returns "
        "SPLIT."),
    "probe_benign_not_what_message": (
        "GENUINE FINDING (excluded): the condition check's "
        "substring scan flags the praise message 'not what I "
        "expected — it's even better' via the 'not what' phrase, "
        "forcing INVESTIGATE on a clean delivered deal. Expected "
        "RELEASE by construction (the message is positive); the "
        "engine returns INVESTIGATE. Substring false positive — "
        "a finding for the coordinator, not a case to enshrine."),
}
SPECS.append(S(
    "probe_partial_wrong_zip", "partial_deliveries",
    "PROBE: 1 of 2 items delivered to a wrong ZIP — should "
    "investigate, never split-release to the seller",
    "camera lens pair", 50500, "investigate",
    D(items_expected=2, items_delivered=1, delivered_zip="99999"),
    "47172", day=5))
SPECS.append(S(
    "probe_partial_no_delivered_zip", "partial_deliveries",
    "PROBE: 1 of 2 items delivered with no delivered ZIP recorded "
    "— address unverified, should investigate",
    "speaker pair", 50501, "investigate",
    {"number": None, "carrier": "UPS",
     "pickup_scan": "2026-11-02T10:00:00Z", "delivered": True,
     "delivered_at": "2026-11-05T14:22:00Z",
     "items_expected": 2, "items_delivered": 1}, "47172", day=5))
SPECS.append(S(
    "probe_benign_not_what_message", "distractor_evidence",
    "PROBE: clean delivery; buyer message is praise that happens "
    "to contain the substring 'not what'",
    "mechanical watch", 81200, "release", D(), "10001", day=4,
    messages=["It's not what I expected — it's even better, "
              "thank you!"]))


# -------------------------------------------------------------------
# Independent fact derivation (from raw spec inputs, NOT from the
# checklist): the build_state() vocabulary the screening verifies.
# -------------------------------------------------------------------

_DAMAGE_PHRASES = ("dented", "damaged", "wrong item", "not what",
                   "broken", "not working")


def derive_facts(spec, tracking):
    t = dict(tracking or {})
    # Mirror evidence._merge_dispute_tracking: the LATEST seller
    # tracking_proof fills ONLY fields missing from carrier tracking
    # (number / carrier / pickup_scan; delivery facts never merge).
    proofs = [r for r in spec["dispute_evidence"]
              if r.get("party") == "seller"
              and r.get("kind") == "tracking_proof"]
    if proofs:
        data = proofs[-1].get("data") or {}
        if not t.get("number") and data.get("tracking_number"):
            t["number"] = data["tracking_number"]
        if not t.get("pickup_scan") and data.get("pickup_scan"):
            t["pickup_scan"] = data["pickup_scan"]
    if t.get("pickup_scan"):
        shipped = "YES"
    elif t.get("number") and t.get("stalled_days", 0) >= 5:
        shipped = "NO"
    else:
        shipped = "UNKNOWN"
    exp_n, got = t.get("items_expected"), t.get("items_delivered")
    if t.get("delivered"):
        delivered = ("UNKNOWN" if (exp_n and got is not None
                                   and got < exp_n) else "YES")
    elif spec["buyer_confirmed"]:
        delivered = "YES"
    else:
        delivered = "UNKNOWN"
    if t.get("delivered_zip"):
        address = ("YES" if t["delivered_zip"] == spec["delivery_zip"]
                   else "NO")
    else:
        address = "UNKNOWN"
    window = ("YES" if spec["days_since_funding"] <= spec["window_days"]
              else "NO")
    auth = (spec["amount_cents"] if spec["authorized_cents"] is None
            else spec["authorized_cents"])
    amount = "YES" if auth == spec["amount_cents"] else "NO"
    msgs = " ".join(spec["messages"]).lower()
    condition_bad = bool(t.get("exception")) or any(
        p in msgs for p in _DAMAGE_PHRASES) or any(
        r.get("party") == "buyer" and r.get("kind") == "damage_report"
        for r in spec["dispute_evidence"])
    rate = spec["seller_completion_rate"]
    history = ("UNKNOWN" if rate is None
               else "YES" if rate >= 0.9 else "NO")
    return {"SHIPPED": shipped, "DELIVERED": delivered,
            "ADDRESS_MATCH": address, "WITHIN_WINDOW": window,
            "AMOUNT_MATCH": amount,
            "CONDITION_OK": "NO" if condition_bad else "UNKNOWN",
            "BUYER_COMPLAINT": "YES" if spec["dispute_filed"] else "NO",
            "SELLER_HISTORY_GOOD": history}


# -------------------------------------------------------------------
# Deduplication signatures
# -------------------------------------------------------------------

def signature(spec, tracking):
    """Structural fingerprint: everything that decides the verdict,
    ignoring ids, item text, and the concrete tracking number."""
    t = tracking or {}
    ev = tuple(sorted(
        (r.get("party"), r.get("kind"), r.get("text") or "",
         json.dumps(r.get("data") or {}, sort_keys=True))
        for r in spec["dispute_evidence"]))
    auth = (spec["amount_cents"] if spec["authorized_cents"] is None
            else spec["authorized_cents"])
    return (
        spec["amount_cents"], auth, spec["delivery_zip"],
        spec["window_days"], spec["days_since_funding"],
        t.get("carrier"), bool(t.get("number")),
        bool(t.get("pickup_scan")), bool(t.get("in_transit")),
        bool(t.get("delivered")), t.get("delivered_zip"),
        t.get("exception"), t.get("stalled_days"),
        t.get("items_expected"), t.get("items_delivered"),
        tuple(spec["messages"]), spec["dispute_filed"], ev,
        spec["buyer_confirmed"], spec["seller_completion_rate"],
    )


def _golden_specs():
    """The 11 existing golden cases (tests/test_golden_corpus.py),
    re-expressed in spec shape for dedupe. Seller rate is the
    wildcard "ANY" for the DealManager-path cases (gather_evidence
    randomizes it there); the amount cases pin 0.95."""
    base_delivered = {"number": "1ZGOLD001", "carrier": "UPS",
                      "pickup_scan": "2026-11-02T10:00:00Z",
                      "delivered": True,
                      "delivered_at": "2026-11-05T14:22:00Z",
                      "delivered_zip": "47172"}

    def g(amount, tracking, day=3, messages=None, authorized=None,
          rate="ANY"):
        return dict(amount_cents=amount, authorized_cents=authorized,
                    delivery_zip="47172", window_days=14,
                    days_since_funding=day, messages=messages or [],
                    dispute_filed=False, dispute_evidence=[],
                    buyer_confirmed=False,
                    seller_completion_rate=rate), tracking

    out = []
    out.append(g(50000, {**base_delivered, "items_expected": 2,
                         "items_delivered": 1}))
    out.append(g(50000, {**base_delivered, "items_expected": 2,
                         "items_delivered": 2}))
    out.append(g(50000, {**base_delivered, "delivered_zip": "47127"}))
    out.append(g(50000, dict(base_delivered), day=15))
    out.append(g(120000, {"number": "x", "carrier": "FedEx",
                          "pickup_scan": "2026-11-02T09:00:00Z",
                          "in_transit": True,
                          "exception": "delayed — weather"}, day=4))
    out.append(g(30000, {"number": "x", "carrier": "UPS",
                         "pickup_scan": "2026-11-02T09:00:00Z",
                         "exception": "held at facility"}, day=6))
    out.append(g(120000, {**base_delivered,
                          "exception": "package visibly damaged"}))
    out.append(g(120000, dict(base_delivered),
                 messages=["arrived today — screen is cracked, "
                           "item damaged"]))
    out.append(g(50000, dict(base_delivered),
                 messages=["this is the wrong item entirely"]))
    out.append(g(50000, dict(base_delivered), authorized=49999,
                 rate=0.95))
    out.append(g(50000, dict(base_delivered), authorized=30000,
                 rate=0.95))
    return out


def _sig_matches(a, b):
    return all(x == y or x == "ANY" or y == "ANY"
               for x, y in zip(a, b))


# -------------------------------------------------------------------
# Materialization through the real decision path (benchmark pattern)
# -------------------------------------------------------------------

def _tracking_number(rng, carrier):
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


def build_cases():
    rng = random.Random(SEED)
    cases = []
    for spec in SPECS:
        case = dict(spec)
        tracking = dict(spec["tracking"])
        if tracking:
            carrier = tracking.get("carrier", "UPS")
            if tracking.get("number") is None:
                tracking["number"] = _tracking_number(rng, carrier)
            if tracking.get("delivered_zip") == "AGREED":
                tracking["delivered_zip"] = spec["delivery_zip"]
            for rec in case["dispute_evidence"]:
                data = rec.get("data") or {}
                if data.get("tracking_number") == "SELLER-PROOF":
                    data["tracking_number"] = (
                        "9Z" + "".join(rng.choice("0123456789")
                                       for _ in range(10)))
        case["tracking"] = tracking
        case["facts"] = derive_facts(spec, tracking)
        cases.append(case)
    return cases


def materialize(case):
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
    # Deterministic pins (same rationale as the benchmark generator):
    # production fills the seller rate from a simulator RNG and the
    # authorized amount from the PayPal read-back; the corpus fixes
    # both per case.
    evidence["seller_completion_rate"] = case["seller_completion_rate"]
    if case.get("authorized_cents") is not None:
        evidence["authorized_cents"] = case["authorized_cents"]
    return deal, evidence, run_checklist(deal, evidence)


# -------------------------------------------------------------------
# Screening + main
# -------------------------------------------------------------------

def main():
    def _no_grok(deal, results):
        raise ImportError("Grok disabled for corpus generation")

    decision_engine._tier2_grok = _no_grok
    decision_engine._tier1_shadow = lambda deal, results: None

    cases = build_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids in SPECS"

    bench = json.loads(BENCHMARK.read_text())
    existing = [signature(c, c["tracking"]) for c in bench["cases"]]
    existing += [signature(s, t) for s, t in _golden_specs()]

    admitted, rejected, findings = [], [], []
    seen = []
    for case in cases:
        sig = signature(case, case["tracking"])
        if any(_sig_matches(sig, s) for s in seen):
            rejected.append((case, "duplicate of an admitted case"))
            continue
        if any(_sig_matches(sig, s) for s in existing):
            rejected.append((case, "duplicate of golden/benchmark"))
            continue
        try:
            deal, _ev, results = materialize(case)
            facts = build_state(deal, results)
            decision = decision_engine.adjudicate(deal, results)
        except Exception as exc:  # (a) must run cleanly
            rejected.append((case, f"error through decision path: "
                                   f"{type(exc).__name__}: {exc}"))
            continue
        problems = []
        if facts != case["facts"]:
            problems.append(f"facts {facts} != constructed "
                            f"{case['facts']}")
        if decision.verdict.value != case["expected_verdict"]:
            problems.append(f"verdict {decision.verdict.value} != "
                            f"expected {case['expected_verdict']}")
        if case["expected_verdict"] == "split" and (
                decision.split_release_cents
                != case["expected_split_release_cents"]):
            problems.append(f"split cents "
                            f"{decision.split_release_cents} != "
                            f"{case['expected_split_release_cents']}")
        if problems:
            detail = "; ".join(problems)
            if case["id"] in PROBES:
                findings.append({
                    "id": case["id"], "category": case["category"],
                    "expected": case["expected_verdict"],
                    "actual": decision.verdict.value,
                    "detail": detail, "triage": PROBES[case["id"]]})
                rejected.append((case, f"probe mismatch (finding): "
                                       f"{detail}"))
                continue
            print(f"GENERATOR-EXPECTATION BUG in {case['id']}: "
                  f"{detail}\nFix the generator spec; refusing to "
                  f"write a corpus that enshrines a mismatch.")
            return 1
        seen.append(sig)
        admitted.append(case)

    payload = {
        "meta": {
            "corpus": "A5 golden-corpus growth set",
            "generated": "2026-10-24",
            "seed": SEED,
            "generator": "scripts/generate_corpus.py",
            "case_count": len(admitted),
            "copyright": "Copyright 2026 Brian Acosta",
            "license": "Apache License 2.0",
            "screening": {
                "generated": len(cases),
                "admitted": len(admitted),
                "rejected": [
                    {"id": c["id"], "reason": why}
                    for c, why in rejected],
                "findings": findings,
            },
        },
        "cases": admitted,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    by_cat, by_verdict = {}, {}
    for c in admitted:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        by_verdict[c["expected_verdict"]] = (
            by_verdict.get(c["expected_verdict"], 0) + 1)
    print(f"generated {len(cases)} cases; admitted {len(admitted)}; "
          f"rejected {len(rejected)}")
    print(f"by category: {by_cat}")
    print(f"by expected verdict: {by_verdict}")
    for c, why in rejected:
        print(f"  rejected {c['id']}: {why}")
    for f in findings:
        print(f"  FINDING {f['id']}: expected {f['expected']}, "
              f"system returned {f['actual']} — excluded from corpus")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
