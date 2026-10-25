#!/usr/bin/env python3
"""Grade a code delivery against a client requirements list.

Read-only: a local repo is only read (its tests run only if a
tests_pass requirement is present and --no-tests is not given); a
public GitHub URL is only cloned. Prints the evidence summary, the
deterministic grade, and — when run inside a deal context is not
required — the verdict the existing Tier 0 path would reach for a
hypothetical deal, as JSON.

Usage:
    python scripts/code_delivery_grade.py --repo-path PATH \
        --requirements requirements.json [--no-tests]
    python scripts/code_delivery_grade.py \
        --repo-url https://github.com/owner/repo \
        --requirements requirements.json --clone-dir /tmp/clones

Copyright Brian Acosta, Apache 2.0.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.code_delivery import (GitHubEvidenceAdapter,  # noqa: E402
                               adjudicate_code_delivery)
from src.models import Deal, DealTerms  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo-path")
    src.add_argument("--repo-url")
    ap.add_argument("--requirements", required=True,
                    help="JSON file: list of requirement dicts")
    ap.add_argument("--no-tests", action="store_true",
                    help="do not execute tests_pass targets "
                         "(remote / untrusted sampling mode)")
    ap.add_argument("--clone-dir")
    ap.add_argument("--amount-cents", type=int, default=100000)
    args = ap.parse_args()

    requirements = json.loads(Path(args.requirements).read_text())
    adapter = GitHubEvidenceAdapter(
        repo_path=args.repo_path, repo_url=args.repo_url,
        requirements=requirements, run_tests=not args.no_tests,
        clone_dir=args.clone_dir)
    deal = Deal(id="sampling", terms=DealTerms(
        amount_cents=args.amount_cents, currency="USD",
        item_description="code delivery sampling",
        buyer_email="client@example.com",
        seller_email="freelancer@example.com",
        delivery_zip="47172", window_days=14))
    evidence = adapter.gather(deal, {}, 3)
    decision, _ = adjudicate_code_delivery(deal, evidence)
    out = {"evidence": evidence["code_delivery"],
           "verdict": decision.verdict.value,
           "split_release_cents": decision.split_release_cents,
           "key_evidence": decision.key_evidence,
           "reasoning": decision.reasoning}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
