"""The code-deliverable checklist pack.

This is the ONLY new "checks" code a code-deliverable deal needs —
it is a checklist pack in the same sense as src/checklist.py (same
CheckResult shape, same status vocabulary), but it is NOT a new
decision path: its results are fed to the EXISTING
``decision_engine.adjudicate()`` (Tier 0 rules, shadow sensor, Tier 2
routing — untouched), and settlements execute through the EXISTING
DealManager.settle() / split_settlement money path.

How the pack's check NAMES reuse Tier 0's rules (deliberately — the
kernel did not change):

  - ``pickup_confirmed`` FAIL is a Tier 0 hard fail -> REFUND.
    The pack fails it when no repo / an empty repo was delivered, or
    when the measured fraction is below the policy split minimum
    (treated as nothing delivered).
  - ``delivered`` + ``address_match`` + ``amount_match`` +
    ``no_dispute`` all PASS is Tier 0's decisive-pass rule ->
    RELEASE. The pack passes all four when the fraction is at/above
    the policy release threshold (address = the delivered repo IS
    the destination the deal named; the buyer-confirmation analogue
    is not needed).
  - ``delivered`` UNKNOWN carrying ``detail={items_delivered:
    satisfied_weight, items_expected: total_weight}`` is Tier 0's
    pre-existing partial-shipment rule -> SPLIT at
    ``amount_cents * satisfied // total`` — i.e. a split at exactly
    the measured completion fraction, computed by kernel code that
    predates this workstream. The pack uses it when the fraction is
    between the policy split minimum and the release threshold, and
    also passes address/amount/no-dispute (with no condition fail),
    as that rule requires.
  - ``condition_clear`` FAIL (with ``delivered`` present) is Tier
    0's condition-dispute rule -> INVESTIGATE. The pack fails it
    when evidence is ungradeable or an UNKNOWN-weight item is
    decisive (manual-judgment items with no human verdict — see
    grading.py). INVESTIGATE, never a guess.

Every evidence string in this pack carries the integer fraction
(satisfied/total weights and basis points), so the Decision record
Tier 0 produces — reasoning + key_evidence, written to the audit
log — records the measured fraction for every verdict. The full
per-requirement grade rides in the ``code_completion`` check's
``detail`` dict.

Thresholds come from src/policy.py (``code_release_threshold`` /
``code_split_min_fraction``, additive Wave 2 keys with defaults).
"""
from ..decision_engine import adjudicate
from ..models import CheckResult, CheckStatus, Deal
from ..policy import DEFAULT_POLICY, Policy


def _fraction_str(grade: dict) -> str:
    return (f"{grade.get('satisfied_weight', 0)}/"
            f"{grade.get('total_weight', 0)} weighted requirements "
            f"satisfied (fraction "
            f"{grade.get('fraction_basis_points', 0)} basis points = "
            f"{grade.get('fraction_basis_points', 0) / 100:.2f}%)")


