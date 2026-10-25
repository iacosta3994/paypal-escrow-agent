# Held-out benchmark (FROZEN 2026-10-24)

Copyright 2026 Brian Acosta. Licensed under the Apache License 2.0.

Workstream A4 of the improvement plan: a held-out yardstick for
before/after comparison across architecture changes. The cases here
were generated once, measured against the build that froze them,
and must not change afterwards.

## Files

- `benchmark_cases.json` — the frozen case set: 48 deals, each with
  construction inputs (item, amount, ZIP, carrier, tracking shape,
  dispute state, seller rate), an expected verdict, the expected
  split amount where applicable, and the eight determinate
  ground-truth facts (the `build_state()` vocabulary) used for
  per-question sensor scoring. Cases were deliberately built to not
  duplicate or near-duplicate anything in `tests/test_golden_corpus.py`,
  `tests/test_decisions.py`, or the 2026-10-23 complexity ladder:
  different amounts, items, ZIPs, carrier strings, and fact
  combinations.
- `benchmark_cases.sha256` — SHA-256 of `benchmark_cases.json`:
  `10ddbe90ad8f3c7bf1fb50884af42d19845f713a3f1bc78dcf9a9001cfd47588`
- `generate_benchmark.py` — the seeded generator (seed 20261024).
  It self-checks every case through the real decision path before
  writing, reproduces the frozen file byte-for-byte, and refuses to
  overwrite a differing frozen set without `--force`.
- `benchmark_results.json` — output of the most recent runner run.
- `benchmark_baseline_2026-10-24.json` — the baseline measurement
  against the build that froze the set (commit recorded inside).
- `../scripts/benchmark.py` — the runner.

## Rules

- **Do not edit the cases.** Any edit changes the hash, the runner's
  verification fails, and comparisons against the baseline stop
  meaning anything. Regenerating with `--force` is a deliberate act
  that invalidates every prior comparison — treat it as starting a
  new benchmark, not updating this one.
- The runner verifies the hash before measuring and records it in
  every results file, so a later run can prove it used the identical
  set.
- Numbers are reported exactly as measured. Do not tune cases to
  move them.

## Re-running against a changed build

```bash
~/laya-scratch-venv/bin/python scripts/benchmark.py
```

Writes `eval/benchmark_results.json` and prints verdict accuracy
(overall and per expected-verdict class), split-amount exactness,
and per-question sensor accuracy on determinate facts (pooled and
per question), using the same scoring method as the 2026-10-23
complexity ladder.
