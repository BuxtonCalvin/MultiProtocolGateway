# Description: Builds release notes and the next version number from the commits since the last release (direct commits and squash-merged pull requests alike).
# File: release_notes.py
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

"""Release notes and version numbers from commit messages.

One engine serves both ways changes arrive:

* **Direct commits** (for example on ``TSDB_dev``), written with a keyword: ``feat: Add Bluetooth``.
* **Pull requests from other people**, squash-merged: GitHub uses the pull request title as the commit
  message, so ``feat: Add an Acme register map (#42)`` is read exactly like a direct commit.

Every commit since the last release tag is classified by its keyword. The keyword decides the section of
the release notes AND the version bump; the highest bump among all commits wins.

====================================  ==================  ======================
Keyword                                Section             Bump (from 1.4.2)
====================================  ==================  ======================
``feat!:`` ``fix!:`` ``breaking:``     Breaking Changes    major  -> 2.0.0
``feat:``                              Features            minor  -> 1.5.0
``fix:`` ``perf:``                     Bug Fixes           minor  -> 1.5.0
``patch:`` (very minor bug fix)        Bug Fixes           patch  -> 1.4.3
``chore:`` ``build:`` ``ci:``          Maintenance         patch  -> 1.4.3
``test:`` ``refactor:`` ``style:``     Maintenance         patch  -> 1.4.3
``docs:``                              Documentation       patch  -> 1.4.3
anything else                          Other Changes       minor  -> 1.5.0
====================================  ==================  ======================

A ``BREAKING CHANGE:`` line in a commit's body also makes it a major change. Merge commits are ignored
(the commits they bring in are counted instead), and so is any commit containing ``[skip changelog]``.
Unrecognized messages are listed under Other Changes and ask for a minor bump, so nothing is under-bumped.

Each note looks like ``* Title ([#41](url) by [@alice](url), `abc1234`)``; the pull request number is
present for squash-merged pull requests, the author comes from the commit (its GitHub no-reply address, or a
lookup through the GitHub API), and the hash is the commit's.

Usage::

    python tools/release_notes.py                       # preview: print the proposed version and notes
    python tools/release_notes.py --notes-file notes.md --github-output     # as the Release workflow does
    python tools/release_notes.py --bump patch          # override the bump chosen from the keywords
    python tools/release_notes.py --version 2.0.0       # override the whole version
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

_log: logging.Logger = logging.getLogger("release_notes")

DEFAULT_REPOSITORY: str = "BuxtonCalvin/MultiProtocolGateway"
API_ROOT: str = "https://api.github.com"
GITHUB: str = "https://github.com"
REQUEST_TIMEOUT_SECONDS: int = 30
MAX_CONSECUTIVE_LOOKUP_FAILURES: int = 3
SHORT_SHA_LENGTH: int = 7

SECTION_BREAKING: str = "🚨 Breaking Changes"
SECTION_FEATURES: str = "🚀 Features"
SECTION_FIXES: str = "🐛 Bug Fixes"
SECTION_MAINTENANCE: str = "🧰 Maintenance"
SECTION_DOCS: str = "📚 Documentation"
SECTION_OTHER: str = "🔧 Other Changes"
SECTION_ORDER: tuple[str, ...] = (
    SECTION_BREAKING, SECTION_FEATURES, SECTION_FIXES, SECTION_MAINTENANCE, SECTION_DOCS, SECTION_OTHER,
)

BUMP_RANK: dict[str, int] = {"patch": 0, "minor": 1, "major": 2}

Version = tuple[int, int, int]


@dataclass(frozen=True)
class TypeRule:
    """Where a keyword's commits are listed and how large a version bump they ask for."""

    section: str
    bump: str


TYPE_RULES: dict[str, TypeRule] = {
    "breaking": TypeRule(SECTION_BREAKING, "major"),
    "feat": TypeRule(SECTION_FEATURES, "minor"),
    "fix": TypeRule(SECTION_FIXES, "minor"),
    "perf": TypeRule(SECTION_FIXES, "minor"),
    "patch": TypeRule(SECTION_FIXES, "patch"),
    "chore": TypeRule(SECTION_MAINTENANCE, "patch"),
    "build": TypeRule(SECTION_MAINTENANCE, "patch"),
    "ci": TypeRule(SECTION_MAINTENANCE, "patch"),
    "test": TypeRule(SECTION_MAINTENANCE, "patch"),
    "refactor": TypeRule(SECTION_MAINTENANCE, "patch"),
    "style": TypeRule(SECTION_MAINTENANCE, "patch"),
    "docs": TypeRule(SECTION_DOCS, "patch"),
}
UNRECOGNIZED_RULE: TypeRule = TypeRule(SECTION_OTHER, "minor")

