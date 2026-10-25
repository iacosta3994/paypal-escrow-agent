"""GitHub / local-repo evidence adapter for code-deliverable deals.

Implements the SAME EvidenceAdapter boundary the simulator
implements (src/adapters/base.py): one ``gather(deal, tracking,
days_since_funding, messages=None) -> evidence dict`` method, with
every contract key present (neutral where a code deal has no
analogue). No kernel code was changed to support it: DealManager
accepts it by the constructor injection Wave 1 already provided.

Where the code facts ride:

  - ``evidence["code_delivery"]`` — the full repo summary + the
    deterministic grade (see grading.py). The code checklist pack
    (checklist.py in this package) consumes this.
  - ``evidence["tracking"]`` — the same facts mapped onto the
    carrier-shaped names the PHYSICAL checklist consumes, exactly as
    src/adapters/base.py anticipates ("a merged PR is the
    'delivered' event"). This mapping makes even
    ``DealManager.monitor()`` (physical run_checklist + Tier 0)
    reach the same verdicts as the code checklist pack on graded
    deals — the tests assert the two paths agree, cents included:

        fraction >= release threshold -> delivered, no items detail
        split-min <= fraction < release -> delivered=True WITH
            items_expected=total_weight / items_delivered=
            satisfied_weight, so the physical 'delivered' check
            reports UNKNOWN-with-detail and Tier 0's pre-existing
            partial-shipment rule splits at amount * S // W
        fraction < split minimum -> stalled_days set past the stall
            threshold, so 'pickup_confirmed' hard-fails -> REFUND
        ungradeable / decisive unknown -> tracking 'exception' set,
            so 'condition_clear' fails -> Tier 0 INVESTIGATE

Repo source: configured on the adapter, or per-pass via the
``tracking`` argument to monitor()/gather() — keys ``repo_path`` /
``repo_url`` / ``requirements`` / ``manual_verdicts`` in that dict
override the adapter's configured values for that pass (the same
way the simulator's tracking record is passed per pass).

STRICTLY read-only toward GitHub: a URL is only cloned (see
gather.clone_public_repo). No GitHub API, issues, PRs or writes.
"""
import tempfile
from pathlib import Path

from ..policy import DEFAULT_POLICY, Policy
from .gather import clone_public_repo, gather_repo, now_iso
from .grading import grade_requirements
from .requirements import load_requirements


