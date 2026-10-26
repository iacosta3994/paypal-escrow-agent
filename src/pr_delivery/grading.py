# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""Deterministic grading of a pull request against its requirements.

Same design as the code-delivery grader (see
code_delivery/grading.py): every kind that CAN be graded
deterministically IS; ``manual_judgment`` grades UNKNOWN unless a
human/policy verdict is supplied; the completion fraction is
integer basis points; and if the UNKNOWN weight could swing the
fraction across a policy boundary, the grade is flagged
``decisive_unknown``. The result type is the code domain's own
:class:`GradeResult`, so a PR grade and a repo grade are the same
shape of audit record.
"""
import fnmatch
import re

from ..models import CheckStatus
from ..policy import DEFAULT_POLICY, Policy
from ..code_delivery.grading import GradeResult, RequirementResult
from .gather import PREvidence
from .requirements import load_pr_requirements

_ITEM_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")


def _matches_any(target: str, filenames: list[str]) -> str | None:
    for name in filenames:
        if name == target or fnmatch.fnmatch(name, target):
            return name
    return None


def _grade_one(ev: PREvidence, req, manual_verdicts: dict) -> RequirementResult:
    def done(status, evidence):
        return RequirementResult(id=req.id, kind=req.kind,
                                 weight=req.weight, status=status,
                                 evidence=evidence)
    if req.kind == "files_include":
        hit = _matches_any(req.target, ev.filenames)
        if hit:
            return done(CheckStatus.PASS,
                        f"changed file {hit} matches {req.target!r}")
        return done(CheckStatus.FAIL,
                    f"no changed file matches {req.target!r} "
                    f"({len(ev.files)} files in the PR)")
    if req.kind == "files_exclude":
        hit = _matches_any(req.target, ev.filenames)
        if hit:
            return done(CheckStatus.FAIL,
                        f"excluded file changed: {hit} "
                        f"matches {req.target!r}")
        return done(CheckStatus.PASS,
                    f"no changed file matches excluded {req.target!r}")
    if req.kind == "check_passes":
        if not ev.checks_available:
            return done(CheckStatus.UNKNOWN,
                        "check runs unavailable for the head commit; "
                        "not guessing")
        runs = [c for c in ev.checks
                if c.name.lower() == req.target.lower()]
        if not runs:
            return done(CheckStatus.UNKNOWN,
                        f"no check run named {req.target!r} on head "
                        f"{ev.head_sha[:8]}")
        # Rerun semantics: GitHub returns EVERY run of a check,
        # including superseded ones. Only the LATEST run counts —
        # the highest run id (ids are monotonic; payload position
        # breaks ties for id-less recorded fixtures). A failed run
        # that a successful rerun replaced must not fail the PR,
        # and a passing run replaced by a failing rerun must not
        # pass it.
        latest = max(enumerate(runs),
                     key=lambda t: (t[1].run_id, t[0]))[1]
        superseded = len(runs) - 1
        tail = (f" ({superseded} superseded run(s) ignored)"
                if superseded else "")
        if latest.status != "completed":
            return done(CheckStatus.UNKNOWN,
                        f"check {latest.name!r} latest run "
                        f"still {latest.status}{tail}")
        if latest.conclusion != "success":
            return done(CheckStatus.FAIL,
                        f"check {latest.name!r} latest run concluded "
                        f"{latest.conclusion or 'without a conclusion'}"
                        f"{tail}")
        return done(CheckStatus.PASS,
                    f"check {req.target!r} latest run concluded "
                    f"success{tail}")
    if req.kind == "checklist_item":
        needle = req.target.lower()
        found = checked = False
        for line in (ev.body or "").splitlines():
            m = _ITEM_RE.match(line)
            if m and needle in m.group(2).lower():
                found = True
                if m.group(1).lower() == "x":
                    checked = True
        if checked:
            return done(CheckStatus.PASS,
                        f"checklist item containing {req.target!r} "
                        f"is checked")
        if found:
            return done(CheckStatus.FAIL,
                        f"checklist item containing {req.target!r} "
                        f"present but unchecked")
        return done(CheckStatus.FAIL,
                    f"no checklist item containing {req.target!r} "
                    f"in the PR body")
    if req.kind == "manual_judgment":
        if req.id in manual_verdicts:
            ok = bool(manual_verdicts[req.id])
            return done(CheckStatus.PASS if ok else CheckStatus.FAIL,
                        "manual judgment recorded by human/policy: "
                        + ("satisfied" if ok else "not satisfied"))
        return done(CheckStatus.UNKNOWN,
                    "manual-judgment item: no human/policy verdict "
                    "supplied; graded UNKNOWN (no sensor consulted)")
    return done(CheckStatus.UNKNOWN, f"unhandled kind {req.kind!r}")


def _straddles(satisfied: int, unknown: int, total: int,
               boundary_bp: int) -> bool:
    """Identical semantics to code_delivery.grading._straddles:
    integer cross-multiplication, no floats."""
    if total == 0:
        return False
    low_cross = satisfied * 10000 < boundary_bp * total
    high_cross = (satisfied + unknown) * 10000 >= boundary_bp * total
    return low_cross and high_cross


def grade_pr_requirements(ev: PREvidence, requirements,
                          policy: Policy | None = None,
                          manual_verdicts: dict | None = None
                          ) -> GradeResult:
    policy = policy or DEFAULT_POLICY
    reqs = load_pr_requirements(requirements)
    manual_verdicts = manual_verdicts or {}
    grade = GradeResult(total_weight=sum(r.weight for r in reqs))
    if not reqs:
        grade.ungradeable_reason = "no requirements supplied to grade against"
        return grade
    grade.results = [_grade_one(ev, r, manual_verdicts) for r in reqs]
    grade.satisfied_weight = sum(
        r.weight for r in grade.results if r.status == CheckStatus.PASS)
    grade.failed_weight = sum(
        r.weight for r in grade.results if r.status == CheckStatus.FAIL)
    grade.unknown_weight = sum(
        r.weight for r in grade.results if r.status == CheckStatus.UNKNOWN)
    grade.fraction_basis_points = (
        grade.satisfied_weight * 10000 // grade.total_weight)
    if grade.unknown_weight:
        rel_bp = round(policy.code_release_threshold * 10000)
        min_bp = round(policy.code_split_min_fraction * 10000)
        if (_straddles(grade.satisfied_weight, grade.unknown_weight,
                       grade.total_weight, rel_bp)
                or _straddles(grade.satisfied_weight,
                              grade.unknown_weight, grade.total_weight,
                              min_bp)):
            grade.decisive_unknown = True
            grade.ungradeable_reason = (
                f"unknown weight {grade.unknown_weight} of "
                f"{grade.total_weight} is decisive: grading the "
                f"unknown items either way crosses a verdict "
                f"boundary (release {rel_bp}bp / split-min {min_bp}bp)")
    return grade
