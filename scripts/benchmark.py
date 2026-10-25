#!/usr/bin/env python3
"""Held-out benchmark runner (workstream A4, frozen 2026-10-24).

Copyright 2026 Brian Acosta. Licensed under the Apache License,
Version 2.0 (see LICENSE in the repo root).

Measures the CURRENT build against the frozen held-out set in
eval/benchmark_cases.json — the before/after yardstick for later
architecture waves. Two measurements per run:

  1. Verdict accuracy: every case flows through the REAL decision
     path — gather_evidence -> run_checklist -> adjudicate (the
     same three calls DealManager.monitor makes) — and the verdict
     is compared with the case's expected verdict. The only stub is
     the reserved Grok tier, disabled exactly as the test suite's
     no_grok fixture disables it (its unavailability path,
     INVESTIGATE, is documented product behaviour, and the account
     is out of credits). The Laya shadow forest runs for real; it
     cannot change a verdict while Grok is off.
  2. Sensor accuracy, mirroring the complexity ladder's method:
     the forest's trees are evaluated through the production
     invocation (LayaForest.evaluate_tree on the canonical
     state_block rendered from the case's ground-truth facts), and
     each atomic question is scored only on determinate facts
     (truth YES/NO; UNKNOWN facts are not scored). The aggregated
     forest verdict is also compared with an oracle — aggregate()
     fed the TRUE answers — so verdict divergence is attributable
     to the model's reading, not the aggregation logic.

The cases file's SHA-256 is verified against
eval/benchmark_cases.sha256 before anything runs, and the hash is
recorded in the results so any later run can prove it used the
identical frozen set.

    ~/laya-scratch-venv/bin/python scripts/benchmark.py
        [--cases eval/benchmark_cases.json]
        [--out eval/benchmark_results.json] [--repeats 1]

Exit 0 = run completed (accuracy is reported, not gated — gating
is the ladder gate's job). Exit 2 = frozen-set verification or
case-integrity failure.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import decision_engine, laya_questions as lq  # noqa: E402
from src.checklist import run_checklist  # noqa: E402
from src.disputes import submit_evidence  # noqa: E402
from src.evidence import gather_evidence  # noqa: E402
from src.laya_forest import _get_forest, aggregate, build_state  # noqa: E402
from src.models import Deal, DealTerms  # noqa: E402

DEFAULT_CASES = REPO / "eval" / "benchmark_cases.json"
DEFAULT_OUT = REPO / "eval" / "benchmark_results.json"


def _grok_unavailable(deal, results):
    raise ImportError("Grok disabled for benchmark runs (no_grok)")


def materialize(case: dict):
    """Same mapping the generator self-checked with: stored case ->
    (deal, evidence, results) through the real evidence path.
    Seller rate and authorized amount are pinned per case at the
    evidence level (production fills them from a simulator RNG and
    the PayPal authorization read-back respectively; the golden
    corpus feeds amount cases at this level for the same reason)."""
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
        deal.dispute_filed = True
        for rec in case["dispute_evidence"]:
            submit_evidence(deal, rec["party"], rec["kind"],
                            text=rec.get("text", ""),
                            data=dict(rec.get("data") or {}))
    deal.dispute_filed = bool(case.get("dispute_filed"))
    evidence = gather_evidence(deal, case["tracking"],
                               case["days_since_funding"],
                               case.get("messages"))
    evidence["seller_completion_rate"] = case["seller_completion_rate"]
    if case.get("authorized_cents") is not None:
        evidence["authorized_cents"] = case["authorized_cents"]
    return deal, evidence, run_checklist(deal, evidence)


def oracle_aggregate(facts: dict) -> dict:
    """aggregate() fed the TRUE answers (ladder method)."""
    outs = []
    for tree, qids in lq.TREE_QUESTIONS.items():
        answers = {qid: facts[lq.QUESTIONS[qid].fact].lower()
                   for qid in qids}
        f = {k: facts[k].lower() for k in lq.facts_for_tree(tree)}
        outs.append({"tree": tree, "_facts": f, **answers})
    agg = aggregate(outs)
    return {"verdict": agg["verdict"].value,
            "votes": {k: v.value for k, v in agg["votes"].items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--repeats", type=int, default=1,
                    help="sensor-evaluation repeats per case "
                         "(default 1; the ladder used 3)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the frozen-set hash check (not "
                         "recommended: results then prove nothing "
                         "about which cases were measured)")
    args = ap.parse_args()

    raw = args.cases.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    sidecar = args.cases.parent / (args.cases.stem + ".sha256")
    if not args.no_verify:
        if not sidecar.exists():
            print(f"benchmark: no sidecar hash at {sidecar}; "
                  "refusing to run an unverifiable set")
            return 2
        expected = sidecar.read_text().split()[0]
        if digest != expected:
            print(f"benchmark: HASH MISMATCH — cases file sha256 "
                  f"{digest} != frozen {expected}. The held-out "
                  "set has changed; results would not be comparable "
                  "to the baseline.")
            return 2
    payload = json.loads(raw)
    cases = payload["cases"]

    # The reserved Grok tier is off for every run (see docstring).
    decision_engine._tier2_grok = _grok_unavailable
    forest = _get_forest()  # production singleton; one model load

    try:
        build_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO,
            capture_output=True, text=True).stdout.strip() or None
    except Exception:
        build_commit = None

    started = time.time()
    per_case = []
    fact_mismatches = []
    verdict_correct = 0
    by_class: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    split_cents_correct = 0
    split_cents_total = 0

    # Sensor tallies: pooled, per question id, per tree.question.
    pooled = [0, 0]
    per_question: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_tree_question: dict[str, list[int]] = defaultdict(
        lambda: [0, 0])

    for case in cases:
        deal, _ev, results = materialize(case)
        truth_facts = case["facts"]

        # Integrity: the facts the current build derives must match
        # the frozen ground truth. A mismatch after an architecture
        # change is itself a finding — it is recorded, and scoring
        # still uses the frozen truth (ladder method: blocks are
        # rendered from the truth facts).
        derived_facts = build_state(deal, results)
        if derived_facts != truth_facts:
            fact_mismatches.append({
                "id": case["id"], "stored": truth_facts,
                "derived": derived_facts})

        # --- 1. verdict through the real decision path ---
        decision = decision_engine.adjudicate(deal, results)
        got = decision.verdict.value
        want = case["expected_verdict"]
        ok = got == want
        verdict_correct += ok
        by_class[want][0] += ok
        by_class[want][1] += 1
        confusion[want][got] += 1
        split_ok = None
        if want == "split":
            split_cents_total += 1
            split_ok = (decision.split_release_cents
                        == case.get("expected_split_release_cents"))
            split_cents_correct += bool(split_ok)

        # --- 2. sensor: per-question accuracy on determinate facts ---
        det_correct = 0
        det_total = 0
        rep_records = []
        for rep in range(args.repeats):
            outs = []
            answers_log = {}
            for tree in lq.TREE_QUESTIONS:
                out = forest.evaluate_tree(
                    tree, lq.state_block(truth_facts, tree),
                    facts=truth_facts)
                outs.append(out)
                for qid in lq.TREE_QUESTIONS[tree]:
                    ans = out.get(qid, "unknown")
                    truth = truth_facts[lq.QUESTIONS[qid].fact]
                    answers_log[f"{tree}.{qid}"] = ans
                    if truth in ("YES", "NO"):
                        good = ans == truth.lower()
                        det_total += 1
                        det_correct += good
                        pooled[0] += good
                        pooled[1] += 1
                        per_question[qid][0] += good
                        per_question[qid][1] += 1
                        key = f"{tree}.{qid}"
                        per_tree_question[key][0] += good
                        per_tree_question[key][1] += 1
            agg = aggregate(outs)
            rep_records.append({
                "rep": rep, "answers": answers_log,
                "forest_verdict": agg["verdict"].value,
                "forest_votes": {k: v.value
                                 for k, v in agg["votes"].items()},
            })
        oracle = oracle_aggregate(truth_facts)
        per_case.append({
            "id": case["id"], "category": case["category"],
            "expected_verdict": want, "verdict": got,
            "verdict_correct": ok, "tier": decision.tier,
            "split_release_cents": decision.split_release_cents,
            "split_cents_correct": split_ok,
            "shadow_verdict": decision.shadow_verdict,
            "facts": truth_facts,
            "det_correct": det_correct, "det_total": det_total,
            "det_accuracy": (det_correct / det_total
                             if det_total else None),
            "oracle": oracle, "sensor_repeats": rep_records,
        })
        print(f"{case['id']:42} verdict {got:11} "
              f"(expected {want:11}) {'OK ' if ok else 'MISS'} "
              f"sensor {det_correct}/{det_total}", flush=True)

    n = len(cases)
    verdict_summary = {
        "overall": {"correct": verdict_correct, "total": n,
                    "accuracy": verdict_correct / n if n else None},
        "by_expected_class": {
            k: {"correct": c, "total": t, "accuracy": c / t if t else None}
            for k, (c, t) in sorted(by_class.items())},
        "confusion": {k: dict(v) for k, v in sorted(confusion.items())},
        "split_cents": {"correct": split_cents_correct,
                        "total": split_cents_total},
    }
    sensor_summary = {
        "pooled": {"correct": pooled[0], "total": pooled[1],
                   "accuracy": pooled[0] / pooled[1] if pooled[1] else None},
        "per_question": {
            k: {"correct": c, "total": t,
                "accuracy": c / t if t else None}
            for k, (c, t) in sorted(per_question.items())},
        "per_tree_question": {
            k: {"correct": c, "total": t,
                "accuracy": c / t if t else None}
            for k, (c, t) in sorted(per_tree_question.items())},
        "repeats": args.repeats,
    }
    results_doc = {
        "meta": {
            "benchmark": payload["meta"],
            "cases_file": str(args.cases),
            "cases_sha256": digest,
            "hash_verified": not args.no_verify,
            "build_commit": build_commit,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_s": round(time.time() - started, 1),
            "runner": "scripts/benchmark.py",
        },
        "verdict_summary": verdict_summary,
        "sensor_summary": sensor_summary,
        "fact_mismatches": fact_mismatches,
        "cases": per_case,
    }
    args.out.write_text(json.dumps(results_doc, indent=2) + "\n")

    print()
    print(f"benchmark: {n} cases, sha256 {digest}")
    print(f"verdict accuracy: {verdict_correct}/{n} = "
          f"{verdict_correct / n:.4f}")
    for k, (c, t) in sorted(by_class.items()):
        print(f"  {k:12} {c}/{t} = {c / t:.4f}")
    if split_cents_total:
        print(f"  split cents exact: {split_cents_correct}/"
              f"{split_cents_total}")
    print(f"sensor pooled accuracy: {pooled[0]}/{pooled[1]} = "
          f"{pooled[0] / pooled[1]:.4f} (determinate facts only, "
          f"{args.repeats} repeat(s))")
    for qid, (c, t) in sorted(per_question.items()):
        print(f"  {qid:20} {c}/{t} = {c / t:.4f}")
    if fact_mismatches:
        print(f"WARNING: {len(fact_mismatches)} case(s) where the "
              "build's derived facts differ from the frozen ground "
              "truth (recorded in results)")
    print(f"results -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
