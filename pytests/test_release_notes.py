# Description: Tests for the release engine (tools/release_notes.py), the PR title check, and the release automation files.
# File: test_release_notes.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pyright: strict
# pyright: reportPrivateUsage=false

"""Tests for the commit-driven release notes, version bumps, PR title check and workflow files."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
WORKFLOWS: Path = PROJECT_ROOT / ".github" / "workflows"
REPO: str = "BuxtonCalvin/MultiProtocolGateway"
REPO_URL: str = f"https://github.com/{REPO}"
NOREPLY: str = "1+BuxtonCalvin@users.noreply.github.com"
BOT: str = "49699333+dependabot[bot]@users.noreply.github.com"
SHA: str = "0123456789abcdef0123456789abcdef01234567"


def _load_tool(name: str) -> ModuleType:
    """Import tools/<name>.py (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "tools" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rn: ModuleType = _load_tool("release_notes")
cpt: ModuleType = _load_tool("check_pr_title")


# --------------------------------------------------------------------------- helpers
def _git_is_installed() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0  # noqa: S603, S607


def _git(cwd: Path, *args: str, email: str = NOREPLY) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", f"user.email={email}", *args],  # noqa: S607
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def _commit(path: Path, message: str, email: str = NOREPLY) -> None:
    (path / "f.txt").write_text(message + "\n", encoding="utf-8")  # different content every time
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", message, email=email)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty git repository on branch master."""
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q", "-b", "master")
    return tmp_path


def _released_repo(repo: Path) -> Path:
    """A repository with a first commit tagged v1.4.2."""
    _commit(repo, "feat: First thing")
    _git(repo, "tag", "v1.4.2")
    return repo


# --------------------------------------------------------------------------- commit messages
# (subject, body, author e-mail, (section, bump, title, number, breaking, authors))
_CommitCase = tuple[str, str, str, tuple[str, str, str, int | None, bool, list[str]]]
_COMMIT_CASES: list[_CommitCase] = [
        ("feat: Add Bluetooth functionality", "", NOREPLY, ("🚀 Features", "minor", "Add Bluetooth functionality", None, False, ["BuxtonCalvin"])),
        ("feat(modbus): Add an Acme register map (#42)", "", "alice@example.com", ("🚀 Features", "minor", "Add an Acme register map", 42, False, [])),
        ("fix: Reset failure tracking on reconnect", "", NOREPLY, ("🐛 Bug Fixes", "minor", "Reset failure tracking on reconnect", None, False, ["BuxtonCalvin"])),
        ("perf: Faster scan", "", NOREPLY, ("🐛 Bug Fixes", "minor", "Faster scan", None, False, ["BuxtonCalvin"])),
        ("patch: Fix a typo in a log message", "", NOREPLY, ("🐛 Bug Fixes", "patch", "Fix a typo in a log message", None, False, ["BuxtonCalvin"])),
        ("chore(deps): bump requests from 2.31 to 2.32 (#51)", "", BOT, ("🧰 Maintenance", "patch", "bump requests from 2.31 to 2.32", 51, False, ["dependabot[bot]"])),
        ("chore(ci): bump actions/checkout from 6 to 7", "", BOT, ("🧰 Maintenance", "patch", "bump actions/checkout from 6 to 7", None, False, ["dependabot[bot]"])),
        ("build: Update the Dockerfile", "", NOREPLY, ("🧰 Maintenance", "patch", "Update the Dockerfile", None, False, ["BuxtonCalvin"])),
        ("test: Add a case", "", NOREPLY, ("🧰 Maintenance", "patch", "Add a case", None, False, ["BuxtonCalvin"])),
        ("refactor: Split the scanner", "", NOREPLY, ("🧰 Maintenance", "patch", "Split the scanner", None, False, ["BuxtonCalvin"])),
        ("style: Run ruff format", "", NOREPLY, ("🧰 Maintenance", "patch", "Run ruff format", None, False, ["BuxtonCalvin"])),
        ("docs: Update the releasing guide", "", NOREPLY, ("📚 Documentation", "patch", "Update the releasing guide", None, False, ["BuxtonCalvin"])),
        ("feat!: Rename a setting", "", NOREPLY, ("🚨 Breaking Changes", "major", "Rename a setting", None, True, ["BuxtonCalvin"])),
        ("fix(api)!: Change the payload", "", NOREPLY, ("🚨 Breaking Changes", "major", "Change the payload", None, True, ["BuxtonCalvin"])),
        ("chore!: Drop Python 3.11", "", NOREPLY, ("🚨 Breaking Changes", "major", "Drop Python 3.11", None, True, ["BuxtonCalvin"])),
        ("breaking: Remove the old endpoint", "", NOREPLY, ("🚨 Breaking Changes", "major", "Remove the old endpoint", None, True, ["BuxtonCalvin"])),
        ("feat: New config format", "Details.\n\nBREAKING CHANGE: the old file is no longer read", NOREPLY, ("🚨 Breaking Changes", "major", "New config format", None, True, ["BuxtonCalvin"])),
        ("FEAT: Shouting still counts", "", NOREPLY, ("🚀 Features", "minor", "Shouting still counts", None, False, ["BuxtonCalvin"])),
        ("wip tweak", "", NOREPLY, ("🔧 Other Changes", "minor", "wip tweak", None, False, ["BuxtonCalvin"])),
        ("Update: something happened", "", NOREPLY, ("🔧 Other Changes", "minor", "Update: something happened", None, False, ["BuxtonCalvin"])),  # an unknown keyword is not stripped
        ("Fix thing (#12)", "", "alice@example.com", ("🔧 Other Changes", "minor", "Fix thing", 12, False, [])),
]


@pytest.mark.parametrize(("subject", "body", "email", "expected"), _COMMIT_CASES)
def test_parse_commit_classifies_by_keyword(
    subject: str, body: str, email: str, expected: tuple[str, str, str, int | None, bool, list[str]]
) -> None:
    change = rn.parse_commit(SHA, subject, body, email)
    assert change is not None
    assert (change.section, change.bump, change.title, change.number, change.breaking, change.authors) == expected


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("", ""),
        ("Merge branch 'TSDB_dev' into master", ""),
        ("Merge pull request #3 from someone/branch", ""),
        ("fixup! feat: Add a thing", ""),
        ("squash! feat: Add a thing", ""),
        ("chore: set VERSION to 2.0.0 [skip changelog]", ""),
        ("docs: Tidy", "Not interesting. [skip changelog]"),
        ("docs: Tidy [Skip-Changelog]", ""),
    ],
)
def test_parse_commit_leaves_out_merges_autosquash_and_skipped_commits(subject: str, body: str) -> None:
    assert rn.parse_commit(SHA, subject, body, NOREPLY) is None


