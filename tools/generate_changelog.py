# Description: Builds classes/WebServer/changelog.json (published GitHub Releases plus the build's git version) for the Changelog page.
# File: generate_changelog.py
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

"""Generate ``classes/WebServer/changelog.json`` for the admin UI's Changelog page.

The page shows one entry per *published GitHub Release*. The release notes are the ones the Release
workflow built from the commits since the previous release (``tools/release_notes.py``: direct commits and
squash-merged pull requests, read from their keywords), so the changelog is driven by git tags, releases and
commit messages, and nothing is typed in twice.

A running container has no ``.git`` folder and should not need network access, so this tool
runs at *build time* (in the Docker workflows, or by hand on a developer machine) and the
result is shipped inside the image.

What it records (schema 2)::

    {"schema": 2, "generated_at": "...", "repository": "owner/name",
     "build": {"version": "1.2.0+5.gabc1234", "commit": "<sha>"},
     "releases": [{"tag": "v1.2.0", "version": "1.2.0", "name": "v1.2.0",
                   "published_at": "...", "prerelease": false, "url": "...",
                   "released_by": "alice", "commit": "abc1234",
                   "intro": "text before the first section",
                   "sections": [{"title": "Features",
                                 "entries": [{"title": "Add X", "number": 12,
                                              "authors": ["alice"], "sha": "def5678"}]}],
                   "body": "the original markdown"}]}

The release body written by ``tools/release_notes.py`` is parsed into ``sections`` and ``entries`` so the
page can show the same detail as the notes (title, pull request link, authors, commit hash) in collapsible
levels. Notes without hashes (for example ones typed by hand) get them from the GitHub API, one lookup per
pull request; ``--no-enrich`` skips every API lookup (offline, or to save API calls).

When the checkout is ahead of the last release tag, the document also gets an ``"unreleased"`` object
(``previous_tag``, ``projected_version``, ``bump``, ``commit`` and ``sections``) listing the commits made
since, so an image built from ``TSDB_dev`` can show what the next release will contain.
``--no-unreleased`` leaves it out.

Usage::

    python tools/generate_changelog.py                       # write the file
    python tools/generate_changelog.py --dry-run             # show what would be written
    python tools/generate_changelog.py --github-output       # also set step outputs in GitHub Actions

Set ``GITHUB_TOKEN`` to avoid GitHub's anonymous rate limit (the workflows do). Draft releases
are never included. Exit status is 0 on success and 1 if the releases cannot be fetched.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/, for release_notes

import release_notes  # noqa: E402

from defs.app_version import (  # noqa: E402
    BUILD_INFO_PATH,
    UNKNOWN_VERSION,
    git_commit,
    git_tag_commit,
    git_version,
)

_log: logging.Logger = logging.getLogger("generate_changelog")

DEFAULT_REPOSITORY: str = "BuxtonCalvin/MultiProtocolGateway"
API_ROOT: str = "https://api.github.com"
PAGE_SIZE: int = 100
REQUEST_TIMEOUT_SECONDS: int = 30
SCHEMA_VERSION: int = 2
MAX_CONSECUTIVE_LOOKUP_FAILURES: int = 3
SHORT_SHA_LENGTH: int = 7

_REPO_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LEADING_V_RE: re.Pattern[str] = re.compile(r"^v(?=\d)")
_DOCKER_TAG_UNSAFE_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9_.-]")

# --- release body parsing -------------------------------------------------------------------
_HEADING_RE: re.Pattern[str] = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_BULLET_RE: re.Pattern[str] = re.compile(r"^\s*[*+-]\s+(?P<text>.+?)\s*$")
_VERSION_HEADING_RE: re.Pattern[str] = re.compile(r"^v?\s*\d+\.\d+")
# "(#12" or "([#12](https://...)" : the start of the pull request reference that follows the title.
_PR_REF_RE: re.Pattern[str] = re.compile(r"\(\s*(?:\[#(?P<linked>\d+)\]\([^)]*\)|#(?P<plain>\d+))")
# "[@alice](url)", "[@dependabot[bot]](url)" or a bare "@alice".
_MENTION_RE: re.Pattern[str] = re.compile(
    r"\[@(?P<linked>[A-Za-z0-9][A-Za-z0-9\-\[\]]*)\]\([^)]*\)|(?<![\w/])@(?P<plain>[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?)"
)
_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{7,40}$")
_CODE_SHA_RE: re.Pattern[str] = re.compile(r"`(?P<sha>[0-9a-f]{7,40})`")
# Where an entry's parenthesized details begin: "([#12](url)", "(#12", "(by " or "(`abc1234`".
_DETAILS_RE: re.Pattern[str] = re.compile(r"\(\s*(?:\[#\d+\]\([^)]*\)|#\d+|by\s|`[0-9a-f]{7,40}`)")


class ChangelogGenerationError(release_notes.ReleaseNotesError):
    """The releases could not be fetched or the output could not be written."""


def _get_json(url: str, token: str | None) -> Any:
    """GET ``url`` from the GitHub API and return the decoded JSON body."""
    if not url.startswith(f"{API_ROOT}/"):
        msg: str = f"refusing to call a non-GitHub URL: {url}"
        raise ChangelogGenerationError(msg)
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mpg-generate-changelog",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request: urllib.request.Request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        msg = f"GitHub returned HTTP {exc.code} for {url}"
        raise ChangelogGenerationError(msg) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        msg = f"could not fetch {url}: {exc}"
        raise ChangelogGenerationError(msg) from exc


def fetch_releases(repository: str, token: str | None, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` published (non-draft) releases of ``repository``, newest first as GitHub orders them."""
    if not _REPO_RE.match(repository):
        msg: str = f"repository must look like owner/name, got {repository!r}"
        raise ChangelogGenerationError(msg)
    releases: list[dict[str, Any]] = []
    page: int = 1
    while len(releases) < limit:
        url: str = f"{API_ROOT}/repos/{repository}/releases?per_page={PAGE_SIZE}&page={page}"
        batch: Any = _get_json(url, token)
        if not isinstance(batch, list):
            msg = f"unexpected response from {url}: expected a JSON list"
            raise ChangelogGenerationError(msg)
        rows: list[Any] = cast(list[Any], batch)
        releases.extend(
            cast(dict[str, Any], row)
            for row in rows
            if isinstance(row, dict) and not cast(dict[str, Any], row).get("draft")
        )
        if len(rows) < PAGE_SIZE:
            break
        page += 1
    return releases[:limit]


