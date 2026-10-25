"""Code-deliverable escrow (Wave 2 / Workstream B).

Thesis under test: the adjudication kernel (deal state machine +
Tier 0 rules + money path) does NOT change for a new domain. Code
deals use only a new evidence adapter + a checklist pack, decided
by the existing decision_engine.adjudicate() and settled by the
existing DealManager.settle(). The fixtures assert verdicts through
BOTH paths — the code checklist pack, and DealManager.monitor()
(physical checklist over the adapter's mapped tracking) — and
require them to agree, split cents included.

Fixture design (sample_requirements.json, total weight 10):
empty_0 ~0%, partial_40 ~40%, partial_70 ~70%, complete_100 100%,
off_spec_100 = a complete build of the WRONG thing (~0% vs spec).
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from src.adapters import EvidenceAdapter
from src.checklist import run_checklist
from src.code_delivery import (GitHubEvidenceAdapter, Requirement,
                               adjudicate_code_delivery,
                               grade_requirements, gather_repo,
                               run_code_checklist)
from src.deal_manager import DealManager
from src.models import DealState, DealTerms, Verdict
from src.policy import Policy

from .e2e_support import audit_log, make_terms  # noqa: F401

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "code_delivery"
REQUIREMENTS = json.loads(
    (FIXTURE_DIR / "sample_requirements.json").read_text())

_spec = importlib.util.spec_from_file_location(
    "generate_fixtures", FIXTURE_DIR / "generate_fixtures.py")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)

AMOUNT = 100_000  # $1,000 code deal — round for split math


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    return _gen.build_all(tmp_path_factory.mktemp("code_fixtures"))


def _terms():
    return DealTerms(amount_cents=AMOUNT, currency="USD",
                     item_description="Build the taskcli CLI tool",
                     buyer_email="client@example.com",
                     seller_email="freelancer@example.com",
                     delivery_zip="47172", window_days=14)


def _adapter(path, **kw):
    return GitHubEvidenceAdapter(repo_path=str(path),
                                 requirements=REQUIREMENTS, **kw)


def _grade(path, **kw):
    mgr = DealManager()  # only for a deal object; no money moves
    deal = mgr.propose(_terms())
    ev = _adapter(path, **kw).gather(deal, {}, 3)
    return deal, ev


def _verdicts(path, **kw):
    """(code-pack decision, grade dict) via adjudicate_code_delivery."""
    deal, ev = _grade(path, **kw)
    decision, _ = adjudicate_code_delivery(deal, ev)
    return decision, ev["code_delivery"]["grade"]


# --- B1: the adapter honours the Wave 1 evidence boundary ---

def test_adapter_satisfies_evidence_protocol(fixtures):
    assert isinstance(_adapter(fixtures["complete_100"]),
                      EvidenceAdapter)


def test_adapter_returns_full_contract_keys(fixtures):
    _, ev = _grade(fixtures["complete_100"])
    for key in ("tracking", "days_since_funding", "authorized_cents",
                "dispute_filed", "dispute_evidence", "buyer_confirmed",
                "receipt_confirmed_at", "seller_completion_rate",
                "messages", "gathered_at", "code_delivery"):
        assert key in ev, key
    assert ev["authorized_cents"] == AMOUNT
    assert ev["seller_completion_rate"] is None  # neutral, not invented


def test_adapter_gathers_repo_facts(fixtures):
    _, ev = _grade(fixtures["complete_100"])
    cd = ev["code_delivery"]
    assert cd["present"] and cd["readme_present"]
    assert "taskcli/store.py" in cd["files"]
    assert cd["test_files"] == ["tests/test_taskcli.py"]
    assert cd["unfinished_markers"]["todo_fixme_count"] == 0


def test_adapter_detects_unfinished_markers(tmp_path):
    _gen._write(tmp_path, "mod.py",
                '"""x."""\n# TODO: finish\n\n'
                'def f():\n    raise NotImplementedError\n')
    _gen._write(tmp_path, "empty.py", "")
    repo = gather_repo(tmp_path, str(tmp_path))
    assert repo.markers["todo_fixme_count"] == 1
    assert repo.markers["not_implemented_error_count"] == 1
    assert "empty.py" in repo.empty_modules
    assert "mod.py" in repo.empty_modules  # stub-only module


def test_adapter_git_commit_activity(tmp_path):
    repo_dir = tmp_path / "gitrepo"
    repo_dir.mkdir()
    _gen._write(repo_dir, "a.py", "X = 1\n")
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo_dir,
                   check=True, env=env)
    repo = gather_repo(repo_dir, str(repo_dir))
    assert repo.commit_count == 1 and repo.latest_commit_at
    assert len(repo.latest_commit_hash) == 40


def test_adapter_rejects_non_github_urls():
    adapter = GitHubEvidenceAdapter(
        repo_url="https://example.com/x/y", requirements=REQUIREMENTS)
    mgr = DealManager()
    deal = mgr.propose(_terms())
    with pytest.raises(ValueError):
        adapter.gather(deal, {}, 1)


# --- B2: deterministic grading, integer fraction math ---

def test_requirement_weights_must_be_ints():
    with pytest.raises(ValueError):
        Requirement(id="x", kind="file_exists", weight=1.5, target="a")
    with pytest.raises(ValueError):
        Requirement(id="x", kind="bogus_kind", target="a")


def test_stub_symbol_does_not_count(tmp_path):
    _gen._write(tmp_path, "taskcli/__init__.py", '"""p."""\n')
    _gen._write(tmp_path, "taskcli/store.py",
                '"""s."""\n\ndef add_task(tasks, title):\n'
                '    raise NotImplementedError\n')
    repo = gather_repo(tmp_path, str(tmp_path))
    grade = grade_requirements(repo, REQUIREMENTS)
    by_id = {r.id: r for r in grade.results}
    assert by_id["store_add_task"].status.value == "fail"
    assert "stub" in by_id["store_add_task"].evidence


def test_manual_judgment_unknown_and_decisive(tmp_path):
    _gen._write(tmp_path, "README.md", "# x\n\n## Usage\n\nrun it\n")
    reqs = [{"id": "looks_good", "kind": "manual_judgment",
             "weight": 10, "target": "", "description": "feels right"}]
    repo = gather_repo(tmp_path, str(tmp_path))
    grade = grade_requirements(repo, reqs)
    assert grade.unknown_weight == 10 and grade.decisive_unknown
    # A human verdict settles the same item deterministically.
    grade2 = grade_requirements(repo, reqs,
                                manual_verdicts={"looks_good": True})
    assert grade2.satisfied_weight == 10 and not grade2.decisive_unknown
    assert grade2.fraction_basis_points == 10000


def test_manual_judgment_non_decisive_unknown(tmp_path):
    # 9/10 deterministically satisfied + a weight-1 manual item:
    # unknown either way stays >= the 0.9 release threshold, so the
    # unknown is NOT decisive and the fraction stands as computed.
    _gen._write(tmp_path, "README.md", "# x\n\n## Usage\n\nu\n")
    reqs = [{"id": "f", "kind": "file_exists", "weight": 9,
             "target": "README.md"},
            {"id": "m", "kind": "manual_judgment", "weight": 1,
             "target": ""}]
    grade = grade_requirements(gather_repo(tmp_path, str(tmp_path)), reqs)
    assert grade.satisfied_weight == 9 and grade.unknown_weight == 1
    assert not grade.decisive_unknown
    assert grade.fraction_basis_points == 9000


# --- B3/B4: fixture table — measured fraction + verdict, both paths ---

# name, designed %, expected verdict, expected split cents (or None)
FIXTURE_TABLE = [
    ("empty_0", 0, Verdict.REFUND, None),
    ("partial_40", 40, Verdict.SPLIT, 40_000),
    ("partial_70", 70, Verdict.SPLIT, 70_000),
    ("complete_100", 100, Verdict.RELEASE, None),
    ("off_spec_100", 0, Verdict.REFUND, None),
]


@pytest.mark.parametrize("name,designed,verdict,split_cents",
                         FIXTURE_TABLE,
                         ids=[t[0] for t in FIXTURE_TABLE])
def test_fixture_fraction_and_verdict_code_pack(fixtures, name, designed,
                                                verdict, split_cents):
    decision, grade = _verdicts(fixtures[name])
    measured = grade["fraction_basis_points"] / 100
    assert abs(measured - designed) <= 15, (name, grade)
    assert decision.verdict == verdict, (name, decision)
    assert decision.tier == 0 and decision.cost_usd == 0.0
    if verdict == Verdict.SPLIT:
        assert decision.split_release_cents == split_cents
    # The fraction is in the decision record (audit trail) itself.
    blob = " ".join(decision.key_evidence) + decision.reasoning
    assert f"{grade['satisfied_weight']}/{grade['total_weight']}" in blob


@pytest.mark.parametrize("name,designed,verdict,split_cents",
                         FIXTURE_TABLE,
                         ids=[t[0] for t in FIXTURE_TABLE])
def test_fixture_verdict_matches_through_deal_manager_monitor(
        fixtures, audit_log, name, designed, verdict, split_cents):
    """The kernel-untouched proof: DealManager.monitor() runs the
    PHYSICAL checklist over the adapter's mapped tracking — and Tier
    0 reaches the same verdict (and the same split cents)."""
    mgr = DealManager(evidence_adapter=_adapter(fixtures[name]))
    deal = mgr.propose(_terms())
    mgr.fund(deal)
    decision, results = mgr.monitor(deal, tracking={},
                                    days_since_funding=3)
    assert decision.verdict == verdict, (name, decision,
                                          [(r.name, r.status) for r in results])
    if verdict == Verdict.SPLIT:
        assert decision.split_release_cents == split_cents


def test_missing_repo_refunds(fixtures):
    decision, grade = _verdicts(fixtures["empty_0"].parent / "no_such_repo")
    assert decision.verdict == Verdict.REFUND
    assert grade["fraction_basis_points"] == 0


def test_ungradeable_goes_investigate(tmp_path):
    # A repo exists, but the ONLY requirement is a manual-judgment
    # item with decisive weight and no human verdict: INVESTIGATE,
    # never a guessed release/refund/split.
    _gen._write(tmp_path, "README.md", "# x\n\n## Usage\n\nu\n")
    reqs = [{"id": "quality", "kind": "manual_judgment", "weight": 5,
             "target": "", "description": "is it good?"}]
    mgr = DealManager()
    deal = mgr.propose(_terms())
    ev = GitHubEvidenceAdapter(repo_path=str(tmp_path),
                               requirements=reqs).gather(deal, {}, 3)
    decision, _ = adjudicate_code_delivery(deal, ev)
    assert decision.verdict == Verdict.INVESTIGATE
    assert decision.tier == 0


def test_policy_thresholds_come_from_policy(fixtures):
    # Same 70% fixture, release threshold lowered to 0.6 -> RELEASE.
    # Proves the bounds live in policy data, not in the pack's code.
    deal, ev = _grade(fixtures["partial_70"])
    p = Policy().with_overrides(code_release_threshold=0.6)
    decision, _ = adjudicate_code_delivery(deal, ev, policy=p)
    assert decision.verdict == Verdict.RELEASE
    results = run_code_checklist(deal, ev, policy=p)
    comp = {r.name: r for r in results}["code_completion"]
    assert comp.detail["release_threshold_basis_points"] == 6000


def test_tests_not_run_grades_unknown_not_guessed(fixtures):
    # run_tests=False (remote-sampling mode): the tests_pass item is
    # UNKNOWN. At weight 2 of 10 on an otherwise-complete repo, the
    # unknown is decisive (8000bp satisfied could be 10000bp), so
    # the verdict must be INVESTIGATE — not a release on a guess.
    decision, grade = _verdicts(fixtures["complete_100"], run_tests=False)
    assert grade["unknown_weight"] == 2 and grade["decisive_unknown"]
    assert decision.verdict == Verdict.INVESTIGATE


# --- B6: end-to-end through DealManager + the money path ---

def test_e2e_split_settles_through_deal_manager(fixtures, audit_log):
    mgr = DealManager(evidence_adapter=_adapter(fixtures["partial_70"]))
    deal = mgr.propose(_terms(), proposer="buyer")
    mgr.accept_terms(deal, "seller")
    mgr.fund(deal)
    assert deal.state == DealState.FUNDED
    # Repo delivered as evidence -> graded via the code pack ->
    # existing adjudicate() -> existing settle() money path.
    ev = mgr.evidence_adapter.gather(deal, {}, 3)
    decision, _ = adjudicate_code_delivery(deal, ev)
    assert decision.verdict == Verdict.SPLIT
    mgr.settle(deal, decision)
    assert deal.state == DealState.SPLIT_SETTLED
    assert deal.split_released_cents == 70_000
    assert deal.split_voided_cents == 30_000


def test_e2e_release_and_refund_through_monitor(fixtures, audit_log):
    for name, end_state in (("complete_100", DealState.RELEASED),
                            ("empty_0", DealState.REFUNDED)):
        mgr = DealManager(evidence_adapter=_adapter(fixtures[name]))
        deal = mgr.propose(_terms(), proposer="seller")
        mgr.accept_terms(deal, "buyer")
        mgr.fund(deal)
        mgr.run_cycle(deal, tracking={}, days_since_funding=3)
        assert deal.state == end_state, name


def test_e2e_monitor_physical_checklist_agrees_on_grade_record(fixtures,
                                                              audit_log):
    # The physical checklist also consumed the adapter evidence
    # without error (contract keys are all present and neutral where
    # a code deal has no physical analogue).
    mgr = DealManager(evidence_adapter=_adapter(fixtures["partial_40"]))
    deal = mgr.propose(_terms())
    mgr.fund(deal)
    ev = mgr.evidence_adapter.gather(deal, {}, 3)
    results = run_checklist(deal, ev)
    by_name = {r.name: r for r in results}
    assert by_name["delivered"].detail == {"items_delivered": 4,
                                           "items_expected": 10}