def run_code_checklist(deal: Deal, evidence: dict,
                       policy: Policy | None = None) -> list[CheckResult]:
    policy = policy or DEFAULT_POLICY
    cd = evidence.get("code_delivery") or {}
    grade = cd.get("grade") or {}
    total = grade.get("total_weight", 0)
    satisfied = grade.get("satisfied_weight", 0)
    bp = grade.get("fraction_basis_points", 0)
    rel_bp = round(policy.code_release_threshold * 10000)
    min_bp = round(policy.code_split_min_fraction * 10000)
    present = bool(cd.get("present"))
    empty = bool(cd.get("empty"))
    ungradeable = bool(grade.get("ungradeable_reason"))
    frac = _fraction_str(grade)
    results: list[CheckResult] = []

    # pickup_confirmed — the "seller performed at all" fact.
    if not present:
        reason = cd.get("reason") or "not found"
        results.append(CheckResult(
            "pickup_confirmed", CheckStatus.FAIL,
            f"no code delivery: repo missing ({reason}); {frac}", 1.5))
    elif empty:
        results.append(CheckResult(
            "pickup_confirmed", CheckStatus.FAIL,
            f"no code delivery: repo is empty; {frac}", 1.5))
    elif not ungradeable and bp < min_bp:
        results.append(CheckResult(
            "pickup_confirmed", CheckStatus.FAIL,
            f"code delivery below the policy split minimum "
            f"({min_bp} basis points): {frac} — treated as nothing "
            f"delivered", 1.5))
    else:
        results.append(CheckResult(
            "pickup_confirmed", CheckStatus.PASS,
            f"repo delivered from {cd.get('source')}: "
            f"{cd.get('file_count', 0)} files, "
            f"{cd.get('commit_count')} commits; {frac}", 1.2))

    # delivered — complete / partial / delivered-but-ungradeable.
    if ungradeable and present and not empty:
        delivered = CheckResult(
            "delivered", CheckStatus.PASS,
            f"repo delivered but grading is blocked: "
            f"{grade.get('ungradeable_reason')}; {frac}", 2.0)
    elif bp >= rel_bp and present and not empty:
        delivered = CheckResult(
            "delivered", CheckStatus.PASS,
            f"code delivery complete: {frac} (release threshold "
            f"{rel_bp} basis points met)", 2.0)
    elif bp >= min_bp and present and not empty:
        delivered = CheckResult(
            "delivered", CheckStatus.UNKNOWN,
            f"partial code delivery: {frac}", 0.9,
            detail={"items_delivered": satisfied,
                    "items_expected": total})
    else:
        delivered = CheckResult(
            "delivered", CheckStatus.UNKNOWN,
            f"no complete code delivery: {frac}", 0.5)
    results.append(delivered)

    # address_match — the delivered repo is the deal's destination.
    if present and not empty:
        results.append(CheckResult(
            "address_match", CheckStatus.PASS,
            f"delivery destination matches the deal: repo "
            f"{cd.get('source')}", 1.5))
    else:
        results.append(CheckResult(
            "address_match", CheckStatus.UNKNOWN,
            "no repo delivered to match against the deal", 0.5))

    # within_window / amount_match / no_dispute — same semantics as
    # the physical pack, read from the standard evidence keys.
    days = evidence.get("days_since_funding", 0)
    if days <= deal.terms.window_days:
        results.append(CheckResult(
            "within_window", CheckStatus.PASS,
            f"day {days} of {deal.terms.window_days}", 0.8))
    else:
        results.append(CheckResult(
            "within_window", CheckStatus.FAIL,
            f"day {days} exceeds {deal.terms.window_days}-day window",
            1.4))
    if evidence.get("authorized_cents") == deal.terms.amount_cents:
        results.append(CheckResult(
            "amount_match", CheckStatus.PASS,
            "authorized == deal amount", 1.0))
    else:
        results.append(CheckResult(
            "amount_match", CheckStatus.FAIL,
            "authorized != deal amount", 1.6))
    if evidence.get("dispute_filed"):
        results.append(CheckResult(
            "no_dispute", CheckStatus.FAIL,
            "dispute filed by a party", 1.7))
    else:
        results.append(CheckResult(
            "no_dispute", CheckStatus.PASS, "no disputes", 0.7))

    # condition_clear — the INVESTIGATE lever for ungradeable cases.
    if ungradeable:
        results.append(CheckResult(
            "condition_clear", CheckStatus.FAIL,
            f"ungradeable or conflicting code evidence: "
            f"{grade.get('ungradeable_reason')}; {frac}", 1.8))
    else:
        results.append(CheckResult(
            "condition_clear", CheckStatus.UNKNOWN,
            "no condition report", 0.4))

    # code_completion — informational; carries the full audit detail.
    status = (CheckStatus.PASS if bp >= rel_bp
              else CheckStatus.FAIL if bp < min_bp
              else CheckStatus.UNKNOWN)
    results.append(CheckResult(
        "code_completion", status,
        f"completion fraction: {frac}; release threshold {rel_bp} "
        f"basis points, split minimum {min_bp} basis points", 1.0,
        detail={"satisfied_weight": satisfied, "total_weight": total,
                "unknown_weight": grade.get("unknown_weight", 0),
                "fraction_basis_points": bp,
                "release_threshold_basis_points": rel_bp,
                "split_min_basis_points": min_bp,
                "per_requirement": grade.get("per_requirement", [])}))
    return results


def adjudicate_code_delivery(deal: Deal, evidence: dict,
                             policy: Policy | None = None):
    """Grade a code delivery through the EXISTING decision path:
    this pack's checklist -> decision_engine.adjudicate (Tier 0).
    Returns (decision, results) — the same shape DealManager.monitor
    returns for physical deals."""
    policy = policy or DEFAULT_POLICY
    results = run_code_checklist(deal, evidence, policy=policy)
    return adjudicate(deal, results, policy=policy), results
