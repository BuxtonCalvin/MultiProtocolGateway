# Description: services/changelog_service.py — loads the release history shown on the admin UI's Changelog page.
# File: changelog_service.py
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

"""services/changelog_service.py

Loads the release history shown on the admin UI's Changelog page
(Global Settings -> Changelog) from ``classes/WebServer/changelog.json``.

That file is *generated*, not hand-edited: ``tools/generate_changelog.py`` copies the
published GitHub Releases into it at build time (version tag, date, who released it, the commit,
and the release-drafter notes split into sections and entries, each with its pull request number,
authors and commit hash). Releases are therefore the single source of truth, and the version
history on this page is always the version history in git. The file is gitignored and ships
inside the Docker image, because a running container has neither ``.git`` nor guaranteed network access.

The page is a four-level outline, every level collapsible::

    Version 2.x                      (a series: all releases of one major version)
      Version 2.1.0                  (a release: tag, commit, released by, compare link)
        Features                     (a section of the release notes)
          Add X (#12 by @alice, abc1234)     (an entry: linked PR number, author and commit)

Everything that becomes a link (pull request, author, commit, compare) is *built here* from a
validated number, login or hash and the repository name. Nothing from the file is ever used as a
URL, and release-note text is treated as untrusted: it is HTML-escaped (only inline code is kept).

The file is re-read automatically when it changes on disk, so regenerating it shows up
without a restart. Files written by older versions of the generator (schema 1: no sections) still
load; their releases are shown as rendered notes text.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from os import stat_result
from pathlib import Path
from typing import Any, cast

import markdown as markdown_lib

_log: logging.Logger = logging.getLogger(__name__)

CHANGELOG_PATH: Path = Path(__file__).resolve().parents[1] / "changelog.json"
DEFAULT_REPOSITORY: str = "BuxtonCalvin/MultiProtocolGateway"
GITHUB: str = "https://github.com"
SHORT_SHA_LENGTH: int = 7
OTHER_SERIES_LABEL: str = "Other"

_SEMVER_RE: re.Pattern[str] = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_DATE_RE: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}")
_REPO_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_LOGIN_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?(?:\[bot\])?$")
_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{7,40}$")
_INLINE_CODE_RE: re.Pattern[str] = re.compile(r"`([^`]+)`")
# A bare GitHub URL that is not already part of a Markdown link, <autolink> or attribute.
_BARE_GITHUB_URL_RE: re.Pattern[str] = re.compile(r"(?<![(\[<\"'=/\w])https://github\.com/[^\s<>()\[\]\"']+")
# "@alice" -> a link to her GitHub profile. Not matched inside words/emails (a@b.com), inside an
# existing Markdown link, or after a backtick. GitHub logins are 1-39 chars, no leading/trailing hyphen.
_MENTION_RE: re.Pattern[str] = re.compile(r"(?<![\w@/\[`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)(?![\w@/\]-])")
_UNSAFE_HREF_RE: re.Pattern[str] = re.compile(r"""href\s*=\s*(["'])\s*(?:javascript|data|vbscript):[^"']*\1""", re.IGNORECASE)


class ChangelogLoadError(ValueError):
    """changelog.json is missing, is not valid JSON, or does not follow the documented format."""


@dataclass(frozen=True)
class ChangelogAuthor:
    """A GitHub account credited for an entry or a release; ``url`` is always a github.com profile link."""

    login: str
    url: str


@dataclass(frozen=True)
class ChangelogEntry:
    """One change (normally one merged pull request)."""

    title_html: str                 # escaped text; only `inline code` becomes <code>
    number: int | None              # pull request number
    pr_url: str                     # "" when there is no number
    authors: tuple[ChangelogAuthor, ...]
    sha: str                        # abbreviated commit hash, "" when unknown
    commit_url: str                 # "" when there is no sha


@dataclass(frozen=True)
class ChangelogSection:
    """A heading of the release notes ("Features", "Bug Fixes", ...) and its entries."""

    title: str
    entries: tuple[ChangelogEntry, ...]


@dataclass(frozen=True)
class ChangelogRelease:
    """One published release with its notes, already split into safe, linkable parts."""

    version: str            # "1.1.0" (the tag without a leading "v")
    tag: str                # "v1.1.0"
    name: str               # release title, normally the tag
    published: str          # "YYYY-MM-DD", or "" when unknown
    prerelease: bool
    url: str                # link to the release on GitHub, or "" when not a valid https URL
    released_by: ChangelogAuthor | None
    commit: str             # abbreviated commit hash of the tag, "" when unknown
    commit_url: str
    compare_url: str        # compare link against the previous release, "" for the oldest one
    previous_tag: str       # tag the compare link starts from
    intro_html: str         # sanitized HTML for text that precedes the first section
    sections: tuple[ChangelogSection, ...]
    notes_html: str         # the whole body as sanitized HTML; used when there are no sections
    entry_count: int


@dataclass(frozen=True)
class ChangelogSeries:
    """All releases that share a major version (``2.x``)."""

    label: str
    releases: tuple[ChangelogRelease, ...]  # newest first
    entry_count: int


@dataclass(frozen=True)
class Changelog:
    """Everything the Changelog page needs."""

    repository: str                         # "owner/name"
    releases_url: str                       # link to the repository's Releases page
    generated_at: str                       # ISO timestamp of when the file was generated
    build_version: str                      # version recorded when the file was generated, "" if none
    releases: tuple[ChangelogRelease, ...]  # newest version first
    series: tuple[ChangelogSeries, ...]     # the same releases grouped by major version, newest first


def render_notes(body: str) -> str:
    """
    Render release-note Markdown to HTML that is safe to place in a page.

    Raw HTML in the notes is escaped (it would otherwise be passed through by the Markdown
    renderer), bare github.com URLs and ``@username`` mentions (contributor credits added by
    release-drafter) become links, ``javascript:``/``data:``/``vbscript:``
    links are neutralized, and every link opens in a new tab without leaking the referrer.
    """
    if not body.strip():
        return ""
    escaped: str = html.escape(body, quote=False)
    linked: str = _BARE_GITHUB_URL_RE.sub(lambda m: f"<{m.group(0)}>", escaped)
    linked = _MENTION_RE.sub(lambda m: f"[@{m.group(1)}](https://github.com/{m.group(1)})", linked)
    rendered: str = markdown_lib.markdown(
        linked, extensions=["fenced_code", "sane_lists"], output_format="html"
    )
    rendered = _UNSAFE_HREF_RE.sub('href="#"', rendered)
    return rendered.replace("<a href=", '<a target="_blank" rel="noopener noreferrer" href=')


def inline_html(text: str) -> str:
    """Escape ``text`` for HTML and turn `backtick` spans into ``<code>``. Nothing else is interpreted."""
    return _INLINE_CODE_RE.sub(r"<code>\1</code>", html.escape(text, quote=True))


def author_for(login: Any) -> ChangelogAuthor | None:
    """A validated author with a github.com profile link, or None if ``login`` is not a plausible GitHub login."""
    if not isinstance(login, str) or not _LOGIN_RE.match(login):
        return None
    if login.endswith("[bot]"):
        return ChangelogAuthor(login=login, url=f"{GITHUB}/apps/{login.removesuffix('[bot]')}")
    return ChangelogAuthor(login=login, url=f"{GITHUB}/{login}")


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg: str = f"{where} must be a non-empty string"
        raise ChangelogLoadError(msg)
    return value.strip()


def _optional_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _semver_key(version: str) -> tuple[int, ...]:
    """Sort key for a version string; anything that is not MAJOR.MINOR.PATCH sorts last."""
    match: re.Match[str] | None = _SEMVER_RE.match(version)
    if match is None:
        return (-1,)
    return tuple(int(part) for part in match.groups())


def _short_sha(value: Any) -> str:
    """The abbreviated form of a commit hash, or "" if ``value`` is not a hex hash."""
    if isinstance(value, str) and _HEX_RE.match(value):
        return value[:SHORT_SHA_LENGTH]
    return ""


def _parse_entry(raw: Any, repository: str, where: str) -> ChangelogEntry:
    if not isinstance(raw, dict):
        msg: str = f"{where} must be an object"
        raise ChangelogLoadError(msg)
    item: dict[str, Any] = cast(dict[str, Any], raw)
    title: str = _require_str(item.get("title"), f"{where}.title")
    raw_number: Any = item.get("number")
    number: int | None = raw_number if isinstance(raw_number, int) and not isinstance(raw_number, bool) and raw_number > 0 else None
    raw_authors: Any = item.get("authors")
    authors: list[ChangelogAuthor] = []
    if isinstance(raw_authors, list):
        for login in cast(list[Any], raw_authors):
            author: ChangelogAuthor | None = author_for(login)
            if author is not None and author not in authors:
                authors.append(author)
    sha: str = _short_sha(item.get("sha"))
    return ChangelogEntry(
        title_html=inline_html(title),
        number=number,
        pr_url=f"{GITHUB}/{repository}/pull/{number}" if number is not None else "",
        authors=tuple(authors),
        sha=sha,
        commit_url=f"{GITHUB}/{repository}/commit/{sha}" if sha else "",
    )


def _parse_sections(raw: Any, repository: str, where: str) -> tuple[ChangelogSection, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg: str = f"{where} must be a list"
        raise ChangelogLoadError(msg)
    sections: list[ChangelogSection] = []
    for s_index, raw_section in enumerate(cast(list[Any], raw), start=1):
        s_where: str = f"{where}[{s_index}]"
        if not isinstance(raw_section, dict):
            msg = f"{s_where} must be an object"
            raise ChangelogLoadError(msg)
        section: dict[str, Any] = cast(dict[str, Any], raw_section)
        raw_entries: Any = section.get("entries")
        if not isinstance(raw_entries, list):
            msg = f"{s_where}.entries must be a list"
            raise ChangelogLoadError(msg)
        entries: tuple[ChangelogEntry, ...] = tuple(
            _parse_entry(entry, repository, f"{s_where}.entries[{e_index}]")
            for e_index, entry in enumerate(cast(list[Any], raw_entries), start=1)
        )
        sections.append(ChangelogSection(title=_require_str(section.get("title"), f"{s_where}.title"), entries=entries))
    return tuple(sections)


def _parse_release(raw: Any, repository: str, where: str) -> ChangelogRelease:
    if not isinstance(raw, dict):
        msg: str = f"{where} must be an object"
        raise ChangelogLoadError(msg)
    item: dict[str, Any] = cast(dict[str, Any], raw)
    tag: str = _require_str(item.get("tag"), f"{where}.tag")
    version: str = _optional_str(item.get("version")) or tag
    url: str = _optional_str(item.get("url"))
    published: str = _optional_str(item.get("published_at"))
    date_match: re.Match[str] | None = _DATE_RE.match(published)
    sections: tuple[ChangelogSection, ...] = _parse_sections(item.get("sections"), repository, f"{where}.sections")
    commit: str = _short_sha(item.get("commit"))
    return ChangelogRelease(
        version=version,
        tag=tag,
        name=_optional_str(item.get("name")) or tag,
        published=date_match.group(0) if date_match else "",
        prerelease=bool(item.get("prerelease")),
        url=url if url.startswith("https://") else "",
        released_by=author_for(_optional_str(item.get("released_by"))),
        commit=commit,
        commit_url=f"{GITHUB}/{repository}/commit/{commit}" if commit else "",
        compare_url="",
        previous_tag="",
        intro_html=render_notes(_optional_str(item.get("intro"))),
        sections=sections,
        notes_html=render_notes(_optional_str(item.get("body"))),
        entry_count=sum(len(section.entries) for section in sections),
    )


def _series_label(version: str) -> str:
    """``"2.x"`` for a MAJOR.MINOR.PATCH version, ``"Other"`` for anything else."""
    key: tuple[int, ...] = _semver_key(version)
    return f"{key[0]}.x" if key[0] >= 0 else OTHER_SERIES_LABEL


def parse_changelog(data: Any) -> Changelog:
    """Validate the parsed JSON and turn it into a Changelog. Raises ChangelogLoadError with a precise location."""
    if not isinstance(data, dict):
        msg: str = "top level must be an object"
        raise ChangelogLoadError(msg)
    top: dict[str, Any] = cast(dict[str, Any], data)
    raw_releases: Any = top.get("releases")
    if not isinstance(raw_releases, list):
        msg = 'top level must contain a "releases" list'
        raise ChangelogLoadError(msg)

    repository: str = _optional_str(top.get("repository"))
    if not _REPO_RE.match(repository):
        repository = DEFAULT_REPOSITORY

    releases: list[ChangelogRelease] = []
    seen_tags: set[str] = set()
    for index, raw in enumerate(cast(list[Any], raw_releases), start=1):
        release: ChangelogRelease = _parse_release(raw, repository, f"releases[{index}]")
        if release.tag in seen_tags:
            msg = f"releases[{index}].tag {release.tag!r} is used more than once"
            raise ChangelogLoadError(msg)
        seen_tags.add(release.tag)
        releases.append(release)
    releases.sort(key=lambda r: (_semver_key(r.version), r.published), reverse=True)

    # Each release compares against the next-older one (the list is newest first). The tag goes into a URL,
    # so it must look like a tag name; otherwise the link is left out.
    linked: list[ChangelogRelease] = []
    for position, release in enumerate(releases):
        older: ChangelogRelease | None = releases[position + 1] if position + 1 < len(releases) else None
        if older is not None and _TAG_RE.match(older.tag) and _TAG_RE.match(release.tag):
            release = replace(
                release,
                previous_tag=older.tag,
                compare_url=f"{GITHUB}/{repository}/compare/{older.tag}...{release.tag}",
            )
        linked.append(release)

    grouped: dict[str, list[ChangelogRelease]] = {}
    for release in linked:  # already newest first, so insertion order keeps the newest series first
        grouped.setdefault(_series_label(release.version), []).append(release)
    series: tuple[ChangelogSeries, ...] = tuple(
        ChangelogSeries(label=label, releases=tuple(members), entry_count=sum(r.entry_count for r in members))
        for label, members in grouped.items()
    )

    raw_build: Any = top.get("build")
    build_version: str = (
        _optional_str(cast(dict[str, Any], raw_build).get("version")) if isinstance(raw_build, dict) else ""
    )
    return Changelog(
        repository=repository,
        releases_url=f"{GITHUB}/{repository}/releases",
        generated_at=_optional_str(top.get("generated_at")),
        build_version=build_version,
        releases=tuple(linked),
        series=series,
    )


_cache_lock: threading.Lock = threading.Lock()
_cache: dict[Path, tuple[tuple[int, int], Changelog]] = {}


def load_changelog(path: Path = CHANGELOG_PATH) -> Changelog:
    """
    The release history, parsed and rendered. Cached per file and refreshed when the file's
    modification time or size changes.

    Raises ChangelogLoadError if the file is missing or invalid; the page shows that message
    instead of failing.
    """
    try:
        stat: stat_result = path.stat()
    except OSError as exc:
        msg: str = (
            f"{path.name} was not found. It is generated at build time by "
            "tools/generate_changelog.py; run that tool, or use the Docker image, to get the history."
        )
        raise ChangelogLoadError(msg) from exc
    signature: tuple[int, int] = (stat.st_mtime_ns, stat.st_size)

    with _cache_lock:
        cached: tuple[tuple[int, int], Changelog] | None = _cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"{path.name} is not valid JSON: {exc}"
            raise ChangelogLoadError(msg) from exc
        changelog: Changelog = parse_changelog(data)
        _cache[path] = (signature, changelog)
        _log.debug("Changelog loaded: %d release(s)", len(changelog.releases))
        return changelog
