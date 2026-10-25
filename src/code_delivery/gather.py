"""Repository evidence gathering for code-deliverable escrow.

Read-only by construction: a local repo is only read (plus, when a
``tests_pass`` requirement asks for it, its own tests are executed
in a subprocess — local repos only); a public GitHub URL is only
cloned. No issues, PRs, comments, or any other GitHub API/write use.

What is gathered (the raw evidence the grader consumes):

  - README presence + text (the public spec the freelancer saw);
  - the file tree (sorted, capped) and per-file sizes;
  - unfinished markers: TODO/FIXME lines, ``NotImplementedError``
    occurrences, and empty/stub modules;
  - test files present, and — for ``tests_pass`` targets on local
    repos — an actual pytest run with a pass/fail result;
  - commit activity via ``git log`` when the repo is a git checkout:
    total count and the latest commit's timestamp/hash.
"""
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
              "node_modules", ".mypy_cache", ".tox"}
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".rst", ".js", ".ts", ".tsx",
                  ".jsx", ".java", ".go", ".rs", ".rb", ".sh", ".toml",
                  ".cfg", ".ini", ".json", ".yaml", ".yml"}
_MAX_FILES = 500
_MAX_SCAN_BYTES = 200_000
_GITHUB_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?$")


@dataclass
class RepoEvidence:
    source: str                    # local path or GitHub URL, as given
    root: str | None = None        # local filesystem root, if any
    present: bool = False
    reason: str = ""               # why not present, when it isn't
    files: list[str] = field(default_factory=list)
    files_truncated: bool = False
    readme_name: str | None = None
    readme_text: str = ""
    markers: dict = field(default_factory=dict)
    empty_modules: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    test_runs: dict = field(default_factory=dict)
    commit_count: int | None = None
    latest_commit_at: str | None = None
    latest_commit_hash: str | None = None

    @property
    def empty(self) -> bool:
        return self.present and not self.files

    def summary(self) -> dict:
        return {
            "source": self.source, "root": self.root,
            "present": self.present, "reason": self.reason,
            "empty": self.empty, "file_count": len(self.files),
            "files": self.files[:_MAX_FILES],
            "files_truncated": self.files_truncated,
            "readme_present": self.readme_name is not None,
            "readme_name": self.readme_name,
            "unfinished_markers": self.markers,
            "empty_modules": self.empty_modules,
            "test_files": self.test_files,
            "test_runs": self.test_runs,
            "commit_count": self.commit_count,
            "latest_commit_at": self.latest_commit_at,
            "latest_commit_hash": self.latest_commit_hash,
        }


def is_stub_function(node) -> bool:
    """True when a def's body is only a docstring plus pass / ... /
    raise NotImplementedError — a placeholder, not an implementation."""
    body = [n for n in node.body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant))]
    if not body:
        return True
    if len(body) == 1:
        only = body[0]
        if isinstance(only, ast.Pass):
            return True
        if isinstance(only, ast.Raise) and only.exc is not None:
            exc = only.exc
            func = exc.func if isinstance(exc, ast.Call) else exc
            if isinstance(func, ast.Name) and \
                    func.id == "NotImplementedError":
                return True
    return False


def is_stub_module(path: Path) -> bool:
    """True when a .py file holds no real code: empty, comments-only,
    or an AST of only a docstring / pass / Ellipsis statements."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    if not text.strip():
        return True
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False  # real (if broken) code — not a stub
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant):
            continue  # docstring / Ellipsis
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A module whose functions are ALL stubs is a stub module.
            if is_stub_function(node):
                continue
            return False
        return False
    return True


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def clone_public_repo(url: str, dest: Path) -> Path:
    """Clone a PUBLIC GitHub repo (read-only). The only network /
    GitHub operation this package performs."""
    if not _GITHUB_URL.match(url or ""):
        raise ValueError(
            f"repo_url must be a public https://github.com/<owner>/"
            f"<repo> URL, got {url!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["git", "clone", "--quiet", url, str(dest)],
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(
            f"git clone of {url} failed: {out.stderr.strip()[:300]}")
    return dest


def run_tests(root: Path, target: str, timeout: int = 60) -> dict:
    """Actually run a tests_pass target under pytest (local repos).

    Returns {"ran", "passed", "returncode", "tail"}; ``passed`` is
    None when the run errored (timeout) — the grader turns that into
    UNKNOWN, never a guessed pass/fail.
    """
    tpath = root / target
    if not tpath.exists():
        return {"ran": False, "passed": False, "status": "missing",
                "returncode": None, "tail": f"no such test target: {target}"}
    env = dict(os.environ, PYTHONPATH=str(root),
               PYTHONDONTWRITEBYTECODE="1")
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p",
             "no:cacheprovider", target],
            cwd=str(root), env=env, capture_output=True, text=True,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": None, "status": "error",
                "returncode": None,
                "tail": f"pytest timed out after {timeout}s"}
    tail = (out.stdout + out.stderr).strip()[-500:]
    return {"ran": True, "passed": out.returncode == 0,
            "status": "passed" if out.returncode == 0 else "failed",
            "returncode": out.returncode, "tail": tail}


def gather_repo(root: Path, source: str,
                test_targets: list[str] | None = None,
                run_test_targets: bool = True,
                test_timeout: int = 60) -> RepoEvidence:
    ev = RepoEvidence(source=source, root=str(root))
    if not root.exists():
        ev.reason = f"repo path does not exist: {root}"
        return ev
    if not root.is_dir():
        ev.reason = f"repo path is not a directory: {root}"
        return ev
    ev.present = True

    all_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            all_files.append(Path(dirpath) / name)
    rel = [str(p.relative_to(root)) for p in all_files]
    ev.files = rel[:_MAX_FILES]
    ev.files_truncated = len(rel) > _MAX_FILES

    for p in all_files:
        if p.parent == root and p.name.lower().startswith("readme"):
            ev.readme_name = p.name
            try:
                ev.readme_text = p.read_text(
                    encoding="utf-8", errors="ignore")
            except OSError:
                ev.readme_text = ""
            break

    todos: list[str] = []
    not_impl = 0
    for p in all_files:
        r = str(p.relative_to(root))
        if r.startswith("test_") or "/test" in f"/{r}" or \
                p.name.startswith("test_") or p.name.endswith("_test.py"):
            ev.test_files.append(r)
        if p.suffix == ".py" and is_stub_module(p):
            ev.empty_modules.append(r)
        if p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        not_impl += text.count("NotImplementedError")
        for i, line in enumerate(text.splitlines(), 1):
            if "TODO" in line or "FIXME" in line:
                todos.append(f"{r}:{i}: {line.strip()[:80]}")
    ev.markers = {"todo_fixme_count": len(todos),
                  "todo_fixme_examples": todos[:20],
                  "not_implemented_error_count": not_impl,
                  "empty_module_count": len(ev.empty_modules)}

    if (root / ".git").exists():
        count = _git(root, "rev-list", "--count", "HEAD")
        ev.commit_count = int(count) if count and count.isdigit() else None
        ev.latest_commit_at = _git(root, "log", "-1", "--format=%cI")
        ev.latest_commit_hash = _git(root, "log", "-1", "--format=%H")

    if run_test_targets:
        for target in test_targets or []:
            ev.test_runs[target] = run_tests(root, target, test_timeout)
    return ev


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
