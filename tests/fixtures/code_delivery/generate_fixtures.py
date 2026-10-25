"""Generate the code-deliverable fixture repos (Wave 2 / B4).

Small fixture repos at CONTROLLED completion levels against the
sample client requirements in sample_requirements.json (a small
``taskcli`` CLI tool: package, store module + add_task, cli module +
main, passing tests, README Usage section; total weight 10):

    empty_0         ~0%    a README stub only — nothing built
    partial_40      ~40%   package + store module (no add_task) +
                           README Usage          = 1+2+1 = 4/10
    partial_70      ~70%   + add_task + cli module (no main),
                           no tests              = 7/10
    complete_100   100%    everything, tests pass = 10/10
    off_spec_100  ~0% vs spec (a COMPLETE build of the WRONG thing:
                           a ``notecli`` notes tool with its own
                           modules, main, passing tests and README —
                           none of it is the requested taskcli)

Usage:  python generate_fixtures.py <output-dir>
        (tests call build_all(tmp_path) directly)

Fixtures are generated, not committed as trees, because a committed
tree of nested repos would fight the outer repo's git (nested
.git dirs, __pycache__). This script IS the committed fixture.
"""
import sys
from pathlib import Path

README_HEAD = """# taskcli

A tiny command-line task tracker.

## Installation

    pip install -e .

"""

README_USAGE = """## Usage

    taskcli add "write the escrow agent"
    taskcli list

"""

STORE_NO_ADD = '''"""Task storage (partial: listing only)."""


def list_tasks(tasks):
    """Return the tasks, in order."""
    return list(tasks)
'''

STORE_FULL = '''"""Task storage."""


def add_task(tasks, title):
    """Append a new task dict and return the list."""
    tasks.append({"title": title, "done": False})
    return tasks


def list_tasks(tasks):
    """Return the tasks, in order."""
    return list(tasks)
'''

CLI_NO_MAIN = '''"""CLI (partial: run() helper only, no main entry point)."""
from taskcli.store import list_tasks


def run(argv=None):
    return list_tasks([])
'''

CLI_FULL = '''"""CLI entry point."""
import sys

from taskcli.store import add_task, list_tasks


def main(argv=None):
    tasks = []
    add_task(tasks, "sample")
    print(list_tasks(tasks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TESTS_FULL = '''from taskcli.store import add_task, list_tasks


def test_add_task_appends():
    tasks = []
    add_task(tasks, "one")
    assert list_tasks(tasks) == [{"title": "one", "done": False}]


def test_list_tasks_returns_copy():
    tasks = [{"title": "x", "done": True}]
    assert list_tasks(tasks) == tasks
'''

NOTECLI_README = """# notecli

A tiny command-line notes tool (complete, working — and not what
the client ordered).

## Getting started

    pip install -e .

## How to run

    notecli add "remember this"
    notecli show
"""

NOTECLI_STORE = '''"""Note storage."""


def add_note(notes, text):
    notes.append({"text": text})
    return notes


def list_notes(notes):
    return list(notes)
'''

NOTECLI_CLI = '''"""CLI entry point."""
import sys

from notecli.store import add_note, list_notes


def main(argv=None):
    notes = []
    add_note(notes, "sample")
    print(list_notes(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

NOTECLI_TESTS = '''from notecli.store import add_note, list_notes


def test_add_note():
    notes = []
    add_note(notes, "hi")
    assert list_notes(notes) == [{"text": "hi"}]
'''


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_all(base) -> dict[str, Path]:
    base = Path(base)
    out: dict[str, Path] = {}

    r = base / "empty_0"
    _write(r, "README.md", "# taskcli\n\nComing soon.\n")
    out["empty_0"] = r

    r = base / "partial_40"
    _write(r, "README.md", README_HEAD + README_USAGE)
    _write(r, "taskcli/__init__.py", '"""taskcli package."""\n')
    _write(r, "taskcli/store.py", STORE_NO_ADD)
    out["partial_40"] = r

    r = base / "partial_70"
    _write(r, "README.md", README_HEAD + README_USAGE)
    _write(r, "taskcli/__init__.py", '"""taskcli package."""\n')
    _write(r, "taskcli/store.py", STORE_FULL)
    _write(r, "taskcli/cli.py", CLI_NO_MAIN)
    out["partial_70"] = r

    r = base / "complete_100"
    _write(r, "README.md", README_HEAD + README_USAGE)
    _write(r, "taskcli/__init__.py", '"""taskcli package."""\n')
    _write(r, "taskcli/store.py", STORE_FULL)
    _write(r, "taskcli/cli.py", CLI_FULL)
    _write(r, "tests/test_taskcli.py", TESTS_FULL)
    out["complete_100"] = r

    r = base / "off_spec_100"
    _write(r, "README.md", NOTECLI_README)
    _write(r, "notecli/__init__.py", '"""notecli package."""\n')
    _write(r, "notecli/store.py", NOTECLI_STORE)
    _write(r, "notecli/cli.py", NOTECLI_CLI)
    _write(r, "tests/test_notecli.py", NOTECLI_TESTS)
    out["off_spec_100"] = r

    return out


if __name__ == "__main__":
    built = build_all(sys.argv[1] if len(sys.argv) > 1 else "fixtures_out")
    for name, path in built.items():
        print(f"{name}: {path}")
