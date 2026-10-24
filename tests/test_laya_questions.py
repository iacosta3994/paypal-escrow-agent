"""Tests for the atomic decomposition (src/laya_questions.py) and its
wiring into the forest. No LLM calls: the Router is stubbed with an
oracle that answers by reading the state block — the behaviour the
2026-10-09 experiments proved this format elicits from Laya.
"""
import re

from src import laya_forest, laya_questions as lq
from src.models import (CheckResult, CheckStatus, Deal, DealTerms,
                        Verdict)


def test_every_question_reads_exactly_one_fact():
    for q in lq.QUESTIONS.values():
        assert q.fact in lq.FACTS
        payload = q.router_payload()
        assert payload["type"] == "choice"
        # The winning criteria wording from the 2026-10-09 experiment.
        assert payload["criteria"] == {"yes": "the evidence says YES",
                                       "no": "the evidence says NO"}
        assert payload["instructions"] == q.text


def test_every_fact_is_asked_by_some_tree():
    for fact, trees in lq.coverage().items():
        assert trees, f"fact {fact} is never asked"


def test_no_compound_or_verdict_questions():
    # Seller failure is a compound judgment — derived in code, never
    # asked. A verdict is never asked either.
    assert "seller_failed" not in lq.QUESTIONS
    assert "window_breached" not in lq.QUESTIONS
    assert "final_verdict" not in lq.QUESTIONS
    for q in lq.QUESTIONS.values():
        text = q.text.lower()
        assert "release" not in text and "refund" not in text
        assert text.count("?") == 1  # one ask per question


def test_meta_decider_is_gone():
    assert not hasattr(laya_forest, "META_QUESTIONS")
    assert not hasattr(laya_forest.LayaForest, "meta_decide")


def test_forest_trees_come_from_the_decomposition():
    assert set(laya_forest.TREES) == set(lq.TREE_QUESTIONS)
    for tree, qids in lq.TREE_QUESTIONS.items():
        assert set(laya_forest.TREES[tree]) == set(qids)
        assert laya_forest.TREE_FACTS[tree] == lq.facts_for_tree(tree)


def test_state_block_is_the_proven_format():
    facts = {f: "YES" for f in lq.FACTS}
    block = lq.state_block(facts, "risk")
    lines = block.splitlines()
    assert [ln.split(":")[0] for ln in lines] == lq.facts_for_tree("risk")
    for line in lines:
        assert re.fullmatch(r"[A-Z_]+: (YES|NO|UNKNOWN)\.", line)
    # The forest's renderer produces the identical block.
    assert (laya_forest.tree_state(facts, laya_forest.TREE_FACTS["risk"])
            == block)


def test_risk_vote_derives_seller_failure_from_atomic_answers():
    trees = [
        {"tree": "fulfillment", "shipped": "no", "delivered": "unknown",
         "address_match": "unknown"},
        {"tree": "condition", "condition_ok": "unknown",
         "complaint": "no"},
        {"tree": "risk", "shipped": "no", "within_window": "yes",
         "amount_match": "yes", "seller_history_good": "yes"},
    ]
    agg = laya_forest.aggregate(trees)
    assert agg["votes"]["risk"] == Verdict.REFUND
    assert agg["verdict"] == Verdict.REFUND


def test_risk_vote_window_expiry_refunds_history_only_investigates():
    base = [
        {"tree": "fulfillment", "shipped": "yes", "delivered": "yes",
         "address_match": "yes"},
        {"tree": "condition", "condition_ok": "unknown",
         "complaint": "no"},
    ]
    expired = base + [{"tree": "risk", "shipped": "yes",
                       "within_window": "no", "amount_match": "yes",
                       "seller_history_good": "yes"}]
    assert laya_forest.aggregate(expired)["votes"]["risk"] == Verdict.REFUND
    bad_history = base + [{"tree": "risk", "shipped": "yes",
                           "within_window": "yes", "amount_match": "yes",
                           "seller_history_good": "no"}]
    agg = laya_forest.aggregate(bad_history)
    assert agg["votes"]["risk"] == Verdict.INVESTIGATE
    assert agg["votes"]["fulfillment"] == Verdict.RELEASE


def test_gating_blocks_release_resting_on_unknown_facts():
    # Mid-flight: delivered/address facts are UNKNOWN; yes answers to
    # them are forced guesses and must not carry a RELEASE vote.
    trees = [
        {"tree": "fulfillment", "shipped": "yes", "delivered": "yes",
         "address_match": "yes",
         "_facts": {"SHIPPED": "yes", "DELIVERED": "unknown",
                    "ADDRESS_MATCH": "unknown"}},
        {"tree": "condition", "condition_ok": "yes", "complaint": "no",
         "_facts": {"CONDITION_OK": "unknown", "BUYER_COMPLAINT": "no"}},
        {"tree": "risk", "shipped": "yes", "within_window": "yes",
         "amount_match": "yes", "seller_history_good": "yes",
         "_facts": {"SHIPPED": "yes", "WITHIN_WINDOW": "yes",
                    "AMOUNT_MATCH": "yes", "SELLER_HISTORY_GOOD": "yes"}},
    ]
    agg = laya_forest.aggregate(trees)
    assert agg["votes"]["fulfillment"] == Verdict.INVESTIGATE
    assert agg["votes"]["condition"] == Verdict.INVESTIGATE
    assert agg["votes"]["risk"] == Verdict.RELEASE
    assert agg["verdict"] == Verdict.INVESTIGATE