@pytest.mark.parametrize(
    ("email", "login"),
    [
        ("1+BuxtonCalvin@users.noreply.github.com", "BuxtonCalvin"),
        ("alice@users.noreply.github.com", "alice"),
        ("49699333+dependabot[bot]@users.noreply.github.com", "dependabot[bot]"),
        ("alice@example.com", None),
        ("", None),
        ("-bad-@users.noreply.github.com", None),
    ],
)
def test_login_from_email_only_trusts_github_noreply_addresses(email: str, login: str | None) -> None:
    assert rn.login_from_email(email) == login


def test_author_url_sends_bots_to_the_apps_page() -> None:
    assert rn.author_url("alice") == "https://github.com/alice"
    assert rn.author_url("dependabot[bot]") == "https://github.com/apps/dependabot"


# --------------------------------------------------------------------------- versions and bumps
def test_parse_stable_ignores_prereleases_and_junk() -> None:
    assert rn.parse_stable("v1.4.2") == (1, 4, 2)
    assert rn.parse_stable("1.10.0") == (1, 10, 0)
    assert rn.parse_stable("v2.0.0-rc1") is None
    assert rn.parse_stable("nightly") is None


@pytest.mark.parametrize(
    ("bump", "expected"),
    [("patch", (1, 4, 3)), ("minor", (1, 5, 0)), ("major", (2, 0, 0))],
)
def test_next_version_resets_the_lower_parts(bump: str, expected: tuple[int, int, int]) -> None:
    assert rn.next_version((1, 4, 2), bump) == expected