def normalize_release(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a GitHub release object to the fields the Changelog page uses; None if it has no tag."""
    tag: Any = raw.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    tag = tag.strip()
    name: Any = raw.get("name")
    published: Any = raw.get("published_at") or raw.get("created_at")
    url: Any = raw.get("html_url")
    body: Any = raw.get("body")
    author: Any = raw.get("author")
    released_by: Any = cast(dict[str, Any], author).get("login") if isinstance(author, dict) else None
    body_text: str = body if isinstance(body, str) else ""
    intro, sections = parse_release_body(body_text)
    return {
        "tag": tag,
        "version": _LEADING_V_RE.sub("", tag),
        "name": name.strip() if isinstance(name, str) and name.strip() else tag,
        "published_at": published if isinstance(published, str) else "",
        "prerelease": bool(raw.get("prerelease")),
        "url": url if isinstance(url, str) else "",
        "released_by": released_by if isinstance(released_by, str) else "",
        "commit": "",
        "intro": intro,
        "sections": sections,
        "body": body_text,
    }


def build_document(
    repository: str,
    releases: list[dict[str, Any]],
    version: str,
    commit: str,
    now: datetime,
    tag_commit: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Assemble the changelog.json document; ``tag_commit`` maps a tag to the commit it points at (abbreviated here)."""
    normalized: list[dict[str, Any]] = [
        entry for entry in (normalize_release(raw) for raw in releases) if entry is not None
    ]
    if tag_commit is not None:
        for entry in normalized:
            full: str | None = tag_commit(entry["tag"])
            entry["commit"] = full[:SHORT_SHA_LENGTH] if full else ""
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "repository": repository,
        "build": {"version": version, "commit": commit},
        "releases": normalized,
    }


def _plain(text: str) -> str:
    """A heading's text without emphasis markers, so ``**Features**`` and ``Features`` compare equal."""
    return text.replace("*", "").replace("_", " ").strip()


def parse_entry(text: str) -> dict[str, Any] | None:
    """
    Split one release-note bullet into ``{"title", "number", "authors"}`` (plus ``"sha"`` when the
    line carries a commit hash in backticks).

    The details in parentheses are all optional, so every form this project's notes use is understood:
    ``Title ([#12](url) by [@alice](url), `abc1234`)`` (a squash-merged pull request),
    ``Title (by [@alice](url), `abc1234`)`` and ``Title (`abc1234`)`` (a direct commit),
    ``Title (#12) @alice`` (earlier notes) and a bare ``Title``. Returns None for an empty bullet.
    """
    details: re.Match[str] | None = _DETAILS_RE.search(text)
    if details is None:
        bare: str = text.strip()
        return {"title": bare, "number": None, "authors": []} if bare else None
    title: str = text[: details.start()].strip()
    if not title:
        return None
    tail: str = text[details.start():]
    authors: list[str] = []
    for mention in _MENTION_RE.finditer(tail):
        login: str = mention.group("linked") or mention.group("plain")
        if login not in authors:
            authors.append(login)
    reference: re.Match[str] | None = _PR_REF_RE.search(tail)
    number: int | None = int(reference.group("linked") or reference.group("plain")) if reference else None
    result: dict[str, Any] = {"title": title, "number": number, "authors": authors}
    code_sha: re.Match[str] | None = _CODE_SHA_RE.search(tail)
    if code_sha is not None:
        result["sha"] = code_sha.group("sha")[:SHORT_SHA_LENGTH]
    return result


