"""Laya decision forest for escrow adjudication.

A forest of small decision networks with the same repeating structure,
all aimed at one goal: release or return the funds.

Prompt engineering findings (2026-10-09, 25 combos tested):
- WINNER: simple "KEY: VALUE." state + choice-type questions = perfect
  YES/NO separation. noul underperforms; checklist narrative fails.
- Trees answer factual choice questions from their angle; a deterministic
  aggregator turns the trees' answers into a verdict.

Layer 1 — trees (sensors). The questions are canonical in
  src/laya_questions.py (the atomic decomposition): every question
  reads exactly ONE case fact and asks a single YES/NO. Compound
  judgments are never posed to the model — "did the seller fail?" is
  derived from the shipped/window answers in _tree_vote, in code.
  - fulfillment: shipped? delivered? delivered to the agreed ZIP?
  - condition: condition free of reported damage? dispute filed?
  - risk: shipped? still within the window? amount matched?
    seller's completion history good?
Layer 2 — deterministic aggregation (aggregate()): each tree casts one
  vote from its answers; majority wins, no majority -> INVESTIGATE.
  The old model meta-decider was REMOVED 2026-10-23 (Ian: Laya answers
  atomic questions only — a verdict is not an atomic question, so the
  model is never asked one, not even behind a flag).

SHADOW MODE (2026-10-23, Ian's call — Option A): the forest failed the
full adjudication checklist when it acted as judge (wrong verdicts on
all three scenarios, 2026-10-09), so it has been demoted from judge to
sensor. Its verdict is computed and written to the audit log next to
the real decision, but it never decides: disagreement with the real
decision routes to INVESTIGATE, never an override. See
decision_engine.adjudicate().

Local (421M params, Apache 2.0). Inference cost: $0.
"""
from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from .laya_questions import (TREE_QUESTIONS, facts_for_tree,
                              questions_for_tree, state_block)
from .models import CheckResult, CheckStatus, Deal, Decision, Verdict

MODEL = "typed-decisions"  # fine-tuned checkpoint; base English can't do this

# The questions each tree puts to Laya — built from the canonical
# atomic decomposition (src/laya_questions.py). Nothing outside that
# module may define a question: atomic single-fact asks only.
TREES: dict[str, dict] = {tree: questions_for_tree(tree)
                          for tree in TREE_QUESTIONS}


def build_state(deal: Deal, results: list[CheckResult]) -> dict:
    """The case facts every question reads. Rendered for the model as
    simple KEY: VALUE. lines by laya_questions.state_block()."""
    by_name = {r.name: r for r in results}
    def yn(name: str, pass_means_yes: bool = True) -> str:
        r = by_name.get(name)
        if not r or r.status == CheckStatus.UNKNOWN:
            return "UNKNOWN"
        is_pass = r.status == CheckStatus.PASS
        return "YES" if (is_pass == pass_means_yes) else "NO"
    return {
        "SHIPPED": yn("pickup_confirmed"),
        "DELIVERED": yn("delivered"),
        "ADDRESS_MATCH": yn("address_match"),
        "WITHIN_WINDOW": yn("within_window"),
        "AMOUNT_MATCH": yn("amount_match"),
        "CONDITION_OK": yn("condition_clear"),
        "BUYER_COMPLAINT": yn("no_dispute", pass_means_yes=False),
        "SELLER_HISTORY_GOOD": yn("seller_history"),
    }


def tree_state(facts: dict, keys: list[str]) -> str:
    """Each tree sees only the facts relevant to its questions."""
    return "\n".join(f"{k}: {facts[k]}." for k in keys)

TREE_FACTS = {tree: facts_for_tree(tree) for tree in TREE_QUESTIONS}


def _tree_vote(tree: str, answers: dict) -> Verdict:
    """One deterministic vote per tree, from its own answers only.

    Fact-certainty gating: the router's choice format forces a YES/NO
    answer even when the underlying fact is UNKNOWN — a guess, not a
    reading. When the tree output carries its fact values ("_facts",
    attached by evaluate_tree), a vote that would rest on a guessed
    answer is gated to INVESTIGATE: RELEASE/REFUND require the
    decisive facts to be known and to back the vote. Outputs without
    "_facts" (older fixtures/tests) vote on answers alone.
    """
    a = {k: v for k, v in answers.items() if k != "tree"}
    facts = a.pop("_facts", None)

    def known(fact: str, value: str) -> bool:
        """True if gating is off, or the fact is known to be `value`."""
        return facts is None or facts.get(fact) == value

    if tree == "fulfillment":
        if a.get("shipped") == "no" and known("SHIPPED", "no"):
            return Verdict.REFUND
        if (a.get("shipped") == "yes" and a.get("delivered") == "yes"
                and a.get("address_match") != "no"
                and known("SHIPPED", "yes")
                and known("DELIVERED", "yes")):
            return Verdict.RELEASE
        return Verdict.INVESTIGATE
    if tree == "condition":
        if (a.get("condition_ok") == "yes" and a.get("complaint") == "no"
                and known("CONDITION_OK", "yes")):
            return Verdict.RELEASE
        return Verdict.INVESTIGATE
    if tree == "risk":
        # Seller failure is DERIVED here, in code — it is a compound
        # judgment (never shipped OR window blown), so it is never
        # asked. Legacy answer keys from the pre-decomposition
        # question set are still honoured so old fixtures/tests vote
        # identically.
        shipped = a.get("shipped")
        if shipped is None:
            shipped = {"yes": "no", "no": "yes"}.get(
                a.get("seller_failed"), "unknown")
        within = a.get("within_window")
        if within is None:
            within = {"yes": "no", "no": "yes"}.get(
                a.get("window_breached"), "unknown")
        if ((shipped == "no" and known("SHIPPED", "no"))
                or (within == "no" and known("WITHIN_WINDOW", "no"))):
            return Verdict.REFUND
        if (a.get("amount_match") == "no"
                or a.get("seller_history_good") == "no"):
            return Verdict.INVESTIGATE
        if (shipped == "yes" and within == "yes"
                and known("SHIPPED", "yes")
                and known("WITHIN_WINDOW", "yes")):
            return Verdict.RELEASE
        return Verdict.INVESTIGATE
    return Verdict.INVESTIGATE