def test_next_version_without_a_previous_release_is_1_0_0() -> None:
    assert rn.next_version(None, "patch") == (1, 0, 0)
    assert rn.format_version((1, 10, 0)) == "1.10.0"


def test_the_highest_bump_wins() -> None:
    def change(subject: str) -> Any:
        return rn.parse_commit(SHA, subject, "", NOREPLY)

    assert rn.highest_bump([]) is None
    assert rn.highest_bump([change("chore: a"), change("patch: b")]) == "patch"
    assert rn.highest_bump([change("chore: a"), change("feat: b"), change("patch: c")]) == "minor"
    assert rn.highest_bump([change("chore: a")] * 20 + [change("fix: b")]) == "minor"  # one fix among routine chores
    assert rn.highest_bump([change("feat: a"), change("feat!: b")]) == "major"
    assert rn.highest_bump([change("chore: a"), change("no keyword")]) == "minor"  # unrecognized is never under-bumped


# --------------------------------------------------------------------------- rendering
def _change(subject: str, email: str = NOREPLY, sha: str = SHA) -> Any:
    change = rn.parse_commit(sha, subject, "", email)
    assert change is not None
    return change


def test_render_entry_shows_pr_author_and_hash_like_this_projects_notes() -> None:
    assert rn.render_entry(_change("feat: Add Acme map (#42)", "alice@users.noreply.github.com"), REPO) == (
        f"* Add Acme map ([#42]({REPO_URL}/pull/42) by [@alice](https://github.com/alice), `0123456`)"
    )
    assert rn.render_entry(_change("feat: Add Bluetooth"), REPO) == (
        "* Add Bluetooth (by [@BuxtonCalvin](https://github.com/BuxtonCalvin), `0123456`)"
    )
    assert rn.render_entry(_change("wip tweak", "alice@example.com"), REPO) == "* wip tweak (`0123456`)"
    assert rn.render_entry(_change("chore(deps): bump x (#5)", BOT), REPO) == (
        f"* bump x ([#5]({REPO_URL}/pull/5) by [@dependabot[bot]](https://github.com/apps/dependabot), `0123456`)"
    )
    assert rn.render_entry(_change("fix: Quick (#9)", "alice@example.com"), REPO) == f"* Quick ([#9]({REPO_URL}/pull/9), `0123456`)"


def test_render_notes_groups_sections_in_a_fixed_order_and_sorts_by_title() -> None:
    changes: list[Any] = [
        _change("docs: Update guide"),
        _change("fix: Zebra fix"),
        _change("feat: Beta"),
        _change("feat: alpha"),
        _change("feat!: Rename setting"),
        _change("wip"),
        _change("chore: tidy"),
    ]
    notes: str = rn.render_notes("2.0.0", "v1.4.2", rn.group_changes(changes), REPO, "2026-11-25")
    headings: list[str] = re.findall(r"^### (.+)$", notes, re.MULTILINE)
    assert headings == ["🚨 Breaking Changes", "🚀 Features", "🐛 Bug Fixes", "🧰 Maintenance", "📚 Documentation", "🔧 Other Changes"]
    assert notes.startswith("## v2.0.0 (2026-11-25)\n")
    assert notes.index("* alpha") < notes.index("* Beta")  # sorted by title, ignoring case
    assert f"**Detailed Changes**: [v1.4.2...v2.0.0]({REPO_URL}/compare/v1.4.2...v2.0.0)" in notes


def test_render_notes_leaves_out_empty_sections_and_the_compare_link_without_a_previous_release() -> None:
    notes: str = rn.render_notes("1.0.0", None, rn.group_changes([_change("feat: A")]), REPO, "2026-11-25")
    assert "###" in notes
    assert "Bug Fixes" not in notes
    assert "Detailed Changes" not in notes