def parse_release_body(body: str) -> tuple[str, list[dict[str, Any]]]:
    """
    Turn a release's Markdown notes into ``(intro, sections)``.

    A heading starts a section, except a heading that is just the version (``## v1.2.0``), which
    is skipped. Bullets become entries of the current section (a bullet before any heading goes
    into a section called "Changes"). Text before the first section is the ``intro``. HTML lines
    and the "Detailed Changes" footer are ignored; the page computes its own compare link.
    A body with no recognizable entries gives no sections (the page then shows the body as text).
    """
    intro_lines: list[str] = []
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in body.splitlines():
        line: str = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("<"):
            continue
        heading: re.Match[str] | None = _HEADING_RE.match(line)
        if heading is not None:
            text: str = _plain(heading.group("text"))
            if _VERSION_HEADING_RE.match(text):
                current = None
                continue
            current = {"title": text, "entries": []}
            sections.append(current)
            continue
        bullet: re.Match[str] | None = _BULLET_RE.match(line)
        if bullet is not None:
            entry: dict[str, Any] | None = parse_entry(bullet.group("text"))
            if entry is None:
                continue
            if current is None:
                current = next((s for s in sections if s["title"] == "Changes"), None)
                if current is None:
                    current = {"title": "Changes", "entries": []}
                    sections.append(current)
            current["entries"].append(entry)
            continue
        if not sections:
            intro_lines.append(line.strip())

    populated: list[dict[str, Any]] = [s for s in sections if s["entries"]]
    return ("\n".join(intro_lines), populated)


def enrich_entries(
    repository: str,
    releases: list[dict[str, Any]],
    token: str | None,
    get_json: Callable[[str, str | None], Any] | None = None,
) -> int:
    """
    Add ``sha`` (the merge commit) and the pull request author to entries that have a PR number but no hash.

    Notes written by ``tools/release_notes.py`` already carry hashes and authors; this is for older notes
    (for example ones written by hand on GitHub). Each pull request is looked up once through the GitHub API. A failed lookup only leaves that entry without a sha; after
    ``MAX_CONSECUTIVE_LOOKUP_FAILURES`` failures in a row (for example the rate limit) the
    remaining lookups are skipped. Returns the number of entries that were enriched.
    """
    fetch: Callable[[str, str | None], Any] = get_json or _get_json
    cache: dict[int, dict[str, Any] | None] = {}
    failures: int = 0
    enriched: int = 0
    for release in releases:
        for section in release.get("sections", []):
            for entry in section["entries"]:
                number: int | None = entry.get("number")
                if number is None or entry.get("sha"):
                    continue  # no pull request to look up, or the notes already carry the hash
                if number not in cache:
                    cache[number] = None
                    if failures < MAX_CONSECUTIVE_LOOKUP_FAILURES:
                        try:
                            data: Any = fetch(f"{API_ROOT}/repos/{repository}/pulls/{number}", token)
                        except ChangelogGenerationError as exc:
                            failures += 1
                            _log.warning("Could not look up pull request #%d: %s", number, exc)
                        else:
                            failures = 0
                            if isinstance(data, dict):
                                cache[number] = cast(dict[str, Any], data)
                details: dict[str, Any] | None = cache[number]
                if details is None:
                    continue
                merge_sha: Any = details.get("merge_commit_sha")
                if isinstance(merge_sha, str) and _HEX_RE.match(merge_sha):
                    entry["sha"] = merge_sha[:SHORT_SHA_LENGTH]
                user: Any = details.get("user")
                login: Any = cast(dict[str, Any], user).get("login") if isinstance(user, dict) else None
                if isinstance(login, str) and login:
                    authors: list[str] = entry["authors"]
                    if login in authors:
                        authors.remove(login)
                    authors.insert(0, login)  # the pull request's author comes first
                enriched += 1
    return enriched