class GitHubEvidenceAdapter:
    """EvidenceAdapter for code deliverables (local path primary,
    public GitHub URL read-only)."""

    def __init__(self, repo_path: str | None = None,
                 repo_url: str | None = None, requirements=None,
                 policy: Policy | None = None, run_tests: bool = True,
                 test_timeout: int = 60, clone_dir: str | None = None,
                 manual_verdicts: dict | None = None):
        self.repo_path = repo_path
        self.repo_url = repo_url
        self.requirements = load_requirements(requirements) \
            if requirements else []
        self.policy = policy or DEFAULT_POLICY
        self.run_tests = run_tests
        self.test_timeout = test_timeout
        self.clone_dir = clone_dir
        self.manual_verdicts = manual_verdicts or {}
        self._clones: dict[str, Path] = {}
        self.last_repo_evidence = None
        self.last_grade = None

    # -- repo resolution -------------------------------------------------
    def _resolve_root(self, repo_path, repo_url) -> tuple[Path | None, str]:
        if repo_path:
            return Path(repo_path), str(repo_path)
        if repo_url:
            if repo_url not in self._clones:
                slug = repo_url.rstrip("/").removesuffix(".git") \
                    .rsplit("/", 1)[-1] or "repo"
                base = Path(self.clone_dir) if self.clone_dir else \
                    Path(tempfile.mkdtemp(prefix="escrow-code-"))
                self._clones[repo_url] = clone_public_repo(
                    repo_url, base / slug)
            return self._clones[repo_url], repo_url
        return None, ""

    # -- EvidenceAdapter contract ----------------------------------------
    def gather(self, deal, tracking, days_since_funding, messages=None):
        tracking = tracking or {}
        repo_path = tracking.get("repo_path", self.repo_path)
        repo_url = tracking.get("repo_url", self.repo_url)
        req_spec = tracking.get("requirements", self.requirements)
        requirements = load_requirements(req_spec) if req_spec else []
        manual = dict(self.manual_verdicts)
        manual.update(tracking.get("manual_verdicts") or {})

        root, source = self._resolve_root(repo_path, repo_url)
        test_targets = [r.target for r in requirements
                        if r.kind == "tests_pass"]
        if root is None:
            from .gather import RepoEvidence
            repo_ev = RepoEvidence(source="", reason="no repo_path or "
                                                     "repo_url supplied")
        else:
            repo_ev = gather_repo(
                root, source, test_targets=test_targets,
                run_test_targets=self.run_tests,
                test_timeout=self.test_timeout)
        grade = grade_requirements(repo_ev, requirements,
                                   policy=self.policy,
                                   manual_verdicts=manual)
        self.last_repo_evidence, self.last_grade = repo_ev, grade

        rel_bp = round(self.policy.code_release_threshold * 10000)
        min_bp = round(self.policy.code_split_min_fraction * 10000)
        bp = grade.fraction_basis_points
        ungradeable = bool(grade.ungradeable_reason)

        mapped: dict = {"carrier": "github",
                        "number": (source.rstrip("/").rsplit("/", 1)[-1]
                                   if source else "code-delivery")}
        if repo_ev.present and not repo_ev.empty:
            # NOTE: a pickup_scan must NOT be set in the below-split-
            # minimum case — the physical pickup check passes on any
            # pickup_scan, and the hard fail below is what refunds a
            # nothing-delivered deal through the physical path too.
            if ungradeable or bp >= min_bp:
                mapped["pickup_scan"] = (repo_ev.latest_commit_at
                                         or now_iso())
            mapped["in_transit"] = True
            mapped["delivered"] = True
            mapped["delivered_at"] = repo_ev.latest_commit_at or now_iso()
            mapped["delivered_zip"] = deal.terms.delivery_zip
            if ungradeable:
                mapped["exception"] = \
                    f"ungradeable code evidence: {grade.ungradeable_reason}"
            elif bp < min_bp:
                # Nothing of value delivered: a label with no pickup —
                # stalled past the policy stall threshold.
                mapped["stalled_days"] = self.policy.stall_days + 365
            elif bp < rel_bp:
                mapped["items_expected"] = grade.total_weight
                mapped["items_delivered"] = grade.satisfied_weight
        elif repo_ev.present and repo_ev.empty:
            mapped["stalled_days"] = self.policy.stall_days + 365
        # (repo missing entirely: no number-carrying delivery facts
        #  beyond the id above + stall, set here for the hard fail)
        if not repo_ev.present:
            mapped["stalled_days"] = self.policy.stall_days + 365

        return {
            "tracking": mapped,
            "days_since_funding": days_since_funding,
            "authorized_cents": deal.terms.amount_cents,
            "dispute_filed": deal.dispute_filed,
            "dispute_evidence": list(deal.dispute_evidence),
            "buyer_confirmed": deal.buyer_confirmed_at is not None,
            "receipt_confirmed_at": deal.buyer_confirmed_at,
            # No seller-history analogue is gathered for code deals;
            # neutral None (checklist reads it as UNKNOWN), never a
            # fabricated rate like the simulator's random one.
            "seller_completion_rate": None,
            "messages": messages or [],
            "gathered_at": now_iso(),
            "code_delivery": {
                **repo_ev.summary(),
                "grade": grade.to_dict(),
                "release_threshold_basis_points": rel_bp,
                "split_min_basis_points": min_bp,
            },
        }


# Descriptive alias: the domain name for the same adapter.
CodeDeliveryEvidenceAdapter = GitHubEvidenceAdapter