KEYWORD_HELP: str = """Start the title with a keyword and a colon:
  feat:                                     new feature                        -> minor bump
  fix:  perf:                               bug fix, performance               -> minor bump
  patch:                                    a very minor bug fix               -> patch bump
  chore:  build:  ci:  test:  refactor:  style:   routine work                 -> patch bump
  docs:                                     documentation                      -> patch bump
  feat!:  fix!:  chore!:  breaking:         anything that breaks existing use  -> major bump
Optional scope: feat(modbus): ...      Example: feat: Add a Changelog page under Global Settings
Add [skip changelog] to leave a change out of the release notes."""

_HEADER_RE: re.Pattern[str] = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\r\n]*)\))?(?P<bang>!)?:\s*(?P<title>\S.*)$"
)
_PR_SUFFIX_RE: re.Pattern[str] = re.compile(r"\s*\(#(?P<number>\d+)\)\s*$")
_SKIP_RE: re.Pattern[str] = re.compile(r"\[skip[ -]changelog\]", re.IGNORECASE)
_AUTOSQUASH_RE: re.Pattern[str] = re.compile(r"^(?:fixup|squash)! ")
_BREAKING_FOOTER_RE: re.Pattern[str] = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_LOGIN_PATTERN: str = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?(?:\[bot\])?"
_LOGIN_RE: re.Pattern[str] = re.compile(rf"^{_LOGIN_PATTERN}$")
_NOREPLY_RE: re.Pattern[str] = re.compile(rf"^(?:\d+\+)?(?P<login>{_LOGIN_PATTERN})@users\.noreply\.github\.com$")
_STABLE_RE: re.Pattern[str] = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_REPO_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{7,40}$")


class ReleaseNotesError(RuntimeError):
    """The release notes could not be produced (bad input, git failure, or a failed API call)."""


@dataclass
class Change:
    """One commit that belongs in the release notes."""

    sha: str
    title: str
    keyword: str                 # lower-case keyword from TYPE_RULES, "" when the message has none
    scope: str
    breaking: bool
    number: int | None           # pull request number from a trailing "(#12)", if any
    section: str
    bump: str
    authors: list[str]           # GitHub logins, the first being the one credited first


@dataclass(frozen=True)
class TitleCheck:
    """The verdict on a pull request title."""

    ok: bool
    message: str
    section: str
    bump: str