def build_unreleased(
    root: Path,
    repository: str,
    token: str | None,
    *,
    enrich: bool,
    get_json: Callable[[str, str | None], Any] | None = None,
) -> dict[str, Any] | None:
    """
    The changes made since the last release tag, in the structure stored under ``"unreleased"``.

    Uses the same engine as the Release workflow (``tools/release_notes.py``), so the Changelog page of an image
    built from ``TSDB_dev`` can show what the next release will contain, and which version it will have.
    Returns None when there is no release tag yet, no git history, or nothing releasable after the last tag.
    """
    previous_tag: str | None = release_notes.latest_release_tag(root, "HEAD")
    if previous_tag is None:
        return None
    try:
        changes: list[release_notes.Change] = release_notes.collect_changes(root, f"{previous_tag}..HEAD")
    except release_notes.ReleaseNotesError:
        return None
    if not changes:
        return None
    if enrich:
        release_notes.enrich_authors(repository, changes, token, get_json or _get_json)
    bump: str = release_notes.highest_bump(changes) or "minor"
    previous: release_notes.Version | None = release_notes.parse_stable(previous_tag)
    head: str = release_notes.run_git(root, ["rev-parse", "HEAD"]) or ""
    return {
        "previous_tag": previous_tag,
        "projected_version": release_notes.format_version(release_notes.next_version(previous, bump)),
        "bump": bump,
        "commit": head[:SHORT_SHA_LENGTH],
        "sections": release_notes.to_json_sections(release_notes.group_changes(changes)),
    }


def docker_safe_tag(version: str) -> str:
    """A Docker image tag for ``version``; Docker forbids ``+``, so ``1.1.10+5.gabc`` becomes ``1.1.10-5.gabc``."""
    return _DOCKER_TAG_UNSAFE_RE.sub("-", version)


def write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        Path(temp_name).replace(path)
    except OSError as exc:
        Path(temp_name).unlink(missing_ok=True)
        msg: str = f"could not write {path}: {exc}"
        raise ChangelogGenerationError(msg) from exc


def write_github_outputs(version: str, commit: str) -> None:
    """Append ``version``, ``image_tag`` and ``commit`` to the file named by ``$GITHUB_OUTPUT`` (GitHub Actions)."""
    target: str = os.environ.get("GITHUB_OUTPUT", "")
    if not target:
        _log.warning("--github-output was given but GITHUB_OUTPUT is not set; skipping")
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(f"version={version}\n")
        stream.write(f"image_tag={docker_safe_tag(version)}\n")
        stream.write(f"commit={commit}\n")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Generate classes/WebServer/changelog.json from published GitHub Releases."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY,
        help=f"owner/name of the repository (default: $GITHUB_REPOSITORY or {DEFAULT_REPOSITORY})",
    )
    parser.add_argument("--output", type=Path, default=BUILD_INFO_PATH, help="file to write")
    parser.add_argument("--max-releases", type=int, default=200, help="how many releases to keep")
    parser.add_argument("--no-enrich", action="store_true", help="skip the per-pull-request API lookups (no commit hashes)")
    parser.add_argument("--no-unreleased", action="store_true", help="leave out the section for commits made since the last release")
    parser.add_argument("--dry-run", action="store_true", help="print the document instead of writing it")
    parser.add_argument("--github-output", action="store_true", help="set version/image_tag/commit step outputs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args: argparse.Namespace = parse_args(argv)
    token: str | None = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    version: str = git_version(PROJECT_ROOT) or os.environ.get("MPG_VERSION") or UNKNOWN_VERSION
    commit: str = git_commit(PROJECT_ROOT) or os.environ.get("GITHUB_SHA", "") or "unknown"

    try:
        raw_releases: list[dict[str, Any]] = fetch_releases(args.repo, token, args.max_releases)
        document: dict[str, Any] = build_document(
            args.repo, raw_releases, version, commit, datetime.now(timezone.utc), tag_commit=git_tag_commit
        )
        if not args.no_enrich:
            enriched: int = enrich_entries(args.repo, document["releases"], token)
            _log.info("Looked up %d pull request(s) for commit hashes and authors", enriched)
        if not args.no_unreleased:
            unreleased: dict[str, Any] | None = build_unreleased(PROJECT_ROOT, args.repo, token, enrich=not args.no_enrich)
            if unreleased is not None:
                document["unreleased"] = unreleased
        text: str = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if args.dry_run:
            sys.stdout.write(text)
        else:
            write_atomically(args.output, text)
    except ChangelogGenerationError as exc:
        _log.error("generate_changelog: %s", exc)  # noqa: TRY400
        return 1

    count: int = len(document["releases"])
    if not args.dry_run:
        _log.info("Wrote %d release(s) for %s (build version %s) to %s", count, args.repo, version, args.output)
    if count == 0:
        _log.warning("No published releases found. Publish a GitHub Release (see documentation/usage/releasing.md).")
    if args.github_output:
        write_github_outputs(version, commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
