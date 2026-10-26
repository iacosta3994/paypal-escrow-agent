# Copyright Brian Acosta — Apache License 2.0 (see LICENSE / NOTICE).
"""/grade workbench: renders the form, grades a local deliverable
folder against a posted requirements list, grades a PR through the
injected gatherer, and reports bad input plainly."""
import json

import pytest

from src.app import app
from src.pr_delivery import PREvidence, ChangedFile, CheckRun


@pytest.fixture()
def client():
    return app.test_client()


def test_grade_page_renders_form(client):
    body = client.get("/grade").get_data(as_text=True)
    assert "Grade a deliverable" in body
    assert 'name="requirements"' in body


def test_grade_repo_end_to_end(client, tmp_path):
    (tmp_path / "LICENSE").write_text("Apache License 2.0\n")
    (tmp_path / "README.md").write_text("# Demo\n\n## Quickstart\nrun it\n")
    reqs = [
        {"id": "license", "kind": "file_exists", "weight": 2,
         "target": "LICENSE"},
        {"id": "qs", "kind": "readme_section", "weight": 1,
         "target": "Quickstart"},
        {"id": "review", "kind": "manual_judgment", "weight": 2},
    ]
    resp = client.post("/grade", data={
        "target_type": "repo", "repo_path": str(tmp_path),
        "requirements": json.dumps(reqs),
        "manual_verdicts": json.dumps({"review": True})})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "100.0% complete" in body and "RELEASE" in body
    assert "license" in body and "pass" in body


def test_grade_repo_decisive_unknown_shows_investigate(client, tmp_path):
    (tmp_path / "LICENSE").write_text("x")
    reqs = [{"id": "license", "kind": "file_exists", "weight": 8,
             "target": "LICENSE"},
            {"id": "review", "kind": "manual_judgment", "weight": 2}]
    body = client.post("/grade", data={
        "target_type": "repo", "repo_path": str(tmp_path),
        "requirements": json.dumps(reqs),
        "manual_verdicts": ""}).get_data(as_text=True)
    assert "80.0% complete" in body and "INVESTIGATE" in body


def test_grade_pr_via_injected_gatherer(client, monkeypatch):
    ev = PREvidence(owner="octo", repo="demo", number=7,
                    title="Add widget", head_sha="abc123",
                    files=[ChangedFile("src/widget.py")],
                    checks=[CheckRun("tests", "completed", "success")],
                    checks_available=True)
    monkeypatch.setattr("src.app.gather_pr", lambda *a, **k: ev)
    reqs = [{"id": "code", "kind": "files_include", "weight": 1,
             "target": "src/*.py"},
            {"id": "ci", "kind": "check_passes", "weight": 1,
             "target": "tests"}]
    body = client.post("/grade", data={
        "target_type": "pr", "pr_owner": "octo", "pr_repo": "demo",
        "pr_number": "7", "requirements": json.dumps(reqs),
        "manual_verdicts": ""}).get_data(as_text=True)
    assert "100.0% complete" in body and "Add widget" in body


def test_grade_bad_input_is_a_plain_error(client):
    body = client.post("/grade", data={
        "target_type": "repo", "repo_path": "/nonexistent-xyz",
        "requirements": "{not json",
        "manual_verdicts": ""}).get_data(as_text=True)
    assert "Error:" in body