# --------------------------------------------------------------------------- versions
def parse_stable(text: str) -> Version | None:
    """``"v1.4.2"`` or ``"1.4.2"`` -> ``(1, 4, 2)``; pre-releases and anything else -> None."""
    match: re.Match[str] | None = _STABLE_RE.match(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def format_version(version: Version) -> str:
    """``(1, 4, 2)`` -> ``"1.4.2"``."""
    return f"{version[0]}.{version[1]}.{version[2]}"


def next_version(previous: Version | None, bump: str) -> Version:
    """The version after ``previous`` for a ``major``, ``minor`` or ``patch`` bump (``1.0.0`` when there is no release yet)."""
    if previous is None:
        return (1, 0, 0)
    if bump == "major":
        return (previous[0] + 1, 0, 0)
    if bump == "minor":
        return (previous[0], previous[1] + 1, 0)
    return (previous[0], previous[1], previous[2] + 1)


def highest_bump(changes: Iterable[Change]) -> str | None:
    """The largest bump any change asks for, or None when there are no changes."""
    bumps: list[str] = [change.bump for change in changes]
    return max(bumps, key=BUMP_RANK.__getitem__) if bumps else None


# --------------------------------------------------------------------------- commit messages
def login_from_email(email: str) -> str | None:
    """The GitHub login in a ``users.noreply.github.com`` address, or None for any other address."""
    match: re.Match[str] | None = _NOREPLY_RE.match(email.strip())
    return match.group("login") if match else None


def author_url(login: str) -> str:
    """The profile link for a login; bots (``dependabot[bot]``) live under ``/apps/``."""
    if login.endswith("[bot]"):
        return f"{GITHUB}/apps/{login.removesuffix('[bot]')}"
    return f"{GITHUB}/{login}"


def parse_commit(sha: str, subject: str, body: str, email: str) -> Change | None:
    """
    Classify one commit; None when it must not appear in the notes (merge, autosquash, or ``[skip changelog]``).

    ``subject`` and ``body`` are the commit message's first line and the rest; ``email`` is the author's address,
    used to find the author's GitHub login when it is a no-reply address.
    """
    subject = subject.strip()
    if not subject or subject.startswith("Merge ") or _AUTOSQUASH_RE.match(subject):
        return None
    if _SKIP_RE.search(subject) or _SKIP_RE.search(body):
        return None

    keyword: str = ""
    scope: str = ""
    bang: bool = False
    text: str = subject
    header: re.Match[str] | None = _HEADER_RE.match(subject)
    if header is not None and header.group("type").lower() in TYPE_RULES:
        keyword = header.group("type").lower()
        scope = (header.group("scope") or "").strip()
        bang = header.group("bang") is not None
        text = header.group("title").strip()

    number: int | None = None
    suffix: re.Match[str] | None = _PR_SUFFIX_RE.search(text)
    if suffix is not None:
        number = int(suffix.group("number"))
        text = text[: suffix.start()].rstrip()
    if not text:
        return None

    breaking: bool = bang or keyword == "breaking" or _BREAKING_FOOTER_RE.search(body) is not None
    rule: TypeRule = TYPE_RULES.get(keyword, UNRECOGNIZED_RULE)
    if breaking:
        rule = TYPE_RULES["breaking"]

    login: str | None = login_from_email(email)
    return Change(
        sha=sha, title=text, keyword=keyword, scope=scope, breaking=breaking, number=number,
        section=rule.section, bump=rule.bump, authors=[login] if login else [],
    )


def check_title(title: str) -> TitleCheck:
    """Whether a pull request title starts with a known keyword, with a message that says what to fix."""
    stripped: str = title.strip()
    header: re.Match[str] | None = _HEADER_RE.match(stripped)
    if header is None or header.group("type").lower() not in TYPE_RULES:
        shown: str = stripped or "(empty)"
        return TitleCheck(
            ok=False,
            message=f"The title \"{shown}\" does not start with a release keyword.\n\n{KEYWORD_HELP}",
            section="",
            bump="",
)
    if _SKIP_RE.search(stripped):
        return TitleCheck(
            ok = True,
            message = "Valid title. It will be left out of the release notes ([skip changelog]).",
            section = "",
            bump = "")
    keyword: str = header.group("type").lower()
    rule: TypeRule = TYPE_RULES[keyword]
    if header.group("bang") is not None:
        rule = TYPE_RULES["breaking"]
    return TitleCheck(
        ok=True,
        message=f"Valid title: listed under {rule.section}, asks for a {rule.bump} bump.",
        section=rule.section,
        bump=rule.bump)

# --------------------------------------------------------------------------- git
def run_git(root: Path, args: Sequence[str], *, strip: bool = True) -> str | None:
    """Run ``git <args>`` in ``root``; return stdout, or None when git is missing or the command fails."""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() if strip else result.stdout


def all_tags(root: Path = PROJECT_ROOT) -> list[str]:
    """Every tag name of the repository at ``root`` (empty when git is unavailable)."""
    out: str | None = run_git(root, ["tag", "--list"])
    return out.split() if out else []


def latest_release_tag(root: Path = PROJECT_ROOT, ref: str = "HEAD") -> str | None:
    """The highest stable ``vMAJOR.MINOR.PATCH`` tag reachable from ``ref``, or None."""
    out: str | None = run_git(root, ["tag", "--merged", ref, "--list", "v*"])
    found: list[tuple[Version, str]] = []
    for tag in (out or "").split():
        parsed: Version | None = parse_stable(tag)
        if parsed is not None:
            found.append((parsed, tag))
    return max(found)[1] if found else None


def collect_changes(root: Path, rev_range: str) -> list[Change]:
    """The classified commits in ``rev_range`` (``v1.4.2..HEAD``), merge commits excluded, in git's order."""
    fmt: str = "%H%x1f%ae%x1f%s%x1f%b%x1e"
    out: str | None = run_git(root, ["log", "--no-merges", f"--format={fmt}", rev_range], strip=False)
    if out is None:
        msg: str = f"git log failed for {rev_range!r}; is this a git checkout with full history and tags?"
        raise ReleaseNotesError(msg)
    changes: list[Change] = []
    for record in out.split("\x1e"):
        if not record.strip("\n\x1f "):
            continue
        fields: list[str] = record.strip("\n").split("\x1f")
        fields += [""] * (4 - len(fields))
        change: Change | None = parse_commit(fields[0].strip(), fields[2], fields[3], fields[1])
        if change is not None:
            changes.append(change)
    return changes


# --------------------------------------------------------------------------- GitHub lookups
def api_get_json(url: str, token: str | None) -> Any:
    """GET ``url`` from the GitHub API and return the decoded JSON body."""
    if not url.startswith(f"{API_ROOT}/"):
        msg: str = f"refusing to call a non-GitHub URL: {url}"
        raise ReleaseNotesError(msg)
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mpg-release-notes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request: urllib.request.Request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        msg = f"GitHub returned HTTP {exc.code} for {url}"
        raise ReleaseNotesError(msg) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        msg = f"could not fetch {url}: {exc}"
        raise ReleaseNotesError(msg) from exc


def enrich_authors(
    repository: str,
    changes: Sequence[Change],
    token: str | None,
    get_json: Callable[[str, str | None], Any] | None = None,
) -> int:
    """
    Fill in the author of every change whose commit e-mail was not a GitHub no-reply address.

    Looks the commit up through the GitHub API (``author.login``). A failed lookup only leaves that change
    without an author; after ``MAX_CONSECUTIVE_LOOKUP_FAILURES`` failures in a row (for example the rate limit)
    the remaining lookups are skipped. Returns the number of authors found.
    """
    fetch: Callable[[str, str | None], Any] = get_json or api_get_json
    failures: int = 0
    found: int = 0
    for change in changes:
        if change.authors or failures >= MAX_CONSECUTIVE_LOOKUP_FAILURES:
            continue
        try:
            data: Any = fetch(f"{API_ROOT}/repos/{repository}/commits/{change.sha}", token)
        except ReleaseNotesError as exc:
            failures += 1
            _log.warning("Could not look up commit %s: %s", change.sha[:SHORT_SHA_LENGTH], exc)
            continue
        failures = 0
        author: Any = cast(dict[str, Any], data).get("author") if isinstance(data, dict) else None
        login: Any = cast(dict[str, Any], author).get("login") if isinstance(author, dict) else None
        if isinstance(login, str) and _LOGIN_RE.match(login):
            change.authors.append(login)
            found += 1
    return found


# --------------------------------------------------------------------------- rendering
def _title_key(change: Change) -> str:
    return change.title.lower()


def group_changes(changes: Sequence[Change]) -> list[tuple[str, list[Change]]]:
    """The changes grouped by section (in the fixed section order, empty sections left out) and sorted by title."""
    groups: list[tuple[str, list[Change]]] = []
    for section in SECTION_ORDER:
        members: list[Change] = sorted((c for c in changes if c.section == section), key=_title_key)
        if members:
            groups.append((section, members))
    return groups


def render_entry(change: Change, repository: str) -> str:
    """One Markdown bullet: ``* Title ([#41](url) by [@alice](url), `abc1234`)``."""
    meta: str = ""
    if change.number is not None:
        meta = f"[#{change.number}]({GITHUB}/{repository}/pull/{change.number})"
    authors: str = ", ".join(f"[@{login}]({author_url(login)})" for login in change.authors if _LOGIN_RE.match(login))
    if authors:
        meta = f"{meta} by {authors}" if meta else f"by {authors}"
    sha: str = f"`{change.sha[:SHORT_SHA_LENGTH]}`"
    meta = f"{meta}, {sha}" if meta else sha
    return f"* {change.title} ({meta})"


def render_notes(
    version: str,
    previous_tag: str | None,
    groups: Sequence[tuple[str, Sequence[Change]]],
    repository: str,
    date: str,
) -> str:
    """The release notes in the format the Changelog page reads (and GitHub displays)."""
    lines: list[str] = [f"## v{version} ({date})", ""]
    for section, members in groups:
        lines += [f"### {section}", ""]
        lines += [render_entry(change, repository) for change in members]
        lines.append("")
    if previous_tag:
        lines.append(f"**Detailed Changes**: [{previous_tag}...v{version}]({GITHUB}/{repository}/compare/{previous_tag}...v{version})")
        lines.append("")
    return "\n".join(lines)


def to_json_sections(groups: Sequence[tuple[str, Sequence[Change]]]) -> list[dict[str, Any]]:
    """The sections in the structure stored in changelog.json (title, entries with number, authors and sha)."""
    return [
        {
            "title": section,
            "entries": [
                {"title": c.title, "number": c.number, "authors": list(c.authors), "sha": c.sha[:SHORT_SHA_LENGTH]}
                for c in members
            ],
        }
        for section, members in groups
    ]


# --------------------------------------------------------------------------- command line
def write_github_outputs(values: dict[str, str]) -> None:
    """Append ``values`` as step outputs to the file named by ``$GITHUB_OUTPUT`` (GitHub Actions)."""
    target: str = os.environ.get("GITHUB_OUTPUT", "")
    if not target:
        _log.warning("--github-output was given but GITHUB_OUTPUT is not set; skipping")
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Propose the next version and build release notes from the commits since the last release."
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY, help="owner/name")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="git checkout to read (default: this project)")
    parser.add_argument("--ref", default="HEAD", help="commit or branch to release (default HEAD)")
    parser.add_argument("--bump", choices=["auto", "major", "minor", "patch"], default="auto",
                        help="override the bump chosen from the commit keywords")
    parser.add_argument("--version", default="", help="release exactly this version (for example 2.0.0) instead")
    parser.add_argument("--date", default="", help="date shown in the notes (default today, UTC)")
    parser.add_argument("--notes-file", type=Path, default=None, help="write the Markdown notes to this file")
    parser.add_argument("--no-enrich", action="store_true", help="skip the GitHub lookups of commit authors")
    parser.add_argument("--github-output", action="store_true", help="set version/tag/bump/previous_tag/count/has_changes outputs")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status (0 ok, also when there is nothing to release; 2 on bad input)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args: argparse.Namespace = parse_args(argv)
    repository: str = args.repo
    if not _REPO_RE.match(repository):
        _log.error("release_notes: repository must look like owner/name, got %r", repository)
        return 2
    root: Path = args.root
    try:
        previous_tag: str | None = latest_release_tag(root, args.ref)
        rev_range: str = f"{previous_tag}..{args.ref}" if previous_tag else args.ref
        changes: list[Change] = collect_changes(root, rev_range)
    except ReleaseNotesError as exc:
        _log.error("release_notes: %s", exc)
        return 2

    if not changes:
        _log.info("No releasable commits since %s.", previous_tag or "the start of the history")
        if args.github_output:
            write_github_outputs({"has_changes": "false", "count": "0", "previous_tag": previous_tag or ""})
        return 0

    previous: Version | None = parse_stable(previous_tag) if previous_tag else None
    bump: str = highest_bump(changes) or "minor"
    if args.bump != "auto":
        bump = args.bump
    if args.version:
        chosen: Version | None = parse_stable(args.version)
        if chosen is None:
            _log.error("release_notes: --version must look like 1.2.3, got %r", args.version)
            return 2
        if previous is not None and chosen <= previous:
            _log.error("release_notes: --version %s is not higher than the last release %s", args.version, previous_tag)
            return 2
        version: Version = chosen
    else:
        version = next_version(previous, bump)
    version_text: str = format_version(version)
    tag: str = f"v{version_text}"
    if tag in all_tags(root):
        _log.error("release_notes: the tag %s already exists", tag)
        return 2

    if not args.no_enrich:
        token: str | None = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
        enrich_authors(repository, changes, token)

    date: str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notes: str = render_notes(version_text, previous_tag, group_changes(changes), repository, date)
    if args.notes_file is not None:
        args.notes_file.write_text(notes, encoding="utf-8", newline="\n")
    _log.info("Proposed release %s (%s bump) from %d commit(s) since %s.", tag, bump, len(changes), previous_tag or "the start")
    if args.notes_file is None:
        sys.stdout.write(notes)
    if args.github_output:
        write_github_outputs({
            "has_changes": "true", "version": version_text, "tag": tag, "bump": bump,
            "previous_tag": previous_tag or "", "count": str(len(changes)),
        })
    return 0


if __name__ == "__main__":
    sys.exit(main())
