"""Canonical atomic decomposition of an escrow case for the Laya forest.

Ian's direction (2026-10-23): Laya runs correctly only when the problem
is broken down to what she is proven good at. The 2026-10-09 prompt
experiments (25 combos, prompt_experiment.py) found exactly one
reliable shape:

  - state as simple ``KEY: VALUE.`` lines (format v1_simple), and
  - "choice" questions whose criteria are ``the evidence says YES`` /
    ``the evidence says NO`` (q_choice) — perfect YES/NO separation.

She fails at compound questions, evidence-weighing, and verdicts. So
this module is the ONLY source of questions the forest may ask:

  - Every question reads exactly ONE fact from the case state — the
    facts build_state() produces from the checklist results.
  - Every question is a single YES/NO ask in the proven format.
  - Compound judgments are NEVER asked. "Did the seller fail?" is a
    judgment over two facts (never shipped OR window blown), so it is
    derived in deterministic code (laya_forest._tree_vote), not posed
    to the model. The old verdict question (meta-decider) is gone for
    the same reason: aggregation is code, not a model prompt.

Coverage rule: every state fact is asked by at least one tree, so the
forest's picture of a case is complete; each tree sees only the facts
its own questions read.

HYGIENE POLICY (enforced by tests/test_state_block_hygiene.py): the
state block stays canonical — ``KEY: VALUE.`` lines, atomic booleans
only (YES/NO/UNKNOWN). No numeric detail lines, no extra facts, no
decorated values; numerics live in Tier 0. The complexity ladder
showed enrichment is what corrupts the forest's answers (R1: degrade
past ~8 extra facts; R4: raw detail lines contaminate neighbours at
every size) — the hygiene gate fails the suite if a block ever grows
past this shape.

Phrasing is empirical, not stylistic (live probe, 2026-10-23): the
condition question originally read "Was the delivered item reported
free of damage?" and Laya answered YES unconditionally — on
CONDITION_OK: YES, NO, and UNKNOWN alike. The direct restatement
"Is the delivered item's condition OK?" tracks the fact perfectly
(NO -> no, YES -> yes, 3/3 each). Questions here are phrased as
direct restatements of their fact for that reason.

Second phrasing change (complexity ladder, 2026-10-23): the
within_window question originally read "Is this deal still within
the agreed delivery window?" and the risk tree answered YES on
WITHIN_WINDOW: NO in every plain configuration tested (12/12 ladder
reps plus probes) — a stuck-YES sensor. The replacement, "Is the
delivery window still open for this deal?", tracks the fact in the
shipped-late configuration. It is NOT a clean win, and the ladder
report says so: full-ladder error count was unchanged (30/516 each
way) — the new phrasing instead flips YES -> no when the risk block
carries 16 extra facts, and raw detail lines defeat both phrasings.
Kept because its residual failures lean toward INVESTIGATE (safe for
a shadow sensor) while the old phrasing's leaned toward RELEASE, and
because the shipped-late configuration is the most production-like
of the failing set. Residual checkpoint limitation: when SHIPPED and
WITHIN_WINDOW are both NO, every phrasing tried still draws YES.
"""
from __future__ import annotations

from dataclasses import dataclass

# The facts build_state() produces (src/laya_forest.build_state), in
# canonical order. Every question below reads exactly one of these.
FACTS: tuple[str, ...] = (
    "SHIPPED",
    "DELIVERED",
    "ADDRESS_MATCH",
    "WITHIN_WINDOW",
    "AMOUNT_MATCH",
    "CONDITION_OK",
    "BUYER_COMPLAINT",
    "SELLER_HISTORY_GOOD",
)

# The winning criteria wording from the 2026-10-09 experiment
# (v1_simple + q_choice scored 1.0; every richer phrasing scored less).
_CRITERIA = {"yes": "the evidence says YES", "no": "the evidence says NO"}


@dataclass(frozen=True)
class AtomicQuestion:
    """One question, one fact, one YES/NO answer."""

    id: str
    fact: str  # the single state key this question reads
    text: str  # the single-fact ask, in the proven choice format

    def router_payload(self) -> dict:
        return {"type": "choice", "instructions": self.text,
                "criteria": dict(_CRITERIA)}