def test_the_notes_are_read_back_by_the_changelog_generator() -> None:
    """What the Release workflow writes is exactly what the Changelog page's generator parses."""
    generator: ModuleType = _load_tool("generate_changelog")
    changes: list[Any] = [_change("feat: Add Acme map (#42)", "alice@users.noreply.github.com"), _change("patch: Fix typo"), _change("wip", "x@example.com")]
    notes: str = rn.render_notes("1.5.0", "v1.4.2", rn.group_changes(changes), REPO, "2026-11-25")
    intro, sections = generator.parse_release_body(notes)
    assert intro == ""
    found: list[tuple[str, str, int | None, list[str], str]] = [
        (s["title"], e["title"], e["number"], e["authors"], e["sha"]) for s in sections for e in s["entries"]
    ]
    assert found == [
        ("🚀 Features", "Add Acme map", 42, ["alice"], "0123456"),
        ("🐛 Bug Fixes", "Fix typo", None, ["BuxtonCalvin"], "0123456"),
        ("🔧 Other Changes", "wip", None, [], "0123456"),
    ]


def test_to_json_sections_has_the_shape_the_changelog_file_stores() -> None:
    sections: list[dict[str, Any]] = rn.to_json_sections(rn.group_changes([_change("feat: A (#3)", "alice@users.noreply.github.com")]))
    assert sections == [{"title": "🚀 Features", "entries": [{"title": "A", "number": 3, "authors": ["alice"], "sha": "0123456"}]}]