def aggregate(tree_outputs: list[dict]) -> dict:
    """Deterministic aggregation of the trees' answers.

    Replaces the model meta-decider as the verdict path. Each tree casts
    one vote (_tree_vote, with fact-certainty gating: a vote may not
    rest on a guessed answer to an UNKNOWN fact); a majority (>= 2 of
    3) wins, a three-way split is INVESTIGATE. Confidence is the
    tree-agreement fraction — the share of trees backing the verdict —
    replacing the old hard-coded 0.7.

    Returns {"verdict": Verdict, "confidence": float,
             "votes": {tree_name: Verdict}}.
    """
    votes = {t["tree"]: _tree_vote(t["tree"], t) for t in tree_outputs}
    if not votes:
        return {"verdict": Verdict.INVESTIGATE, "confidence": 0.0,
                "votes": votes}
    counts = Counter(votes.values())
    verdict, top = counts.most_common(1)[0]
    if top < 2:  # no majority: every tree leaned a different way
        verdict = Verdict.INVESTIGATE
        top = counts[Verdict.INVESTIGATE]
    return {"verdict": verdict, "confidence": round(top / len(votes), 4),
            "votes": votes}


class ShadowResult(NamedTuple):
    """The forest's shadow output: its (advisory) decision + tree votes."""
    decision: Decision
    votes: dict  # tree name -> Verdict


class LayaForest:
    def __init__(self):
        self._router = None

    @property
    def router(self):
        if self._router is None:
            from laya import Router
            self._router = Router()
        return self._router

    def evaluate_tree(self, name: str, state: str,
                      facts: dict | None = None) -> dict:
        raw = self.router.predict(state, TREES[name], model=MODEL)
        answers = raw.get("answers", {})
        # The fact values behind this tree's state block, carried so
        # _tree_vote can gate votes that would rest on guessed
        # answers to UNKNOWN facts. Display code skips "_" keys.
        # Callers that already hold the ground-truth facts dict (the
        # build_state() output, passed through by shadow()) supply it
        # directly: re-parsing the rendered text is a lossy round-trip
        # that silently misses renamed keys or decorated values
        # (ladder 2026-10-23 §4; fix applied 2026-10-24). The text
        # parse below remains only as a fallback for callers that
        # have nothing but a state block.
        if facts is not None:
            tree_facts = {k: str(facts[k]).strip().lower()
                          for k in TREE_FACTS[name] if k in facts}
        else:
            tree_facts = {}
            for line in state.splitlines():
                k, _, v = line.partition(": ")
                tree_facts[k.strip()] = v.strip().rstrip(".").lower()
        return {"tree": name, "_facts": tree_facts, **{
            q: (answers.get(q, {}).get("choice", "unknown") or "unknown")
            for q in TREES[name]
        }}

    def shadow(self, deal: Deal, results: list[CheckResult]) -> ShadowResult:
        """Run the trees and aggregate. Advisory only (shadow mode): the
        returned decision is the forest's opinion, to be logged next to
        the real decision — never returned as the decision itself.

        The model is only ever asked the atomic questions from
        src/laya_questions.py; the verdict comes from aggregate(),
        deterministic code. There is no model meta-decider anymore.
        """
        facts = build_state(deal, results)
        trees = [self.evaluate_tree(name, state_block(facts, name),
                                    facts=facts)
                 for name in TREES]
        agg = aggregate(trees)
        verdict = agg["verdict"]
        confidence = agg["confidence"]
        how = "deterministic aggregate"
        def shown(t: dict) -> dict:
            return {k: v for k, v in t.items()
                    if k != "tree" and not k.startswith("_")}

        reasoning = ("Laya forest (shadow): " +
                     "; ".join(f"{t['tree']}=" + ",".join(
                         f"{k}:{v}" for k, v in shown(t).items())
                         for t in trees) +
                     f" | votes: " + ", ".join(
                         f"{name}={v.value}"
                         for name, v in agg["votes"].items()) +
                     f" | {how} -> {verdict.value}")
        decision = Decision(
            deal_id=deal.id, verdict=verdict, confidence=confidence,
            reasoning=reasoning,
            key_evidence=[f"{t['tree']}: {shown(t)}" for t in trees],
            tier=1, cost_usd=0.0,
        )
        return ShadowResult(decision=decision, votes=agg["votes"])

    def decide(self, deal: Deal, results: list[CheckResult]) -> Decision:
        return self.shadow(deal, results).decision


_forest: LayaForest | None = None


def _get_forest() -> LayaForest:
    global _forest
    if _forest is None:
        _forest = LayaForest()
    return _forest


def forest_shadow(deal: Deal, results: list[CheckResult]) -> ShadowResult:
    """Shadow-mode entry point: forest opinion + per-tree votes."""
    return _get_forest().shadow(deal, results)


def forest_decide(deal: Deal, results: list[CheckResult]) -> Decision:
    return _get_forest().decide(deal, results)
