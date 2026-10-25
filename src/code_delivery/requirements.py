"""Structured client requirements for code-deliverable escrow.

A requirements list is the checklist pack's input: the client's
requirements, in the open, as data — the code-delivery analogue of
the deal terms a physical-goods buyer states up front. Simple
format (JSON-able dicts, or :class:`Requirement` objects):

    {"id": "store_add", "kind": "symbol_exists", "weight": 2,
     "target": "taskcli/store.py:add_task"}

Kinds (all deterministic except the last):

  - ``file_exists``     target = repo-relative file path; the file
                        must exist and be non-empty.
  - ``module_exists``   target = dotted module (``taskcli.store``)
                        or a path; the module file / package must
                        exist and contain real code (an empty or
                        comments-only module, or one whose body is
                        only ``pass``/a docstring, does NOT count).
  - ``symbol_exists``   target = ``<path-or-module>:<name>``; the
                        name must be defined (def / class / assign)
                        in that file, and a def whose body is only
                        ``pass`` / ``...`` / ``raise
                        NotImplementedError`` is a stub and does NOT
                        count.
  - ``tests_pass``      target = test file or directory; for local
                        repos the target is actually run under
                        pytest and must pass. If tests are not run
                        (remote sampling mode), the item grades
                        UNKNOWN rather than guessing.
  - ``readme_section``  target = heading text; the repo README must
                        contain a Markdown heading containing that
                        text (case-insensitive).
  - ``manual_judgment`` no deterministic test exists. Grades UNKNOWN
                        unless a human/policy verdict is supplied to
                        the grader (``manual_verdicts``); see
                        grading.py for how UNKNOWNs affect verdicts.

Weights are positive INTEGERS on purpose: the completion fraction
is computed with integer arithmetic only (see grading.py), so a
weight like 1.5 would make the audit math ambiguous. Reject it.
"""
from dataclasses import dataclass

KINDS = frozenset({
    "file_exists", "module_exists", "symbol_exists", "tests_pass",
    "readme_section", "manual_judgment",
})


@dataclass(frozen=True)
class Requirement:
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
    def from_dict(cls, d: dict) -> "Requirement":
        return cls(id=d["id"], kind=d["kind"],
                   weight=d.get("weight", 1), target=d.get("target", ""),
                   description=d.get("description", ""))

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "weight": self.weight,
                "target": self.target, "description": self.description}


def load_requirements(spec) -> list[Requirement]:
    """Accept Requirement objects, dicts, or a list mixing both."""
    out = [r if isinstance(r, Requirement) else Requirement.from_dict(r)
           for r in spec or []]
    ids = [r.id for r in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate requirement ids in {ids}")
    return out