# --------------------------------------------------------------------------- author lookups
def test_enrich_authors_only_looks_up_commits_without_an_author() -> None:
    changes: list[Any] = [_change("feat: Known"), _change("feat: Unknown", "alice@example.com", sha="b" * 40)]
    calls: list[str] = []

    def fake(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        return {"author": {"login": "alice"}}

    assert rn.enrich_authors(REPO, changes, "tok", fake) == 1
    assert calls == [f"https://api.github.com/repos/{REPO}/commits/{'b' * 40}"]
    assert changes[1].authors == ["alice"]
    assert changes[0].authors == ["BuxtonCalvin"]


def test_enrich_authors_gives_up_after_repeated_failures_and_ignores_odd_answers() -> None:
    changes: list[Any] = [_change(f"feat: T{i}", "x@example.com", sha=f"{i:040x}") for i in range(1, 9)]
    calls: list[str] = []

    def failing(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        msg: str = "rate limited"
        raise rn.ReleaseNotesError(msg)

    assert rn.enrich_authors(REPO, changes, None, failing) == 0
    assert len(calls) == rn.MAX_CONSECUTIVE_LOOKUP_FAILURES

    odd: list[Any] = [_change("feat: A", "x@example.com")]

    def not_a_login(url: str, token: str | None) -> dict[str, Any]:
        return {"author": {"login": "bad login!"}}

    assert rn.enrich_authors(REPO, odd, None, not_a_login) == 0
    assert odd[0].authors == []


def test_api_get_json_refuses_non_github_urls() -> None:
    with pytest.raises(rn.ReleaseNotesError):
        rn.api_get_json("https://example.com/x", None)


# --------------------------------------------------------------------------- git history
def test_latest_release_tag_is_the_highest_stable_tag_reachable(repo: Path) -> None:
    assert rn.latest_release_tag(repo) is None
    _commit(repo, "feat: one")
    _git(repo, "tag", "v1.2.0")
    _commit(repo, "feat: two")
    _git(repo, "tag", "v1.10.0")  # numerically higher than 1.2.0
    _git(repo, "tag", "v2.0.0-rc1")  # a pre-release never counts
    _git(repo, "tag", "nightly")
    assert rn.latest_release_tag(repo) == "v1.10.0"
    assert rn.latest_release_tag(repo, "HEAD~1") == "v1.2.0"  # only tags reachable from the ref


def test_collect_changes_counts_branch_commits_but_not_merge_commits(repo: Path) -> None:
    _released_repo(repo)
    _commit(repo, "feat: Direct commit on master")
    _git(repo, "checkout", "-q", "-b", "TSDB_dev")
    _commit(repo, "fix: Commit on the dev branch")
    _commit(repo, "chore(deps): bump x (#7)", BOT)
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "TSDB_dev", "-m", "Merge branch 'TSDB_dev'")
    changes: list[Any] = rn.collect_changes(repo, "v1.4.2..HEAD")
    assert sorted(c.title for c in changes) == ["Commit on the dev branch", "Direct commit on master", "bump x"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", c.sha) for c in changes)


def test_collect_changes_reads_the_body_for_a_breaking_footer_and_skip_marker(repo: Path) -> None:
    _released_repo(repo)
    _commit(repo, "feat: New format\n\nBREAKING CHANGE: the old file is no longer read")
    _commit(repo, "docs: Tidy\n\nNot interesting [skip changelog]")
    changes: list[Any] = rn.collect_changes(repo, "v1.4.2..HEAD")
    assert [(c.title, c.bump) for c in changes] == [("New format", "major")]


def test_collect_changes_fails_clearly_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(rn.ReleaseNotesError, match="git log failed"):
        rn.collect_changes(tmp_path, "HEAD")


# --------------------------------------------------------------------------- the command line
def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, dict[str, str]]:
    """Run ``release_notes.main`` against ``repo`` and return (exit status, the step outputs it wrote)."""
    out: Path = repo.parent / f"{repo.name}-output.txt"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    status: int = rn.main(["--root", str(repo), "--no-enrich", "--date", "2026-11-25", "--github-output", *args])
    outputs: dict[str, str] = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    return status, outputs


def test_main_proposes_the_next_version_and_writes_the_notes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released_repo(repo)
    _commit(repo, "feat: Add Bluetooth functionality")
    _commit(repo, "patch: Fix a typo in a log message")
    notes_file: Path = repo.parent / "notes.md"
    status, outputs = _run(repo, monkeypatch, "--notes-file", str(notes_file))
    assert status == 0
    assert outputs == {"has_changes": "true", "version": "1.5.0", "tag": "v1.5.0", "bump": "minor", "previous_tag": "v1.4.2", "count": "2"}
    notes: str = notes_file.read_text(encoding="utf-8")
    assert notes.startswith("## v1.5.0 (2026-11-25)")
    assert "Add Bluetooth functionality" in notes
    assert "Fix a typo in a log message" in notes


def test_main_proposes_a_patch_for_routine_work_only(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released_repo(repo)
    _commit(repo, "chore(deps): bump requests (#51)", BOT)
    _commit(repo, "patch: Tiny fix")
    _commit(repo, "docs: Update guide")
    status, outputs = _run(repo, monkeypatch, "--notes-file", str(repo.parent / "n.md"))
    assert status == 0
    assert outputs["version"] == "1.4.3"
    assert outputs["bump"] == "patch"


def test_main_honors_a_bump_or_version_override(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released_repo(repo)
    _commit(repo, "feat: Something new")
    assert _run(repo, monkeypatch, "--bump", "major", "--notes-file", str(repo.parent / "a.md"))[1]["version"] == "2.0.0"
    assert _run(repo, monkeypatch, "--bump", "patch", "--notes-file", str(repo.parent / "b.md"))[1]["version"] == "1.4.3"
    assert _run(repo, monkeypatch, "--version", "3.0.0", "--notes-file", str(repo.parent / "c.md"))[1]["version"] == "3.0.0"


def test_main_rejects_a_version_that_is_not_higher_or_a_tag_that_exists(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released_repo(repo)
    _commit(repo, "feat: Something new")
    assert _run(repo, monkeypatch, "--version", "1.4.2")[0] == 2  # not higher than the last release
    assert _run(repo, monkeypatch, "--version", "1.0.0")[0] == 2
    assert _run(repo, monkeypatch, "--version", "banana")[0] == 2
    _git(repo, "checkout", "-q", "-b", "elsewhere")
    _commit(repo, "feat: Unrelated work")
    _git(repo, "tag", "v1.5.0")  # the proposed tag already exists, on a commit this release does not contain
    _git(repo, "checkout", "-q", "master")
    assert _run(repo, monkeypatch)[0] == 2


def test_main_reports_when_there_is_nothing_to_release(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released_repo(repo)
    _commit(repo, "chore: set VERSION to 2.0.0 [skip changelog]")
    status, outputs = _run(repo, monkeypatch)
    assert status == 0
    assert outputs == {"has_changes": "false", "count": "0", "previous_tag": "v1.4.2"}


def test_main_handles_a_repository_with_no_release_yet(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commit(repo, "feat: First thing")
    status, outputs = _run(repo, monkeypatch, "--notes-file", str(repo.parent / "first.md"))
    assert status == 0
    assert outputs["version"] == "1.0.0"
    assert outputs["previous_tag"] == ""


def test_main_fails_clearly_for_a_bad_repository_name_or_checkout(tmp_path: Path) -> None:
    assert rn.main(["--root", str(tmp_path), "--repo", "not a repo"]) == 2
    assert rn.main(["--root", str(tmp_path / "missing"), "--repo", REPO]) == 2


# --------------------------------------------------------------------------- the pull request title check
@pytest.mark.parametrize(
    ("title", "ok", "section", "bump"),
    [
        ("feat: Add Bluetooth functionality", True, "🚀 Features", "minor"),
        ("feat(modbus): Add a register map", True, "🚀 Features", "minor"),
        ("fix: Reset failure tracking", True, "🐛 Bug Fixes", "minor"),
        ("patch: Fix a typo", True, "🐛 Bug Fixes", "patch"),
        ("chore(deps): bump the python-all group with 4 updates", True, "🧰 Maintenance", "patch"),
        ("docs: Update the guide", True, "📚 Documentation", "patch"),
        ("feat!: Rename a setting", True, "🚨 Breaking Changes", "major"),
        ("breaking: Remove an endpoint", True, "🚨 Breaking Changes", "major"),
        ("chore: tidy [skip changelog]", True, "", ""),
        ("Added bluetooth", False, "", ""),
        ("update: something", False, "", ""),
        ("feat:", False, "", ""),
        ("feat Add a thing", False, "", ""),
        ("", False, "", ""),
    ],
)
def test_check_title(title: str, ok: bool, section: str, bump: str) -> None:
    result = rn.check_title(title)
    assert (result.ok, result.section, result.bump) == (ok, section, bump)
    if not ok:
        assert "feat:" in result.message
        assert "patch:" in result.message  # a rejected title is shown the keyword table


def test_check_pr_title_script_exit_codes_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary: Path = tmp_path / "summary.md"
    env: dict[str, str] = {"PR_TITLE": "feat: Add a thing", "GITHUB_STEP_SUMMARY": str(summary)}
    assert cpt.main([], env) == 0
    assert "Features" in capsys.readouterr().out
    assert "Pull request title: OK" in summary.read_text(encoding="utf-8")
    assert cpt.main(["Added a thing"], {"GITHUB_STEP_SUMMARY": str(summary)}) == 1
    out: str = capsys.readouterr().out
    assert out.startswith("::error title=Pull request title::")
    assert "\n" not in out.strip()  # an annotation is one line; newlines are escaped
    assert "%0A" in out
    assert cpt.main([], {}) == 1  # no title at all


# --------------------------------------------------------------------------- the automation files
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _triggers(text: str) -> str:
    """The ``on:`` block of a workflow file (everything between ``on:`` and ``permissions:``)."""
    start: int = text.index("\non:")
    return text[start:text.index("\npermissions:", start)]


def test_release_workflow_is_manual_previews_by_default_and_publishes_only_from_the_default_branch() -> None:
    text: str = _read(WORKFLOWS / "release.yml")
    triggers: str = _triggers(text)
    assert "workflow_dispatch:" in triggers
    assert not re.search(r"^  (push|pull_request|pull_request_target|release):", triggers, re.MULTILINE)  # manual only
    assert re.search(r"mode:[\s\S]*?default: preview", text)
    for option in ("preview", "publish", "auto", "major", "minor", "patch"):
        assert f"- {option}" in text
    assert "fetch-depth: 0" in text  # the notes are built from the commit history and tags
    assert "python3 tools/release_notes.py" in text
    assert "gh release create" in text
    assert "github.ref_name == github.event.repository.default_branch" in text
    assert "inputs.mode == 'publish'" in text
    assert "[skip changelog]" in text  # the bot's VERSION commit must not appear in the next release's notes
    assert "contents: write" in text


def test_release_workflow_never_puts_user_input_into_a_shell_command() -> None:
    text: str = _read(WORKFLOWS / "release.yml")
    for run_block in re.findall(r"run: \|\n((?:          .*\n)+)", text):
        assert "${{" not in run_block, run_block  # values reach scripts through env, not interpolation


def test_pr_title_workflow_checks_the_base_branch_code_and_passes_the_title_through_env() -> None:
    text: str = _read(WORKFLOWS / "pr-title.yml")
    assert "pull_request_target:" in text
    for event_type in ("opened", "edited", "reopened", "synchronize"):
        assert event_type in text
    assert 'PR_TITLE: ${{ github.event.pull_request.title }}' in text
    assert "python3 tools/check_pr_title.py" in text
    assert "ref:" not in text  # no checkout of the pull request's own code
    assert "github.event.pull_request.title" not in text.replace("PR_TITLE: ${{ github.event.pull_request.title }}", "")


def test_the_public_image_is_built_by_hand_and_release_drafter_is_gone() -> None:
    public: str = _read(WORKFLOWS / "docker-public.yml")
    triggers: str = _triggers(public)
    assert re.search(r"^  workflow_dispatch:", triggers, re.MULTILINE)
    assert not re.search(r"^  (push|pull_request|release):", triggers, re.MULTILINE)  # only the commented-out examples remain
    assert not (PROJECT_ROOT / ".github" / "release-drafter.yml").exists()
    assert "release-drafter" not in _read(WORKFLOWS / "release.yml")


def test_the_version_file_workflow_marks_its_commit_as_skipped() -> None:
    text: str = _read(WORKFLOWS / "update-version-file.yml")
    assert "[skip changelog]" in text
    assert "types: [published]" in text
    assert "!github.event.release.prerelease" in text


def test_dependabot_pull_requests_are_conventional_chores() -> None:
    text: str = _read(PROJECT_ROOT / ".github" / "dependabot.yml")
    prefixes: list[str] = re.findall(r'prefix: "([^"]+)"', text)
    assert len(prefixes) == text.count("package-ecosystem:") >= 3
    assert all(prefix.startswith("chore(") for prefix in prefixes), prefixes  # "chore(deps): ..." asks for a patch bump
    for prefix in prefixes:
        change = rn.parse_commit(SHA, f"{prefix}: bump x from 1 to 2 (#9)", "", BOT)
        assert change is not None
        assert (change.section, change.bump) == ("🧰 Maintenance", "patch")


def test_pull_request_template_lists_every_keyword_and_cannot_mark_itself_breaking() -> None:
    template: str = _read(PROJECT_ROOT / ".github" / "pull_request_template.md")
    for keyword in ("feat:", "fix:", "perf:", "patch:", "chore:", "build:", "ci:", "test:", "refactor:", "style:",
                    "docs:", "feat!:", "breaking:", "[skip changelog]"):
        assert keyword in template, keyword
    # the engine reads a BREAKING CHANGE line in a commit body; the template text must never contain that phrase
    assert not re.search(r"BREAKING[ -]CHANGE", template)
    # every keyword the engine knows is shown in the template
    for keyword in rn.TYPE_RULES:
        assert f"{keyword}:" in template or keyword == "breaking", keyword


def test_the_keyword_table_in_the_documentation_matches_the_engine() -> None:
    docs: str = _read(PROJECT_ROOT / "documentation" / "usage" / "releasing.md")
    for keyword, rule in rn.TYPE_RULES.items():
        assert f"`{keyword}:`" in docs, keyword
        assert rule.bump in docs
