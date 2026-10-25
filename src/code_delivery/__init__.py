# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""Code-deliverable escrow (Wave 2 / Workstream B, 2026-10-24).

Extends the escrow agent to a second domain — a client hires a
freelancer to build software; funds release when the delivered
GitHub repo matches the client's requirements list — WITHOUT
changing the adjudication kernel (deal state machine, Tier 0
decision rules, money path). The extension is exactly:

  - :class:`GitHubEvidenceAdapter` — a second EvidenceAdapter
    (the boundary Wave 1 / A1 defined), gathering repo evidence
    from a local path (primary) or a public GitHub URL (read-only
    clone only);
  - a requirements grader (grading.py) — deterministic wherever a
    requirement kind allows it, integer-explicit fraction math;
  - a checklist pack (checklist.py) — CheckResults in the kernel's
    existing vocabulary, decided by the existing
    decision_engine.adjudicate() and settled by the existing
    DealManager.settle().

Verdict mapping (policy thresholds in src/policy.py):
fraction >= code_release_threshold -> RELEASE; between
code_split_min_fraction and the release threshold -> SPLIT at the
measured fraction; nothing delivered -> REFUND; ungradeable or
decisive-unknown evidence -> INVESTIGATE.
"""
from .adapter import CodeDeliveryEvidenceAdapter, GitHubEvidenceAdapter
from .checklist import adjudicate_code_delivery, run_code_checklist
from .gather import RepoEvidence, gather_repo
from .grading import GradeResult, grade_requirements
from .requirements import Requirement, load_requirements

__all__ = [
    "CodeDeliveryEvidenceAdapter", "GitHubEvidenceAdapter",
    "Requirement", "load_requirements",
    "RepoEvidence", "gather_repo", "GradeResult", "grade_requirements",
    "run_code_checklist", "adjudicate_code_delivery",
]
