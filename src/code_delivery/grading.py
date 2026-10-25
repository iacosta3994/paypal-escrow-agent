"""Deterministic grading of a delivered repo against requirements.

DESIGN (the B2 decision, stated plainly):

  - Every kind that CAN be graded deterministically IS graded
    deterministically (files, modules, symbols via AST, README
    headings, and — for local repos — actually running the tests).
    No model is consulted for any of them, ever.
  - The Laya shadow sensor is NOT used here at all. The simplest
    honest design for ``manual_judgment`` items: they grade UNKNOWN
    unless a human/policy verdict is passed in as
    ``manual_verdicts={requirement_id: True/False}``. An UNKNOWN
    item contributes its weight to the total (the fraction formula
    below) but neither to satisfied nor to failed — and if the
    UNKNOWN weight could swing the verdict across a policy boundary
    (release threshold or split minimum), the grade is flagged
    ``decisive_unknown`` and the checklist pack forces INVESTIGATE
    instead of letting a guess move money. That keeps every
    fixture/real-repo verdict in this workstream sensor-free and
    reproducible; should a sensor ever be wired in for manual items,
    it would be advisory-only in the audit trail and would have to
    follow the state-block hygiene pattern (canonical atomic lines,
    its own hygiene test) — it could not change this fraction.

Fraction math — integer-explicit, auditable:

    satisfied_weight = sum of weights of PASS items
    total_weight     = sum of ALL weights
    fraction_basis_points = satisfied_weight * 10000 // total_weight

  (10000 basis points = 100.00%.) No floats anywhere in the grade;
  policy thresholds are compared by cross-multiplication against
  basis points (see ``decisive_unknown`` / the checklist pack). The
  whole grade — per-requirement results, weights, basis points, and
  the thresholds in force — is a plain dict that rides in the
  evidence dict and is echoed into the Decision's key_evidence by
  the checklist pack, so a settled deal's audit record shows
  exactly how its fraction was computed.
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import CheckStatus
from ..policy import DEFAULT_POLICY, Policy
from .gather import RepoEvidence, is_stub_function, is_stub_module
from .requirements import Requirement, load_requirements


@dataclass
class RequirementResult:
    id: str
    kind: str
    weight: int
    status: CheckStatus
    evidence: str

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "weight": self.weight,
                "status": self.status.value, "evidence": self.evidence}


@dataclass
class GradeResult:
    results: list[RequirementResult] = field(default_factory=list)
    satisfied_weight: int = 0
    failed_weight: int = 0
    unknown_weight: int = 0
    total_weight: int = 0
    fraction_basis_points: int = 0
    decisive_unknown: bool = False
    ungradeable_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "per_requirement": [r.to_dict() for r in self.results],
            "satisfied_weight": self.satisfied_weight,
            "failed_weight": self.failed_weight,
            "unknown_weight": self.unknown_weight,
            "total_weight": self.total_weight,
            "fraction_basis_points": self.fraction_basis_points,
            "fraction_percent": (self.fraction_basis_points / 100.0),
            "decisive_unknown": self.decisive_unknown,
            "ungradeable_reason": self.ungradeable_reason,
        }


def _module_path(root: Path, target: str) -> Path:
    """Dotted module or path -> the file that would define it."""
    if target.endswith(".py") or "/" in target:
        return root / target
    parts = target.split(".")
    as_file = root.joinpath(*parts).with_suffix(".py")
    if as_file.exists():
        return as_file
    return root.joinpath(*parts) / "__init__.py"


def _symbol_defined(root: Path, target: str) -> tuple[bool, str]:
    if ":" not in target:
        return False, f"symbol target {target!r} lacks '<path>:<name>'"
    path_part, name = target.rsplit(":", 1)
    fpath = _module_path(root, path_part)
    if not fpath.exists():
        return False, f"{path_part}: file not found for symbol {name}"
    try:
        tree = ast.parse(fpath.read_text(encoding="utf-8",
                                         errors="ignore"))
    except SyntaxError:
        # Non-Python or unparseable: fall back to a word search.
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        if re.search(rf"\b{re.escape(name)}\b", text):
            return True, f"{name} present in {path_part} (text match)"
        return False, f"{name} not found in {path_part}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            if is_stub_function(node):
                return False, (f"{name} in {path_part} is a stub "
                               f"(pass / NotImplementedError body)")
            return True, f"{name} defined in {path_part}"
        if isinstance(node, ast.ClassDef) and node.name == name:
            return True, f"class {name} defined in {path_part}"
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return True, f"{name} assigned in {path_part}"
    return False, f"{name} not defined in {path_part}"


def _grade_one(ev: RepoEvidence, req: Requirement,
               manual_verdicts: dict) -> RequirementResult:
    root = Path(ev.root) if ev.root else None
    w = req.weight
    if not ev.present or root is None:
        return RequirementResult(
            req.id, req.kind, w, CheckStatus.FAIL,
            f"no repo delivered ({ev.reason or 'missing'})")
    if req.kind == "manual_judgment":
        if req.id in manual_verdicts:
            ok = bool(manual_verdicts[req.id])
            return RequirementResult(
                req.id, req.kind, w,
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                f"manual judgment recorded by human/policy: "
                f"{'satisfied' if ok else 'not satisfied'}")
        return RequirementResult(
            req.id, req.kind, w, CheckStatus.UNKNOWN,
            "manual-judgment item: no human/policy verdict supplied; "
            "graded UNKNOWN (no sensor consulted)")
    if req.kind == "file_exists":
        p = root / req.target
        ok = p.is_file() and p.stat().st_size > 0
        return RequirementResult(
            req.id, req.kind, w,
            CheckStatus.PASS if ok else CheckStatus.FAIL,
            f"file {req.target} {'exists, non-empty' if ok else 'missing or empty'}")
    if req.kind == "module_exists":
        p = _module_path(root, req.target)
        if not p.exists():
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.FAIL,
                f"module {req.target} not found")
        if p.suffix == ".py" and p.name != "__init__.py" \
                and is_stub_module(p):
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.FAIL,
                f"module {req.target} is an empty/stub module")
        return RequirementResult(
            req.id, req.kind, w, CheckStatus.PASS,
            f"module {req.target} present with real code")
    if req.kind == "symbol_exists":
        ok, why = _symbol_defined(root, req.target)
        return RequirementResult(
            req.id, req.kind, w,
            CheckStatus.PASS if ok else CheckStatus.FAIL, why)
    if req.kind == "readme_section":
        if not ev.readme_text:
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.FAIL,
                "no README to hold the section")
        want = req.target.strip().lower()
        for line in ev.readme_text.splitlines():
            m = re.match(r"#{1,6}\s+(.*)", line.strip())
            if m and want in m.group(1).strip().lower():
                return RequirementResult(
                    req.id, req.kind, w, CheckStatus.PASS,
                    f"README heading {line.strip()!r} matches "
                    f"{req.target!r}")
        return RequirementResult(
            req.id, req.kind, w, CheckStatus.FAIL,
            f"no README heading contains {req.target!r}")
    if req.kind == "tests_pass":
        run = ev.test_runs.get(req.target)
        if run is None:
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.UNKNOWN,
                f"tests at {req.target} were not executed "
                f"(collection-only / remote mode); not guessed")
        if run.get("status") == "missing":
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.FAIL,
                f"test target {req.target} does not exist")
        if run.get("passed") is None:
            return RequirementResult(
                req.id, req.kind, w, CheckStatus.UNKNOWN,
                f"test run errored: {run.get('tail', '')[:200]}")
        ok = bool(run.get("passed"))
        return RequirementResult(
            req.id, req.kind, w,
            CheckStatus.PASS if ok else CheckStatus.FAIL,
            f"pytest {req.target}: {'passed' if ok else 'FAILED'} "
            f"({run.get('tail', '')[-120:]})")
    return RequirementResult(req.id, req.kind, w, CheckStatus.UNKNOWN,
                             f"unhandled kind {req.kind}")


def _straddles(satisfied: int, unknown: int, total: int,
               boundary_bp: int) -> bool:
    """True iff treating UNKNOWNs as failed vs satisfied crosses the
    boundary: satisfied/total < boundary <= (satisfied+unknown)/total,
    compared by integer cross-multiplication (no floats)."""
    if total == 0:
        return False
    low_cross = satisfied * 10000 < boundary_bp * total
    high_cross = (satisfied + unknown) * 10000 >= boundary_bp * total
    return low_cross and high_cross


def grade_requirements(ev: RepoEvidence, requirements,
                       policy: Policy | None = None,
                       manual_verdicts: dict | None = None) -> GradeResult:
    policy = policy or DEFAULT_POLICY
    reqs = load_requirements(requirements)
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