def test_gating_lets_known_refund_stand():
    # SHIPPED is known NO; the REFUND vote stands even though the
    # delivered/address facts are unknown — they are not decisive.
    trees = [
        {"tree": "fulfillment", "shipped": "no", "delivered": "yes",
         "address_match": "yes",
         "_facts": {"SHIPPED": "no", "DELIVERED": "unknown",
                    "ADDRESS_MATCH": "unknown"}},
        {"tree": "condition", "condition_ok": "unknown",
         "complaint": "no",
         "_facts": {"CONDITION_OK": "unknown", "BUYER_COMPLAINT": "no"}},
        {"tree": "risk", "shipped": "no", "within_window": "yes",
         "amount_match": "yes", "seller_history_good": "yes",
         "_facts": {"SHIPPED": "no", "WITHIN_WINDOW": "yes",
                    "AMOUNT_MATCH": "yes", "SELLER_HISTORY_GOOD": "yes"}},
    ]
    agg = laya_forest.aggregate(trees)
    assert agg["votes"]["fulfillment"] == Verdict.REFUND
    assert agg["votes"]["risk"] == Verdict.REFUND
    assert agg["verdict"] == Verdict.REFUND


def test_gating_condition_release_needs_known_condition():
    def run(cond_fact):
        trees = [
            {"tree": "fulfillment", "shipped": "yes", "delivered": "yes",
             "address_match": "yes"},
            {"tree": "condition", "condition_ok": "yes",
             "complaint": "no",
             "_facts": {"CONDITION_OK": cond_fact,
                        "BUYER_COMPLAINT": "no"}},
            {"tree": "risk", "shipped": "yes", "within_window": "yes",
             "amount_match": "yes", "seller_history_good": "yes"},
        ]
        return laya_forest.aggregate(trees)["votes"]["condition"]

    assert run("yes") == Verdict.RELEASE
    assert run("unknown") == Verdict.INVESTIGATE


class _OracleRouter:
    """Answers each question by reading its fact line in the state."""

    def predict(self, state, questions, model=None):
        facts = {}
        for line in state.splitlines():
            k, v = line.split(": ")
            facts[k.strip()] = v.strip().rstrip(".").lower()
        return {"answers": {
            qid: {"choice": facts.get(lq.QUESTIONS[qid].fact, "unknown")}
            for qid in questions}}


def _deal() -> Deal:
    return Deal(id="t", terms=DealTerms(
        amount_cents=100, currency="USD", item_description="x",
        buyer_email="b@x", seller_email="s@x", delivery_zip="47172"))


def _results(**statuses):
    return [CheckResult(name, status)
            for name, status in statuses.items()]


def _forest() -> laya_forest.LayaForest:
    forest = laya_forest.LayaForest()
    forest._router = _OracleRouter()
    return forest


def test_shadow_end_to_end_never_ships_late():
    results = _results(
        tracking_exists=CheckStatus.PASS,
        pickup_confirmed=CheckStatus.FAIL,
        in_transit=CheckStatus.FAIL,
        delivered=CheckStatus.UNKNOWN,
        address_match=CheckStatus.UNKNOWN,
        within_window=CheckStatus.PASS,
        amount_match=CheckStatus.PASS,
        no_dispute=CheckStatus.PASS,
        condition_clear=CheckStatus.UNKNOWN,
        seller_history=CheckStatus.PASS)
    shadow = _forest().shadow(_deal(), results)
    assert shadow.decision.verdict == Verdict.REFUND
    assert shadow.votes["fulfillment"] == Verdict.REFUND
    assert shadow.votes["risk"] == Verdict.REFUND


def test_shadow_end_to_end_mid_flight():
    results = _results(
        tracking_exists=CheckStatus.PASS,
        pickup_confirmed=CheckStatus.PASS,
        in_transit=CheckStatus.PASS,
        delivered=CheckStatus.UNKNOWN,
        address_match=CheckStatus.UNKNOWN,
        within_window=CheckStatus.PASS,
        amount_match=CheckStatus.PASS,
        no_dispute=CheckStatus.PASS,
        condition_clear=CheckStatus.UNKNOWN,
        seller_history=CheckStatus.PASS)
    shadow = _forest().shadow(_deal(), results)
    assert shadow.decision.verdict == Verdict.INVESTIGATE
    assert shadow.votes["risk"] == Verdict.RELEASE
    assert shadow.votes["fulfillment"] == Verdict.INVESTIGATE


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
