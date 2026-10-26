# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""Pull-request deliverable grading (generalization, 2026-10-25).

Extends the requirements-list pattern — the agreement stated up
front as data, the same list read back as the audit — to a third
deliverable shape: a GitHub pull request. A PR delivers when its
diff and its checks satisfy the list from the issue / agreement it
claims to close.

  - :func:`gather_pr` — read-only GitHub evidence (PR metadata,
    changed files, head-commit check runs); network behind an
    injectable fetcher.
  - :func:`grade_pr_requirements` — deterministic grading with the
    code domain's exact fraction math and decisive-unknown rule;
    returns the code domain's :class:`GradeResult`, so both domains
    produce the same shape of audit record.
"""
from .gather import ChangedFile, CheckRun, PREvidence, gather_pr
from .grading import grade_pr_requirements
from .requirements import PRRequirement, load_pr_requirements

__all__ = [
    "ChangedFile", "CheckRun", "PREvidence", "gather_pr",
    "grade_pr_requirements",
    "PRRequirement", "load_pr_requirements",
]
