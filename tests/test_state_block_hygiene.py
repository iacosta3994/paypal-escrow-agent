"""State-block hygiene gate (improvement plan S2, 2026-10-24).

The complexity ladder (2026-10-23) proved that what breaks the Laya
forest is block ENRICHMENT: extra facts (R1, degrades past ~8 added
facts) and raw numeric detail lines (R4, contaminates neighboring
answers at every size tested). Production blocks are safe only
because they are canonical: exactly the atomic boolean facts from
src/laya_questions.py, one ``KEY: VALUE.`` line each, values in
{YES, NO, UNKNOWN}, numerics living in Tier 0 where they belong.

This test is the gate that keeps them that way. It fails if anyone
enriches the block later — extra facts, numeric detail lines,
decorated values, renamed/lowercased keys, a missing fact. The
negative controls at the bottom prove the gate itself bites: each
enriched variant MUST be rejected.
"""
import re

import pytest

from src import laya_forest, laya_questions as lq
from src.models import CheckResult, CheckStatus, Deal, DealTerms

_LINE = re.compile(r"([A-Z][A-Z_]*): (YES|NO|UNKNOWN)\.")


def assert_canonical_block(block: str, tree: str) -> None:
    """Raise AssertionError unless `block` is exactly the canonical
    render for `tree`: the tree's facts in canonical order, one
    ``KEY: VALUE.`` line each, nothing else."""
    expected = lq.facts_for_tree(tree)
    lines = block.splitlines()
    assert len(lines) == len(expected), (
        f"{tree}: block has {len(lines)} lines, canonical is "
        f"{len(expected)} — facts added or dropped")
    keys = []
    for line in lines:
        m = _LINE.fullmatch(line)
        assert m, f"{tree}: non-canonical line {line!r} — only " \
                  "'KEY: (YES|NO|UNKNOWN).' lines are allowed " \
                  "(no numerics, no decoration, no renamed keys)"
        keys.append(m.group(1))
    assert keys == expected, (
        f"{tree}: block facts {keys} != canonical {expected}")


def _facts(**over) -> dict:
    f = {k: "YES" for k in lq.FACTS}
    f["BUYER_COMPLAINT"] = "NO"
    f.update(over)
    return f


def test_every_tree_block_is_canonical():
    mixes = [
        _facts(),
        _facts(**{k: "UNKNOWN" for k in lq.FACTS}),
        _facts(SHIPPED="NO", WITHIN_WINDOW="NO", DELIVERED="UNKNOWN",
               CONDITION_OK="NO", BUYER_COMPLAINT="YES"),
    ]
    for facts in mixes:
        for tree in lq.TREE_QUESTIONS:
            assert_canonical_block(lq.state_block(facts, tree), tree)


def test_block_universe_is_exactly_the_canonical_facts():
    seen = set()
    for tree in lq.TREE_QUESTIONS:
        seen.update(lq.facts_for_tree(tree))
    assert seen == set(lq.FACTS)


def test_build_state_only_produces_canonical_values():
    """The facts dict the blocks render from holds exactly the 8
    canonical keys, each YES/NO/UNKNOWN — nothing numeric, ever."""
    names = ["pickup_confirmed", "delivered", "address_match",
             "within_window", "amount_match", "condition_clear",
             "no_dispute", "seller_history"]
    deal = Deal(id="hyg", terms=DealTerms(
        amount_cents=100, currency="USD", item_description="x",
        buyer_email="b@x", seller_email="s@x", delivery_zip="47172"))
    for status in CheckStatus:
        results = [CheckResult(n, status) for n in names]
        facts = laya_forest.build_state(deal, results)
        assert set(facts) == set(lq.FACTS)
        assert set(facts.values()) <= {"YES", "NO", "UNKNOWN"}


# --- negative controls: the gate must reject every enrichment ---

def _risk_block() -> str:
    return lq.state_block(_facts(), "risk")


def test_gate_rejects_numeric_detail_line():
    enriched = _risk_block() + "\nAMOUNT_CENTS: 50000."
    with pytest.raises(AssertionError):
        assert_canonical_block(enriched, "risk")


def test_gate_rejects_extra_fact_line():
    enriched = _risk_block() + "\nSELLER_RATING: YES."
    with pytest.raises(AssertionError):
        assert_canonical_block(enriched, "risk")


def test_gate_rejects_decorated_value():
    enriched = _risk_block().replace(
        "SHIPPED: YES.", "SHIPPED: YES (pickup scan 2026-11-02).")
    assert enriched != _risk_block()
    with pytest.raises(AssertionError):
        assert_canonical_block(enriched, "risk")


def test_gate_rejects_lowercase_and_renamed_keys():
    with pytest.raises(AssertionError):
        assert_canonical_block(
            _risk_block().replace("SHIPPED: YES.", "SHIPPED: yes."),
            "risk")
    with pytest.raises(AssertionError):
        assert_canonical_block(
            _risk_block().replace("SHIPPED: YES.", "SENT: YES."),
            "risk")


def test_gate_rejects_missing_fact_line():
    lines = _risk_block().splitlines()
    with pytest.raises(AssertionError):
        assert_canonical_block("\n".join(lines[:-1]), "risk")