_QUESTIONS = [
    # fulfillment angle: did the goods move, and arrive at the buyer?
    AtomicQuestion("shipped", "SHIPPED",
                   "Was the item shipped?"),
    AtomicQuestion("delivered", "DELIVERED",
                   "Was the item delivered to the buyer?"),
    AtomicQuestion("address_match", "ADDRESS_MATCH",
                   "Did the delivery ZIP match the agreed delivery ZIP?"),
    # condition angle: did the goods arrive intact, is anyone unhappy?
    AtomicQuestion("condition_ok", "CONDITION_OK",
                   "Is the delivered item's condition OK?"),
    AtomicQuestion("complaint", "BUYER_COMPLAINT",
                   "Did the buyer file a dispute on this deal?"),
    # risk angle: is the deal inside its terms, is the seller sound?
    AtomicQuestion("within_window", "WITHIN_WINDOW",
                   "Is the delivery window still open for this deal?"),
    AtomicQuestion("amount_match", "AMOUNT_MATCH",
                   "Did the authorized amount match the agreed deal amount?"),
    AtomicQuestion("seller_history_good", "SELLER_HISTORY_GOOD",
                   "Is the seller's completion history good?"),
]

QUESTIONS: dict[str, AtomicQuestion] = {q.id: q for q in _QUESTIONS}

# Which questions each tree asks. NOTE: there is deliberately no
# "seller_failed" question — seller failure is a compound judgment
# (SHIPPED + WITHIN_WINDOW) and is derived in
# laya_forest._tree_vote, never asked.
#
# COVERAGE AUDIT (2026-10-24, improvement plan S3 — design revised
# the same day on ladder evidence): every DECISIVE fact gets a
# second reader, but the second reader is NOT another model tree.
# The first design gave five decisive facts a second MODEL reader
# (veto reads); the ladder gate then measured pooled accuracy
# falling 94.7% -> 87.7%, because the added pairings are questions
# this checkpoint answers badly (WITHIN_WINDOW: 0.500 from risk,
# 0.333 from fulfillment; new pairings pooled 0.821 vs 0.923 for the
# originals). Asking a weak sensor more questions made it weaker.
# Model reads therefore stay at the measured-good baseline below,
# and the second read is deterministic: aggregate() runs a RELEASE
# AUDIT over the union of ground-truth facts (laya_forest.
# RELEASE_AUDIT) and downgrades any RELEASE vote that a known-bad
# decisive fact contradicts. Coverage:
#   SHIPPED          fulfillment + risk (models) + audit
#   DELIVERED        fulfillment (model) + audit
#   ADDRESS_MATCH    fulfillment (model) + audit
#   WITHIN_WINDOW    risk (model) + audit
#   CONDITION_OK     condition (model) + audit
#   BUYER_COMPLAINT  condition (model) + audit
#   AMOUNT_MATCH     risk (model) + audit (+ Tier 0 hard fail)
#   SELLER_HISTORY_GOOD risk only — non-decisive by design (no
#     Tier 0 set contains it; it never carries a verdict alone).
TREE_QUESTIONS: dict[str, tuple[str, ...]] = {
    "fulfillment": ("shipped", "delivered", "address_match"),
    "condition": ("condition_ok", "complaint"),
    "risk": ("shipped", "within_window", "amount_match",
             "seller_history_good"),
}


def questions_for_tree(tree: str) -> dict[str, dict]:
    """Router payload for one tree: {question_id: choice question}."""
    return {qid: QUESTIONS[qid].router_payload()
            for qid in TREE_QUESTIONS[tree]}


def facts_for_tree(tree: str) -> list[str]:
    """The state keys one tree's questions read, in canonical order."""
    wanted = {QUESTIONS[qid].fact for qid in TREE_QUESTIONS[tree]}
    return [f for f in FACTS if f in wanted]


def state_block(facts: dict, tree: str) -> str:
    """The proven state format (v1_simple): one ``KEY: VALUE.`` line
    per fact this tree's questions read — nothing else."""
    return "\n".join(f"{k}: {facts[k]}." for k in facts_for_tree(tree))


def coverage() -> dict[str, list[str]]:
    """fact -> trees that ask about it. Every fact must appear."""
    cov: dict[str, list[str]] = {f: [] for f in FACTS}
    for tree, qids in TREE_QUESTIONS.items():
        for qid in qids:
            cov[QUESTIONS[qid].fact].append(tree)
    return cov
