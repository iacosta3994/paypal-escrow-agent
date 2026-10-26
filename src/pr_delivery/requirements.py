# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""Structured requirements for pull-request deliverables.

The PR analogue of the code-delivery requirements list: the
agreement (issue / ticket terms) stated up front, in the open, as
data — and the same list read back at delivery as the audit list.
A PR "delivers" when its diff and its checks satisfy the list.

Kinds (all deterministic except the last):

  - ``files_include``   target = a path or fnmatch glob; at least
                        one changed file in the PR must match.
  - ``files_exclude``   target = a path or fnmatch glob; NO changed
                        file may match (e.g. frozen kernel files).
  - ``check_passes``    target = a check-run name on the PR head
                        commit; the LATEST run of that name must
                        exist and have concluded ``success`` (GitHub
                        returns superseded reruns too; only the
                        newest run counts). A check that is missing
                        or whose latest run is still running grades
                        UNKNOWN, never a guess.
  - ``checklist_item``  target = text; the PR body must contain a
                        task-list item (``- [ ]`` / ``- [x]``)
                        whose text contains the target. Present and
                        checked -> PASS; present unchecked or
                        absent -> FAIL (it was part of the claim).
  - ``manual_judgment`` no deterministic test exists; grades
                        UNKNOWN unless a human/policy verdict is
                        supplied (``manual_verdicts``).

Weights are positive INTEGERS, as in the code domain: the
completion fraction is integer-explicit (see grading.py).
"""
from dataclasses import dataclass

KINDS = frozenset({
    "files_include", "files_exclude", "check_passes",
    "checklist_item", "manual_judgment",
})


@dataclass(frozen=True)
class PRRequirement:
    id: str
    kind: str
    weight: int = 1
    target: str = ""
    description: str = ""

    def __post_init__(self):
        if not self.id or not isinstance(self.id, str):
            raise ValueError(f"requirement id must be a non-empty str, "
                             f"got {self.id!r}")
        if self.kind not in KINDS:
            raise ValueError(
                f"requirement {self.id!r}: kind must be one of "
                f"{sorted(KINDS)}, got {self.kind!r}")
        if (isinstance(self.weight, bool)
                or not isinstance(self.weight, int) or self.weight < 1):
            raise ValueError(
                f"requirement {self.id!r}: weight must be a positive "
                f"int (integer-explicit fraction math), got "
                f"{self.weight!r}")
        if self.kind != "manual_judgment" and not self.target:
            raise ValueError(
                f"requirement {self.id!r}: kind {self.kind!r} needs a "
                f"target")

    @classmethod
    def from_dict(cls, d: dict) -> "PRRequirement":
        return cls(id=d["id"], kind=d["kind"],
                   weight=d.get("weight", 1), target=d.get("target", ""),
                   description=d.get("description", ""))

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "weight": self.weight,
                "target": self.target, "description": self.description}


def load_pr_requirements(spec) -> list[PRRequirement]:
    """Accept PRRequirement objects, dicts, or a list mixing both."""
    out = [r if isinstance(r, PRRequirement) else PRRequirement.from_dict(r)
           for r in spec or []]
    ids = [r.id for r in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate requirement ids in {ids}")
    return out
