#!/usr/bin/env python3
"""Complexity-ladder regression gate (improvement plan S1).

The ladder is the measurement that set Laya's role; it is now the
regression gate for the forest. Run it on ANY change to
src/laya_questions.py or src/laya_forest.py:

    ~/laya-scratch-venv/bin/python scripts/ladder_gate.py [--min 0.947]

It copies the ladder harness (goals/paypal-hackathon-escrow-agent/
files/laya-complexity-ladder-2026-10-23.py) into a scratch dir — so
the frozen 2026-10-23 baseline artifacts are never overwritten —
runs it against the CURRENT repo code with live Laya, and compares
pooled per-question accuracy on determinate facts against the
2026-10-23 baseline of 94.7% (540/570). Exit 0 = no regression,
1 = regression (or the run failed). Risk-tree errors should also be
triaged against the documented blind spots (ladder report §3-4):
mixed-valence both-NO, numeric-line contamination, UNKNOWN yes-lean.

Deliberately NOT part of the pytest suite: it needs live model
inference (minutes, not seconds) and its job is gating changes to
the forest, not gating every commit.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HARNESS = (Path.home() / "workspace/goals/paypal-hackathon-escrow-agent"
           / "files" / "laya-complexity-ladder-2026-10-23.py")
VENV_PY = Path.home() / "laya-scratch-venv" / "bin" / "python"
BASELINE = 0.947  # pooled accuracy, 2026-10-23 ladder v2 (540/570)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min", type=float, default=BASELINE,
                    help="minimum pooled accuracy to pass "
                         f"(default: {BASELINE}, the 2026-10-23 baseline)")
    args = ap.parse_args()
    if not HARNESS.exists():
        print(f"gate: harness not found at {HARNESS}")
        return 1
    with tempfile.TemporaryDirectory(prefix="ladder-gate-") as td:
        work = Path(td)
        shutil.copy(HARNESS, work / HARNESS.name)
        proc = subprocess.run(
            [str(VENV_PY), HARNESS.name], cwd=work,
            capture_output=True, text=True)
        results_path = work / "laya-complexity-ladder-2026-10-23-results.json"
        if proc.returncode != 0 or not results_path.exists():
            print("gate: ladder run FAILED")
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
            return 1
        data = json.loads(results_path.read_text())
    cases = data["cases"]
    correct = sum(c["det_correct"] for c in cases)
    total = sum(c["det_total"] for c in cases)
    pooled = correct / total if total else 0.0
    by_dim = {}
    for c in cases:
        d = by_dim.setdefault(c["dim"], [0, 0])
        d[0] += c["det_correct"]
        d[1] += c["det_total"]
    print(f"ladder gate: pooled accuracy {pooled:.4f} "
          f"({correct}/{total}) across {len(cases)} cases")
    for dim, (c, t) in sorted(by_dim.items()):
        print(f"  {dim:12} {c}/{t} = {c / t:.3f}" if t else
              f"  {dim:12} n/a")
    if pooled < args.min:
        print(f"gate: REGRESSION — pooled {pooled:.4f} < {args.min}")
        return 1
    print(f"gate: PASS — no regression vs {args.min} baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
