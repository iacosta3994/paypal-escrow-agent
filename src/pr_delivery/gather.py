# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""Gather pull-request evidence from the GitHub API (read-only).

One question answered: what does this PR actually contain and what
do its checks say? No grading happens here, no verdict is implied —
evidence only, the same separation as code_delivery.gather.

The network is behind an injectable ``fetcher`` (url -> parsed
JSON) so tests run on recorded payloads with no network. The
default fetcher is stdlib urllib; an optional token comes from the
argument or the GITHUB_TOKEN environment variable and is sent as a
Bearer header (never logged, never stored on the evidence object).
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field

API = "https://api.github.com"
MAX_FILE_PAGES = 10  # 100 files/page -> up to 1,000 changed files


@dataclass
class ChangedFile:
    filename: str
    status: str = ""
    additions: int = 0
    deletions: int = 0


@dataclass
class CheckRun:
    name: str
    status: str = ""        # queued | in_progress | completed
    conclusion: str = ""    # success | failure | ... (completed only)
    run_id: int = 0         # GitHub check-run id (monotonic: a rerun
                            # gets a HIGHER id than the run it replaces)
    started_at: str = ""    # ISO timestamp from the payload, if any


@dataclass
class PREvidence:
    owner: str
    repo: str
    number: int
    title: str = ""
    body: str = ""
    state: str = ""
    head_sha: str = ""
    files: list[ChangedFile] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)
    checks_available: bool = False
    source: str = ""

    @property
    def filenames(self) -> list[str]:
        return [f.filename for f in self.files]

    def to_dict(self) -> dict:
        return {
            "owner": self.owner, "repo": self.repo,
            "number": self.number, "title": self.title,
            "state": self.state, "head_sha": self.head_sha,
            "files": [f.filename for f in self.files],
            "checks": [{"name": c.name, "status": c.status,
                        "conclusion": c.conclusion,
                        "run_id": c.run_id,
                        "started_at": c.started_at}
                       for c in self.checks],
            "checks_available": self.checks_available,
            "source": self.source,
        }


def _default_fetcher(token: str | None):
    def fetch(url: str):
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "paypal-escrow-agent/pr-evidence",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return fetch


def gather_pr(owner: str, repo: str, number: int, *,
              token: str | None = None, fetcher=None) -> PREvidence:
    """Fetch PR metadata, changed files (paginated) and head-commit
    check runs. ``fetcher`` is injectable for tests."""
    fetch = fetcher or _default_fetcher(
        token if token is not None else os.environ.get("GITHUB_TOKEN"))
    base = f"{API}/repos/{owner}/{repo}"
    pr = fetch(f"{base}/pulls/{number}")
    ev = PREvidence(
        owner=owner, repo=repo, number=number,
        title=pr.get("title") or "", body=pr.get("body") or "",
        state=pr.get("state") or "",
        head_sha=(pr.get("head") or {}).get("sha") or "",
        source=f"github:{owner}/{repo}#{number}")
    for page in range(1, MAX_FILE_PAGES + 1):
        batch = fetch(f"{base}/pulls/{number}/files"
                      f"?per_page=100&page={page}")
        if not batch:
            break
        for f in batch:
            ev.files.append(ChangedFile(
                filename=f.get("filename") or "",
                status=f.get("status") or "",
                additions=int(f.get("additions") or 0),
                deletions=int(f.get("deletions") or 0)))
        if len(batch) < 100:
            break
    if ev.head_sha:
        try:
            data = fetch(f"{base}/commits/{ev.head_sha}/check-runs"
                         f"?per_page=100")
            for c in data.get("check_runs") or []:
                ev.checks.append(CheckRun(
                    name=c.get("name") or "",
                    status=c.get("status") or "",
                    conclusion=c.get("conclusion") or "",
                    run_id=int(c.get("id") or 0),
                    started_at=c.get("started_at") or ""))
            ev.checks_available = True
        except Exception:
            # Checks unreadable (permissions, no Actions): evidence
            # records unavailability; check_passes items grade
            # UNKNOWN off it rather than guessing.
            ev.checks_available = False
    return ev
