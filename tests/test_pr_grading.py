# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""PR-deliverable grading: gather (recorded payloads, no network),
every requirement kind, and the shared fraction/decisive math."""
import pytest

from src.pr_delivery import (PRRequirement, gather_pr,
                             grade_pr_requirements,
                             load_pr_requirements)

PR = {"title": "Add escrow widget", "state": "open",
      "body": "Implements the widget.\n\n- [x] Widget renders\n"
              "- [ ] Docs updated\n",
      "head": {"sha": "abc123def456"}}
FILES = [{"filename": "src/widget.py", "status": "added",
          "additions": 120, "deletions": 0},
         {"filename": "README.md", "status": "modified",
          "additions": 5, "deletions": 1}]
CHECKS = {"check_runs": [
    {"name": "tests", "status": "completed", "conclusion": "success"},
    {"name": "lint", "status": "completed", "conclusion": "failure"}]}


def fetcher(files=FILES, checks=CHECKS, pr=PR):
    def fetch(url):
        if "/files" in url:
            return files
        if "/check-runs" in url:
            if checks is None:
                raise RuntimeError("no access")
            return checks
        return pr
    return fetch


def ev(**kw):
    return gather_pr("octo", "demo", 7, fetcher=fetcher(**kw))


def statuses(grade):
    return {r.id: r.status.value for r in grade.results}


def test_gather_populates_evidence():
    e = ev()
    assert e.title == "Add escrow widget"
    assert e.head_sha == "abc123def456"
    assert e.filenames == ["src/widget.py", "README.md"]
    assert e.checks_available is True
    assert [c.name for c in e.checks] == ["tests", "lint"]


def test_gather_paginates_files():
    page1 = [{"filename": f"f{i}.py"} for i in range(100)]
    def fetch(url):
        if "page=2" in url:
            return [{"filename": "last.py"}]
        if "/files" in url:
            return page1
        if "/check-runs" in url:
            return {"check_runs": []}
        return PR
    e = gather_pr("o", "r", 1, fetcher=fetch)
    assert len(e.files) == 101 and e.files[-1].filename == "last.py"


def test_files_include_exact_and_glob():
    g = grade_pr_requirements(ev(), [
        {"id": "w", "kind": "files_include", "target": "src/widget.py"},
        {"id": "g", "kind": "files_include", "target": "src/*.py"},
        {"id": "x", "kind": "files_include", "target": "docs/guide.md"},
    ])
    assert statuses(g) == {"w": "pass", "g": "pass", "x": "fail"}


def test_files_exclude():
    ok = grade_pr_requirements(ev(), [
        {"id": "k", "kind": "files_exclude", "target": "src/kernel.py"}])
    assert statuses(ok) == {"k": "pass"}
    bad = grade_pr_requirements(ev(), [
        {"id": "k", "kind": "files_exclude", "target": "README.md"}])
    assert statuses(bad) == {"k": "fail"}


def test_check_passes_states():
    g = grade_pr_requirements(ev(), [
        {"id": "t", "kind": "check_passes", "target": "tests"},
        {"id": "l", "kind": "check_passes", "target": "lint"},
        {"id": "m", "kind": "check_passes", "target": "deploy"},
    ])
    assert statuses(g) == {"t": "pass", "l": "fail", "m": "unknown"}


def test_check_passes_running_and_unavailable_are_unknown():
    running = {"check_runs": [{"name": "tests", "status": "in_progress",
                               "conclusion": ""}]}
    g = grade_pr_requirements(ev(checks=running), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g) == {"t": "unknown"}
    g2 = grade_pr_requirements(ev(checks=None), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g2) == {"t": "unknown"}
    assert ev(checks=None).checks_available is False


def test_checklist_items():
    g = grade_pr_requirements(ev(), [
        {"id": "r", "kind": "checklist_item", "target": "Widget renders"},
        {"id": "d", "kind": "checklist_item", "target": "Docs updated"},
        {"id": "a", "kind": "checklist_item", "target": "Never listed"},
    ])
    assert statuses(g) == {"r": "pass", "d": "fail", "a": "fail"}


def test_manual_judgment_and_decisive_unknown():
    reqs = [{"id": "core", "kind": "files_include", "weight": 8,
             "target": "src/widget.py"},
            {"id": "review", "kind": "manual_judgment", "weight": 2}]
    g = grade_pr_requirements(ev(), reqs)
    assert g.fraction_basis_points == 8000
    assert g.decisive_unknown is True
    g2 = grade_pr_requirements(ev(), reqs,
                               manual_verdicts={"review": True})
    assert g2.fraction_basis_points == 10000
    assert g2.decisive_unknown is False


def test_fraction_math_no_floats():
    g = grade_pr_requirements(ev(), [
        {"id": "a", "kind": "files_include", "weight": 1,
         "target": "README.md"},
        {"id": "b", "kind": "files_include", "weight": 2,
         "target": "nope.txt"}])
    assert g.satisfied_weight == 1 and g.total_weight == 3
    assert g.fraction_basis_points == 3333  # integer floor, auditable


def test_requirement_validation():
    with pytest.raises(ValueError):
        PRRequirement(id="x", kind="bogus", target="t")
    with pytest.raises(ValueError):
        PRRequirement(id="x", kind="files_include", weight=0, target="t")
    with pytest.raises(ValueError):
        load_pr_requirements([
            {"id": "d", "kind": "files_include", "target": "a"},
            {"id": "d", "kind": "files_include", "target": "b"}])


RERUN_OLD_FAIL_NEW_PASS = {"check_runs": [
    {"id": 101, "name": "tests", "status": "completed",
     "conclusion": "failure", "started_at": "2026-10-25T10:00:00Z"},
    {"id": 205, "name": "tests", "status": "completed",
     "conclusion": "success", "started_at": "2026-10-25T11:00:00Z"}]}
RERUN_OLD_PASS_NEW_FAIL = {"check_runs": [
    {"id": 101, "name": "tests", "status": "completed",
     "conclusion": "success", "started_at": "2026-10-25T10:00:00Z"},
    {"id": 205, "name": "tests", "status": "completed",
     "conclusion": "failure", "started_at": "2026-10-25T11:00:00Z"}]}


def test_gather_records_run_identity():
    e = ev(checks=RERUN_OLD_FAIL_NEW_PASS)
    assert [c.run_id for c in e.checks] == [101, 205]
    assert e.checks[1].started_at == "2026-10-25T11:00:00Z"
    assert e.to_dict()["checks"][0]["run_id"] == 101


def test_check_rerun_superseded_failure_passes():
    # The original run failed; the rerun passed. Only the latest
    # run counts, so the item passes.
    g = grade_pr_requirements(ev(checks=RERUN_OLD_FAIL_NEW_PASS), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g) == {"t": "pass"}
    assert "superseded" in g.results[0].evidence


def test_check_rerun_order_in_payload_does_not_matter():
    # Highest id wins even if the payload lists the rerun first.
    shuffled = {"check_runs": list(
        reversed(RERUN_OLD_FAIL_NEW_PASS["check_runs"]))}
    g = grade_pr_requirements(ev(checks=shuffled), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g) == {"t": "pass"}


def test_check_rerun_newer_failure_fails():
    # A passing run superseded by a failing rerun must NOT pass.
    g = grade_pr_requirements(ev(checks=RERUN_OLD_PASS_NEW_FAIL), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g) == {"t": "fail"}


def test_check_rerun_latest_still_running_is_unknown():
    running = {"check_runs": [
        {"id": 101, "name": "tests", "status": "completed",
         "conclusion": "success"},
        {"id": 205, "name": "tests", "status": "in_progress",
         "conclusion": ""}]}
    g = grade_pr_requirements(ev(checks=running), [
        {"id": "t", "kind": "check_passes", "target": "tests"}])
    assert statuses(g) == {"t": "unknown"}
