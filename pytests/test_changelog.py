# Description: Tests for the Changelog page: version resolution, the changelog.json loader, release-note sanitizing, the page template, and the generator tools.
# File: test_changelog.py
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

"""Tests for the Changelog feature (no running app and no network needed)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from classes.WebServer.services import changelog_service
from classes.WebServer.services.changelog_service import (
    Changelog,
    ChangelogEntry,
    ChangelogLoadError,
    ChangelogRelease,
    ChangelogSection,
    load_changelog,
    parse_changelog,
    render_notes,
)
from defs import app_version as av

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
TEMPLATES: Path = PROJECT_ROOT / "classes" / "WebServer" / "templates"
REPOSITORY: str = "BuxtonCalvin/MultiProtocolGateway"
REPO_URL: str = f"https://github.com/{REPOSITORY}"


def _load_tool(name: str) -> ModuleType:
    """Import tools/<name>.py (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "tools" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen: ModuleType = _load_tool("generate_changelog")
uvf: ModuleType = _load_tool("update_version_file")


# --------------------------------------------------------------------------- small typed helpers
def _returns(value: str | None) -> Callable[..., str | None]:
    """A stand-in for a version resolver (any arguments) that always answers ``value``."""

    def resolver(*_args: object, **_kwargs: object) -> str | None:
        return value

    return resolver


def _releases_returning(releases: list[dict[str, Any]]) -> Callable[[str, str | None, int], list[dict[str, Any]]]:
    """A stand-in for ``fetch_releases`` that returns ``releases`` without touching the network."""

    def fetch(repository: str, token: str | None, limit: int) -> list[dict[str, Any]]:
        return releases

    return fetch


def _tags_returning(tags: list[str]) -> Callable[[], list[str]]:
    """A stand-in for ``list_tags`` that returns ``tags`` without running git."""

    def list_tags() -> list[str]:
        return tags

    return list_tags


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_is_installed() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0  # noqa: S603, S607


def _doc(**overrides: Any) -> dict[str, Any]:
    """A schema-1 style document: releases carry only a Markdown ``body`` and no ``sections``."""
    data: dict[str, Any] = {
        "schema": 1,
        "generated_at": "2026-11-21T10:00:00+00:00",
        "repository": REPOSITORY,
        "build": {"version": "1.1.10+2.gabc1234", "commit": "abc"},
        "releases": [
            {"tag": "v1.1.9", "version": "1.1.9", "name": "v1.1.9", "published_at": "2026-10-01T00:00:00Z",
             "prerelease": False, "url": "https://github.com/x/y/releases/tag/v1.1.9", "body": "- fixed a thing"},
            {"tag": "v1.1.10", "version": "1.1.10", "name": "v1.1.10", "published_at": "2026-11-01T00:00:00Z",
             "prerelease": False, "url": "https://github.com/x/y/releases/tag/v1.1.10", "body": "## Features\n- new page"},
            {"tag": "v1.1.2", "version": "1.1.2", "name": "v1.1.2", "published_at": "2026-09-01T00:00:00Z",
             "prerelease": True, "url": "", "body": ""},
        ],
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- version formatting
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v1.1.10-0-gabc1234", "1.1.10"),
        ("v1.1.10-5-gabc1234", "1.1.10+5.gabc1234"),
        ("v1.1.10-5-gabc1234-dirty", "1.1.10+5.gabc1234.dirty"),
        ("v1.1.10-0-gabc1234-dirty", "1.1.10+dirty"),
        ("v1.2.0-rc1-3-gabc1234", "1.2.0-rc1+3.gabc1234"),
        ("1.1.10-1-gabc1234", "1.1.10+1.gabc1234"),
        ("abc1234", "0.0.0+gabc1234"),
        ("abc1234-dirty", "0.0.0+gabc1234.dirty"),
        ("", "unknown"),
        ("fatal: not a git repository", "unknown"),
    ],
)
def test_format_git_describe(raw: str, expected: str) -> None:
    assert av.format_git_describe(raw) == expected


def test_base_version_and_development_build() -> None:
    assert av.base_version("1.1.10+5.gabc1234") == "1.1.10"
    assert av.base_version("1.1.10") == "1.1.10"
    assert av.is_development_build("1.1.10+5.gabc1234") is True
    assert av.is_development_build("1.1.10") is False


# --------------------------------------------------------------------------- version lookup order
@pytest.fixture
def clean_version_cache() -> Iterator[None]:
    av.app_version.cache_clear()
    yield
    av.app_version.cache_clear()


@pytest.mark.usefixtures("clean_version_cache")
def test_env_var_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(av.VERSION_ENV_VAR, "9.9.9")
    assert av.app_version() == "9.9.9"


@pytest.mark.usefixtures("clean_version_cache")
def test_env_var_unknown_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(av.VERSION_ENV_VAR, "unknown")
    monkeypatch.setattr(av, "git_version", _returns("2.0.0"))
    assert av.app_version() == "2.0.0"


@pytest.mark.usefixtures("clean_version_cache")
def test_falls_through_to_build_info_then_version_file_then_metadata_then_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(av.VERSION_ENV_VAR, raising=False)
    monkeypatch.setattr(av, "git_version", _returns(None))
    monkeypatch.setattr(av, "build_info_version", _returns("3.0.0+1.gabc"))
    assert av.app_version() == "3.0.0+1.gabc"
    av.app_version.cache_clear()
    monkeypatch.setattr(av, "build_info_version", _returns(None))
    monkeypatch.setattr(av, "version_file_version", _returns("3.5.0+src"))
    assert av.app_version() == "3.5.0+src"  # a source zip: the VERSION file, marked as a snapshot
    av.app_version.cache_clear()
    monkeypatch.setattr(av, "version_file_version", _returns(None))
    monkeypatch.setattr(av, "_metadata_version", _returns("4.0.0"))
    assert av.app_version() == "4.0.0"
    av.app_version.cache_clear()
    monkeypatch.setattr(av, "_metadata_version", _returns(None))
    assert av.app_version() == av.UNKNOWN_VERSION


def test_build_info_version_reads_the_generated_file(tmp_path: Path) -> None:
    good: Path = tmp_path / "changelog.json"
    good.write_text(json.dumps(_doc()), encoding="utf-8")
    assert av.build_info_version(good) == "1.1.10+2.gabc1234"
    assert av.build_info_version(tmp_path / "missing.json") is None
    bad: Path = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert av.build_info_version(bad) is None
    unknown: Path = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"build": {"version": "unknown"}}), encoding="utf-8")
    assert av.build_info_version(unknown) is None


def test_version_file_version_reads_and_marks_a_source_snapshot(tmp_path: Path) -> None:
    version_file: Path = tmp_path / "VERSION"
    version_file.write_text("2.0.0\n", encoding="utf-8")
    assert av.version_file_version(version_file) == "2.0.0+src"
    assert av.is_development_build("2.0.0+src") is True
    assert av.base_version("2.0.0+src") == "2.0.0"
    for bad_text in ("", "2.0", "v2.0.0", "2.0.0-rc1", "junk"):
        version_file.write_text(bad_text, encoding="utf-8")
        assert av.version_file_version(version_file) is None, bad_text
    assert av.version_file_version(tmp_path / "missing") is None


def test_shipped_version_file_is_valid() -> None:
    assert av.version_file_version() is not None, "VERSION must contain a single MAJOR.MINOR.PATCH line"


def test_git_version_follows_tags_and_commits(tmp_path: Path) -> None:
    """Exercises the real `git describe` path in a throwaway repository."""
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    assert re.fullmatch(r"0\.0\.0\+g[0-9a-f]+", av.git_version(tmp_path) or "")  # no tag yet
    _git(tmp_path, "tag", "v1.1.10")
    assert av.git_version(tmp_path) == "1.1.10"
    (tmp_path / "a.txt").write_text("2", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "second")
    assert re.fullmatch(r"1\.1\.10\+1\.g[0-9a-f]+", av.git_version(tmp_path) or "")
    (tmp_path / "a.txt").write_text("3", encoding="utf-8")
    assert (av.git_version(tmp_path) or "").endswith(".dirty")
    assert re.fullmatch(r"[0-9a-f]{40}", av.git_commit(tmp_path) or "")


def test_git_version_is_none_outside_a_checkout(tmp_path: Path) -> None:
    assert av.git_version(tmp_path) is None
    assert av.git_commit(tmp_path) is None


def test_git_tag_commit_resolves_a_tag_in_a_real_repository(tmp_path: Path) -> None:
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    _git(tmp_path, "tag", "v1.0.0")
    assert re.fullmatch(r"[0-9a-f]{40}", av.git_tag_commit("v1.0.0", tmp_path) or "")
    assert av.git_tag_commit("v9.9.9", tmp_path) is None
    assert av.git_tag_commit("--all", tmp_path) is None  # never read as a git option
    assert av.git_tag_commit("", tmp_path) is None


# --------------------------------------------------------------------------- release-note rendering
def test_render_notes_renders_markdown() -> None:
    out: str = render_notes("## Features\n- one\n- two")
    assert "<h2>Features</h2>" in out
    assert "<li>one</li>" in out


def test_render_notes_escapes_raw_html_and_neutralizes_bad_links() -> None:
    out: str = render_notes("<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://example.com)")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "javascript:" not in out.lower()
    assert 'href="https://example.com"' in out
    assert 'rel="noopener noreferrer"' in out


def test_render_notes_links_bare_github_urls_once() -> None:
    out: str = render_notes(
        "* Add thing by @bob in https://github.com/o/r/pull/12\n\n**Full Changelog**: https://github.com/o/r/compare/v1...v2"
    )
    assert out.count('href="https://github.com/o/r/pull/12"') == 1
    assert 'href="https://github.com/o/r/compare/v1...v2"' in out


def test_render_notes_links_contributor_mentions_but_not_emails() -> None:
    out: str = render_notes("* fix: a thing (#12) @alice, @bob-2\n\nwrite to dev@example.com")
    assert 'href="https://github.com/alice"' in out
    assert ">@alice<" in out
    assert 'href="https://github.com/bob-2"' in out
    assert "https://github.com/example" not in out  # the email address was left alone
    assert "dev@example.com" in out


def test_render_notes_blank_is_empty() -> None:
    assert render_notes("") == ""
    assert render_notes("  \n ") == ""


# --------------------------------------------------------------------------- parsing and validation
def test_parse_sorts_newest_version_first_by_semver_not_text() -> None:
    cl: Changelog = parse_changelog(_doc())
    assert [r.version for r in cl.releases] == ["1.1.10", "1.1.9", "1.1.2"]  # 1.1.10 > 1.1.9 numerically
    assert cl.releases[0].published == "2026-11-01"
    assert cl.releases[2].prerelease is True
    assert cl.releases[2].url == ""
    assert cl.releases[0].url.startswith("https://")
    assert cl.build_version == "1.1.10+2.gabc1234"
    assert cl.releases_url == f"{REPO_URL}/releases"


def test_parse_drops_non_https_release_urls() -> None:
    data: dict[str, Any] = _doc()
    data["releases"][1]["url"] = "javascript:alert(1)"  # the v1.1.10 entry
    by_tag: dict[str, str] = {r.tag: r.url for r in parse_changelog(data).releases}
    assert by_tag["v1.1.10"] == ""
    assert by_tag["v1.1.9"].startswith("https://")


def test_parse_falls_back_to_default_repository_when_invalid() -> None:
    cl: Changelog = parse_changelog(_doc(repository="not a repo"))
    assert cl.releases_url == f"https://github.com/{changelog_service.DEFAULT_REPOSITORY}/releases"


def _top_not_object(data: dict[str, Any]) -> Any:
    return ["nope"]


def _releases_missing(data: dict[str, Any]) -> dict[str, Any]:
    del data["releases"]
    return data


def _release_not_object(data: dict[str, Any]) -> dict[str, Any]:
    data["releases"][0] = "x"
    return data


def _release_without_tag(data: dict[str, Any]) -> dict[str, Any]:
    del data["releases"][0]["tag"]
    return data


def _duplicate_tag(data: dict[str, Any]) -> dict[str, Any]:
    data["releases"][1]["tag"] = data["releases"][0]["tag"]
    return data


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_top_not_object, "top level"),
        (_releases_missing, "releases"),
        (_release_not_object, "must be an object"),
        (_release_without_tag, ".tag"),
        (_duplicate_tag, "more than once"),
    ],
)
def test_parse_rejects_bad_content_with_a_pointer_to_the_problem(mutate: Callable[[dict[str, Any]], Any], message: str) -> None:
    with pytest.raises(ChangelogLoadError, match=re.escape(message)):
        parse_changelog(mutate(_doc()))


def test_empty_release_list_is_valid() -> None:
    assert parse_changelog(_doc(releases=[])).releases == ()


def test_load_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(ChangelogLoadError, match="generate_changelog"):
        load_changelog(tmp_path / "missing.json")
    bad: Path = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ChangelogLoadError, match="not valid JSON"):
        load_changelog(bad)


def test_load_picks_up_regeneration_without_a_restart(tmp_path: Path) -> None:
    path: Path = tmp_path / "changelog.json"
    path.write_text(json.dumps(_doc()), encoding="utf-8")
    assert load_changelog(path) is load_changelog(path)  # cached while unchanged
    path.write_text(json.dumps(_doc(releases=[])), encoding="utf-8")
    os.utime(path, ns=(10**18, 10**18))  # guarantee a different mtime even on coarse clocks
    assert load_changelog(path).releases == ()


# --------------------------------------------------------------------------- page, route and nav wiring
def _render(**ctx: Any) -> str:
    base: str = "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"
    env: Environment = Environment(
        loader=ChoiceLoader([DictLoader({"base.html": base}), FileSystemLoader(str(TEMPLATES))]),
        autoescape=True,
    )
    return env.get_template("pages/changelog.html").render(**ctx)


def _ctx(**overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "changelog": parse_changelog(_doc()),
        "changelog_error": None,
        "app_version": "1.1.10",
        "base_version": "1.1.10",
        "is_dev_build": False,
    }
    ctx.update(overrides)
    return ctx


def test_template_lists_every_release_newest_first_and_marks_the_installed_one() -> None:
    html: str = _render(**_ctx())
    assert html.count("data-release") == 3
    assert html.index("Version 1.1.10") < html.index("Version 1.1.9") < html.index("Version 1.1.2")
    assert 'id="release-1.1.10"' in html
    assert html.count("data-installed-badge") == 1
    assert 'id="installed-version"' in html
    assert ">1.1.10<" in html
    assert "<li>new page</li>" in html  # sanitized notes HTML is rendered, not escaped
    assert "No release notes were provided" in html  # the release with an empty body


def test_template_explains_a_development_build() -> None:
    html: str = _render(**_ctx(app_version="1.1.10+5.gabc1234", is_dev_build=True))
    assert "not exactly release" in html
    assert "installed (base)" in html


def test_template_shows_the_error_instead_of_the_history() -> None:
    html: str = _render(**_ctx(changelog=None, changelog_error="changelog.json was not found"))
    assert "could not be loaded" in html
    assert "was not found" in html
    assert "data-release" not in html


def test_template_handles_no_releases_yet() -> None:
    html: str = _render(**_ctx(changelog=parse_changelog(_doc(releases=[]))))
    assert "No releases have been published yet" in html


def test_template_escapes_untrusted_names() -> None:
    data: dict[str, Any] = _doc()
    data["releases"][0]["version"] = "<b>1.1.9</b>"
    data["releases"][0]["tag"] = "<b>v1.1.9</b>"
    html: str = _render(**_ctx(changelog=parse_changelog(data)))
    assert "<b>1.1.9</b>" not in html


def test_route_and_nav_link_exist() -> None:
    pages_src: str = (PROJECT_ROOT / "classes" / "WebServer" / "routers" / "pages.py").read_text(encoding="utf-8")
    base_src: str = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '"/pages/changelog"' in pages_src
    assert 'href="/pages/changelog"' in base_src
    assert base_src.index("Global Settings submenu") < base_src.index('href="/pages/changelog"') < base_src.index("Protocols menu")


def test_gitignore_excludes_the_generated_file() -> None:
    assert "classes/WebServer/changelog.json" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the generator tool
def _release(tag: str, *, draft: bool = False, body: str | None = "notes") -> dict[str, Any]:
    """A release object shaped like the GitHub API's."""
    return {"tag_name": tag, "name": tag, "draft": draft, "prerelease": False, "published_at": "2026-11-01T00:00:00Z",
            "html_url": f"https://github.com/o/r/releases/tag/{tag}", "body": body}


def test_normalize_release_keeps_only_what_the_page_uses() -> None:
    out: dict[str, Any] | None = gen.normalize_release(_release("v1.2.3", body=None))
    assert out == {"tag": "v1.2.3", "version": "1.2.3", "name": "v1.2.3", "published_at": "2026-11-01T00:00:00Z",
                   "prerelease": False, "url": "https://github.com/o/r/releases/tag/v1.2.3", "released_by": "", "commit": "",
                   "intro": "", "sections": [], "body": ""}
    assert gen.normalize_release({"tag_name": ""}) is None
    assert gen.normalize_release({}) is None


def test_fetch_releases_excludes_drafts_and_pages_through(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[list[dict[str, Any]]] = [
        [_release(f"v1.0.{i}") for i in range(gen.PAGE_SIZE)],
        [_release("v0.9.0"), _release("v0.9.1", draft=True)],
    ]
    calls: list[str] = []

    def fake_get(url: str, token: str | None) -> list[dict[str, Any]]:
        calls.append(url)
        return pages[len(calls) - 1]

    monkeypatch.setattr(gen, "_get_json", fake_get)
    out: list[dict[str, Any]] = gen.fetch_releases("o/r", None, 500)
    assert len(out) == gen.PAGE_SIZE + 1
    assert all(not r.get("draft") for r in out)
    assert len(calls) == 2
    assert "page=2" in calls[1]


def test_fetch_releases_honors_the_limit_and_validates_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, token: str | None) -> list[dict[str, Any]]:
        return [_release(f"v1.0.{i}") for i in range(5)]

    monkeypatch.setattr(gen, "_get_json", fake_get)
    assert len(gen.fetch_releases("o/r", None, 3)) == 3
    with pytest.raises(gen.ChangelogGenerationError):
        gen.fetch_releases("not a repo", None, 3)


def test_get_json_refuses_non_github_urls() -> None:
    with pytest.raises(gen.ChangelogGenerationError):
        gen._get_json("https://example.com/x", None)


def test_docker_safe_tag_replaces_plus() -> None:
    assert gen.docker_safe_tag("1.1.10") == "1.1.10"
    assert gen.docker_safe_tag("1.1.10+5.gabc1234") == "1.1.10-5.gabc1234"


def test_build_document_round_trips_through_the_service() -> None:
    doc: dict[str, Any] = gen.build_document(
        "o/r", [_release("v1.0.0"), {"tag_name": ""}], "1.0.0+1.gabc", "abc", datetime(2026, 11, 21, tzinfo=timezone.utc)
    )
    assert doc["build"] == {"version": "1.0.0+1.gabc", "commit": "abc"}
    assert len(doc["releases"]) == 1  # the tagless entry is dropped
    cl: Changelog = parse_changelog(json.loads(json.dumps(doc)))
    assert cl.releases[0].version == "1.0.0"
    assert cl.build_version == "1.0.0+1.gabc"


def test_main_writes_the_file_and_github_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out_file: Path = tmp_path / "changelog.json"
    gh_out: Path = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setattr(gen, "fetch_releases", _releases_returning([_release("v1.0.0")]))
    monkeypatch.setattr(gen, "git_version", _returns("1.0.0+2.gabc"))
    monkeypatch.setattr(gen, "git_commit", _returns("abc123"))
    monkeypatch.setattr(gen, "build_unreleased", _returns(None))
    assert gen.main(["--output", str(out_file), "--repo", "o/r", "--github-output"]) == 0
    assert parse_changelog(json.loads(out_file.read_text(encoding="utf-8"))).releases[0].tag == "v1.0.0"
    text: str = gh_out.read_text(encoding="utf-8")
    assert "version=1.0.0+2.gabc\n" in text
    assert "image_tag=1.0.0-2.gabc\n" in text
    assert "commit=abc123\n" in text


def test_main_fails_cleanly_when_github_is_unreachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(repository: str, token: str | None, limit: int) -> list[dict[str, Any]]:
        msg: str = "offline"
        raise gen.ChangelogGenerationError(msg)

    monkeypatch.setattr(gen, "fetch_releases", boom)
    out_file: Path = tmp_path / "changelog.json"
    assert gen.main(["--output", str(out_file), "--repo", "o/r"]) == 1
    assert not out_file.exists()


def test_main_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gen, "fetch_releases", _releases_returning([_release("v1.0.0")]))
    monkeypatch.setattr(gen, "build_unreleased", _returns(None))
    out_file: Path = tmp_path / "changelog.json"
    assert gen.main(["--output", str(out_file), "--repo", "o/r", "--dry-run"]) == 0
    assert not out_file.exists()
    assert '"v1.0.0"' in capsys.readouterr().out


# --------------------------------------------------------------------------- VERSION file: major releases only
_TAGS: list[str] = ["v1.1.10", "v1.2.0", "v1.2.1", "v1.10.0", "v2.0.0-rc1", "not-a-version"]


@pytest.mark.parametrize(
    ("new_tag", "existing", "expected"),
    [
        ("v2.0.0", "1.2.1\n", "2.0.0"),    # a new major: VERSION changes
        ("v1.10.1", "1.1.10\n", None),     # patch release: VERSION stays
        ("v1.11.0", "1.1.10\n", None),     # minor release: VERSION stays
        ("v2.0.0-rc2", "1.2.1\n", None),   # a pre-release never counts
        ("v3.0.0", "", "3.0.0"),           # no VERSION yet
    ],
)
def test_decide_changes_version_only_for_a_new_major(new_tag: str, existing: str, expected: str | None) -> None:
    assert uvf.decide(new_tag, existing, [*_TAGS, new_tag]) == expected


def test_decide_sets_the_baseline_for_the_very_first_release() -> None:
    assert uvf.decide("v1.1.10", "", ["v1.1.10"]) == "1.1.10"  # nothing lower exists


def test_decide_never_moves_backwards_or_rewrites_the_same_value() -> None:
    assert uvf.decide("v1.9.9", "2.0.0\n", ["v1.9.8", "v2.0.0", "v1.9.9"]) is None  # a fix for an old major
    assert uvf.decide("v2.0.0", "2.0.0\n", ["v1.9.9", "v2.0.0"]) is None            # a re-run of the same release
    assert uvf.decide("v10.0.0", "9.4.1\n", ["v9.4.1", "v10.0.0"]) == "10.0.0"        # numeric, not text, comparison


def test_previous_stable_picks_the_closest_lower_release() -> None:
    assert uvf.previous_stable((2, 0, 0), _TAGS) == (1, 10, 0)  # 1.10.0 > 1.2.1 numerically
    assert uvf.previous_stable((1, 1, 10), _TAGS) is None


def test_version_tool_writes_the_file_only_when_due(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    version_file: Path = tmp_path / "VERSION"
    version_file.write_text("1.2.1\n", encoding="utf-8")
    gh_out: Path = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setattr(uvf, "list_tags", _tags_returning(["v1.2.1", "v2.0.0"]))
    assert uvf.main(["--tag", "v2.0.0", "--file", str(version_file), "--github-output"]) == 0
    assert version_file.read_text(encoding="utf-8") == "2.0.0\n"
    outputs: str = gh_out.read_text(encoding="utf-8")
    assert "changed=true\n" in outputs
    assert "version=2.0.0\n" in outputs

    monkeypatch.setattr(uvf, "list_tags", _tags_returning(["v1.2.1", "v2.0.0", "v2.1.0"]))
    gh_out.write_text("", encoding="utf-8")
    assert uvf.main(["--tag", "v2.1.0", "--file", str(version_file), "--github-output"]) == 0
    assert version_file.read_text(encoding="utf-8") == "2.0.0\n"  # a minor release leaves it alone
    assert "changed=false\n" in gh_out.read_text(encoding="utf-8")


def test_version_tool_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    version_file: Path = tmp_path / "VERSION"
    version_file.write_text("1.2.1\n", encoding="utf-8")
    monkeypatch.setattr(uvf, "list_tags", _tags_returning(["v1.2.1", "v2.0.0"]))
    assert uvf.main(["--tag", "v2.0.0", "--file", str(version_file), "--dry-run"]) == 0
    assert version_file.read_text(encoding="utf-8") == "1.2.1\n"


def test_update_version_workflow_is_major_only_and_skips_prereleases() -> None:
    text: str = (PROJECT_ROOT / ".github" / "workflows" / "update-version-file.yml").read_text(encoding="utf-8")
    assert "types: [published]" in text
    assert "!github.event.release.prerelease" in text
    assert "update_version_file.py" in text


# --------------------------------------------------------------------------- notes format: parsing the release body
LINKED_BODY: str = f"""## v1.4.0 (2026-11-22)

### Bug Fixes

* Reset failure tracking on reconnect ([#68]({REPO_URL}/pull/68) by [@alice](https://github.com/alice), `f127aa1`)
* Send a message when the link reconnects ([#72]({REPO_URL}/pull/72) by [@bob](https://github.com/bob), `039ec58`)

### Build System

* Pre-commit autoupdate ([#70]({REPO_URL}/pull/70) by [@pre-commit-ci[bot]](https://github.com/apps/pre-commit-ci), `edc3530`)

### Documentation

* Update protocol maps (`84b6acd`)

**Detailed Changes**: [v1.3.0...v1.4.0]({REPO_URL}/compare/v1.3.0...v1.4.0)
"""

DRAFTER_BODY: str = f"""## v1.2.0

### 🚀 Features

* Add a Changelog page ([#41]({REPO_URL}/pull/41) by @BuxtonCalvin, @alice)

### 🐛 Bug Fixes

* Fix `reconnect` message ([#42]({REPO_URL}/pull/42) by @alice)
* A bullet without a pull request

**Detailed Changes**: [v1.1.0...v1.2.0]({REPO_URL}/compare/v1.1.0...v1.2.0)
"""


def _summary(sections: list[dict[str, Any]]) -> list[tuple[str, list[tuple[Any, ...]]]]:
    """Reduce parsed sections to (title, [(entry title, number, authors, sha)]) for easy comparison."""
    return [
        (s["title"], [(e["title"], e["number"], e["authors"], e.get("sha")) for e in s["entries"]])
        for s in sections
    ]


def test_parse_release_body_reads_linked_prs_authors_and_commit_hashes() -> None:
    intro, sections = gen.parse_release_body(LINKED_BODY)
    assert intro == ""
    assert _summary(sections) == [
        ("Bug Fixes", [("Reset failure tracking on reconnect", 68, ["alice"], "f127aa1"),
                       ("Send a message when the link reconnects", 72, ["bob"], "039ec58")]),
        ("Build System", [("Pre-commit autoupdate", 70, ["pre-commit-ci[bot]"], "edc3530")]),
        ("Documentation", [("Update protocol maps", None, [], "84b6acd")]),  # a change without a pull request
    ]


def test_parse_release_body_reads_what_release_drafter_produces() -> None:
    _, sections = gen.parse_release_body(DRAFTER_BODY)
    assert _summary(sections) == [
        ("🚀 Features", [("Add a Changelog page", 41, ["BuxtonCalvin", "alice"], None)]),
        ("🐛 Bug Fixes", [("Fix `reconnect` message", 42, ["alice"], None), ("A bullet without a pull request", None, [], None)]),
    ]


def test_parse_release_body_handles_older_and_free_text_notes() -> None:
    _, old = gen.parse_release_body("## Changes\n\n* Old style (#7) @alice, @bob\n\n### Features\n* Another (#8) @carol")
    assert _summary(old) == [
        ("Changes", [("Old style", 7, ["alice", "bob"], None)]),
        ("Features", [("Another", 8, ["carol"], None)]),
    ]
    assert gen.parse_release_body("First public release.\n\nSupports Modbus RTU.") == ("First public release.\nSupports Modbus RTU.", [])
    assert gen.parse_release_body("") == ("", [])
    # HTML (for example <details> from collapse-after) and empty headings are ignored
    _, html_ish = gen.parse_release_body("### Fixes\n<details>\n* One (#1) @a\n</details>\n### Empty\n")
    assert _summary(html_ish) == [("Fixes", [("One", 1, ["a"], None)])]


# --------------------------------------------------------------------------- notes format: commit hash and author lookups
def _fake_pr(sha: str, login: str) -> dict[str, Any]:
    """A pull request object shaped like the GitHub API's, with a 40-character merge commit hash."""
    return {"merge_commit_sha": sha * 5 + "ab", "user": {"login": login}}


def _releases_with(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"sections": [{"title": "S", "entries": list(entries)}]}]


def test_enrich_adds_sha_and_puts_the_pr_author_first() -> None:
    releases: list[dict[str, Any]] = _releases_with(
        {"title": "A", "number": 1, "authors": ["bob", "alice"]},
        {"title": "B", "number": 2, "authors": []},
        {"title": "No PR", "number": None, "authors": []},
    )
    seen: list[str] = []

    def fake(url: str, token: str | None) -> dict[str, Any]:
        seen.append(url)
        return _fake_pr("abcdef0", "alice") if url.endswith("/1") else _fake_pr("1234567", "carol")

    assert gen.enrich_entries("o/r", releases, "tok", fake) == 2
    first, second, third = releases[0]["sections"][0]["entries"]
    assert first["sha"] == "abcdef0"
    assert first["authors"] == ["alice", "bob"]  # alice was second; the PR author comes first
    assert second["sha"] == "1234567"
    assert second["authors"] == ["carol"]
    assert "sha" not in third
    assert seen == ["https://api.github.com/repos/o/r/pulls/1", "https://api.github.com/repos/o/r/pulls/2"]


def test_enrich_looks_each_pull_request_up_once() -> None:
    releases: list[dict[str, Any]] = [
        *_releases_with({"title": "A", "number": 5, "authors": []}),
        *_releases_with({"title": "A again", "number": 5, "authors": []}),
    ]
    calls: list[str] = []

    def fake(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        return _fake_pr("abcdef0", "alice")

    gen.enrich_entries("o/r", releases, None, fake)
    assert len(calls) == 1


def test_enrich_gives_up_after_repeated_failures_and_never_fails_the_build() -> None:
    entries: list[dict[str, Any]] = [{"title": f"T{i}", "number": i, "authors": []} for i in range(1, 9)]
    releases: list[dict[str, Any]] = _releases_with(*entries)
    calls: list[str] = []

    def failing(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        msg: str = "rate limited"
        raise gen.ChangelogGenerationError(msg)

    assert gen.enrich_entries("o/r", releases, None, failing) == 0
    assert len(calls) == gen.MAX_CONSECUTIVE_LOOKUP_FAILURES  # the rest are skipped, not retried
    assert all("sha" not in e for e in entries)


def test_enrich_ignores_a_malformed_api_answer() -> None:
    releases: list[dict[str, Any]] = _releases_with({"title": "A", "number": 1, "authors": ["x"]})

    def malformed(url: str, token: str | None) -> dict[str, Any]:
        return {"merge_commit_sha": "not hex!", "user": "nope"}

    assert gen.enrich_entries("o/r", releases, None, malformed) == 1
    entry: dict[str, Any] = releases[0]["sections"][0]["entries"][0]
    assert "sha" not in entry
    assert entry["authors"] == ["x"]


def test_build_document_records_who_released_and_the_tag_commit() -> None:
    raw: dict[str, Any] = _release("v1.0.0", body=DRAFTER_BODY)
    raw["author"] = {"login": "BuxtonCalvin"}

    def tag_commit(tag: str) -> str:
        return "0123456789abcdef0123456789abcdef01234567"

    doc: dict[str, Any] = gen.build_document(
        "o/r", [raw], "1.0.0", "abc", datetime(2026, 11, 21, tzinfo=timezone.utc), tag_commit=tag_commit
    )
    release: dict[str, Any] = doc["releases"][0]
    assert doc["schema"] == 2
    assert release["released_by"] == "BuxtonCalvin"
    assert release["commit"] == "0123456"
    assert [s["title"] for s in release["sections"]] == ["🚀 Features", "🐛 Bug Fixes"]


# --------------------------------------------------------------------------- the nested model and its links
def _entry_with_odd_values() -> dict[str, Any]:
    return {
        "title": "Fix `reconnect` <b>message</b>",
        "number": 42,
        "authors": ["alice", "dependabot[bot]", "bad login!"],
        "sha": "0123456789abcdef",
    }


def _doc2() -> dict[str, Any]:
    """A schema-2 document with two major series, sections and entries."""

    def release(tag: str, published: str, sections: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {"tag": tag, "version": tag.removeprefix("v"), "name": tag, "published_at": published, "prerelease": False,
                "url": f"{REPO_URL}/releases/tag/{tag}", "body": "", "intro": "",
                "released_by": "BuxtonCalvin", "commit": "abcdef0123456789", "sections": sections, **extra}

    return {
        "schema": 2,
        "generated_at": "2026-11-22T00:00:00+00:00",
        "repository": REPOSITORY,
        "build": {"version": "2.1.0", "commit": "abc"},
        "releases": [
            release("v1.0.0", "2026-08-01T00:00:00Z", []),
            release("v2.0.0", "2026-10-01T00:00:00Z",
                    [{"title": "Features", "entries": [{"title": "Add X", "number": 7, "authors": ["alice"], "sha": "1111111"}]}]),
            release("v2.1.0", "2026-11-22T00:00:00Z",
                    [{"title": "Bug Fixes", "entries": [_entry_with_odd_values()]},
                     {"title": "Documentation", "entries": [{"title": "Update docs", "number": None, "authors": [], "sha": ""}]}]),
            release("v1.1.0", "2026-09-01T00:00:00Z", [], body="plain notes"),
        ],
    }


def test_series_group_releases_by_major_version_newest_first() -> None:
    cl: Changelog = parse_changelog(_doc2())
    assert [s.label for s in cl.series] == ["2.x", "1.x"]
    assert [r.version for r in cl.series[0].releases] == ["2.1.0", "2.0.0"]
    assert [r.version for r in cl.series[1].releases] == ["1.1.0", "1.0.0"]
    assert [s.entry_count for s in cl.series] == [3, 0]
    assert [r.version for r in cl.releases] == ["2.1.0", "2.0.0", "1.1.0", "1.0.0"]  # the flat list is still there


def test_non_semver_tags_go_to_an_other_series() -> None:
    data: dict[str, Any] = _doc2()
    data["releases"].append({"tag": "nightly", "version": "nightly"})
    assert [s.label for s in parse_changelog(data).series][-1] == "Other"


def test_every_link_is_built_from_validated_parts() -> None:
    release: ChangelogRelease = parse_changelog(_doc2()).releases[0]
    fix: ChangelogEntry = release.sections[0].entries[0]
    docs: ChangelogEntry = release.sections[1].entries[0]
    assert fix.number == 42
    assert fix.pr_url == f"{REPO_URL}/pull/42"
    assert fix.sha == "0123456"
    assert fix.commit_url == f"{REPO_URL}/commit/0123456"
    assert [(a.login, a.url) for a in fix.authors] == [
        ("alice", "https://github.com/alice"),
        ("dependabot[bot]", "https://github.com/apps/dependabot"),
    ]  # "bad login!" is not a GitHub login and was dropped
    assert docs.number is None
    assert docs.pr_url == ""
    assert docs.sha == ""
    assert docs.commit_url == ""
    assert docs.authors == ()
    assert release.commit == "abcdef0"
    assert release.commit_url.endswith("/commit/abcdef0")
    assert release.released_by is not None
    assert release.released_by.url == "https://github.com/BuxtonCalvin"
    assert release.entry_count == 2


def test_entry_titles_are_escaped_and_keep_only_inline_code() -> None:
    fix: ChangelogEntry = parse_changelog(_doc2()).releases[0].sections[0].entries[0]
    assert fix.title_html == "Fix <code>reconnect</code> &lt;b&gt;message&lt;/b&gt;"


def test_compare_links_point_at_the_next_older_release() -> None:
    by_tag: dict[str, ChangelogRelease] = {r.tag: r for r in parse_changelog(_doc2()).releases}
    assert by_tag["v2.1.0"].compare_url == f"{REPO_URL}/compare/v2.0.0...v2.1.0"
    assert by_tag["v2.1.0"].previous_tag == "v2.0.0"
    assert by_tag["v1.0.0"].compare_url == ""  # the oldest release has nothing to compare with
    assert by_tag["v1.0.0"].previous_tag == ""


def test_a_bad_tag_never_becomes_part_of_a_url() -> None:
    data: dict[str, Any] = _doc2()
    data["releases"][1]["tag"] = 'v2.0.0"><script>'
    assert all("<script>" not in r.compare_url for r in parse_changelog(data).releases)


def _sections_not_a_list(data: dict[str, Any]) -> None:
    data["releases"][1]["sections"] = "x"


def _section_not_an_object(data: dict[str, Any]) -> None:
    data["releases"][1]["sections"] = ["x"]


def _section_without_entries(data: dict[str, Any]) -> None:
    data["releases"][1]["sections"] = [{"title": "T"}]


def _section_without_title(data: dict[str, Any]) -> None:
    data["releases"][1]["sections"] = [{"title": "", "entries": []}]


def _entry_without_title(data: dict[str, Any]) -> None:
    data["releases"][1]["sections"] = [{"title": "T", "entries": [{"number": 1}]}]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_sections_not_a_list, "sections must be a list"),
        (_section_not_an_object, "must be an object"),
        (_section_without_entries, "entries must be a list"),
        (_section_without_title, ".title"),
        (_entry_without_title, ".title"),
    ],
)
def test_malformed_sections_are_reported_with_their_location(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data: dict[str, Any] = _doc2()
    mutate(data)
    with pytest.raises(ChangelogLoadError, match=re.escape(message)):
        parse_changelog(data)


def test_a_generator_document_loads_through_the_service_end_to_end() -> None:
    raw: dict[str, Any] = _release("v1.2.0", body=DRAFTER_BODY)
    raw["author"] = {"login": "BuxtonCalvin"}

    def tag_commit(tag: str) -> str:
        return "a" * 40

    def lookup(url: str, token: str | None) -> dict[str, Any]:
        return _fake_pr("fedcba9", "alice")

    doc: dict[str, Any] = gen.build_document(
        "o/r", [raw], "1.2.0", "abc", datetime(2026, 11, 22, tzinfo=timezone.utc), tag_commit=tag_commit
    )
    gen.enrich_entries("o/r", doc["releases"], None, lookup)
    release: ChangelogRelease = parse_changelog(json.loads(json.dumps(doc))).releases[0]
    assert release.commit == "aaaaaaa"
    assert release.released_by is not None
    features: ChangelogSection = release.sections[0]
    assert features.title == "🚀 Features"
    assert features.entries[0].sha == "fedcba9"
    assert [a.login for a in features.entries[0].authors] == ["alice", "BuxtonCalvin"]


# --------------------------------------------------------------------------- the rendered, collapsible page
def _page(doc: dict[str, Any] | None = None, **overrides: Any) -> str:
    context: dict[str, Any] = _ctx(changelog=parse_changelog(doc or _doc2()), base_version="2.1.0", app_version="2.1.0")
    context.update(overrides)
    return _render(**context)


def _reader_text(html: str) -> str:
    """The page as a reader sees it: tags removed, entities decoded, whitespace collapsed."""
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)))


def test_page_has_a_collapsible_level_for_series_release_and_section() -> None:
    html: str = _page()
    assert html.count("<details") == html.count("</details>")
    assert html.count("data-series") == 2
    assert html.count("data-release") == 4
    assert html.count("data-section") == 3
    assert html.count("data-entry") == 3
    assert 'id="changelog-expand"' in html
    assert 'id="changelog-collapse"' in html
    # nesting: a series contains its releases, a release contains its sections
    assert html.index("data-series") < html.index('id="release-2.1.0"') < html.index("data-section")


def test_the_newest_series_the_installed_release_and_its_sections_start_open() -> None:
    html: str = _page()
    assert re.search(r"<details[^>]*data-series open", html)                   # the newest series (2.x)
    assert re.search(r'<details id="release-2.1.0" data-release open', html)   # the installed/newest release
    assert re.search(r'<details id="release-1.0.0" data-release>', html)       # an old release is collapsed
    assert len(re.findall(r"<details data-section open", html)) == 2           # sections of the open release
    assert len(re.findall(r"<details[^>]*data-series open", html)) == 1


def test_entries_show_a_linked_pr_number_authors_and_commit() -> None:
    html: str = _page()
    assert f'href="{REPO_URL}/pull/42"' in html
    assert ">#42<" in html
    assert 'href="https://github.com/alice"' in html
    assert ">@alice<" in html
    assert 'href="https://github.com/apps/dependabot"' in html
    assert f'href="{REPO_URL}/commit/0123456"' in html
    assert ">0123456<" in html
    text: str = _reader_text(html)
    assert "Fix reconnect <b>message</b> (#42 by @alice, @dependabot[bot], 0123456)" in text  # title (#PR by @authors, sha)
    assert "Update docs" in text
    assert "Update docs (" not in text  # an entry with no PR, author or sha has no parentheses


def test_release_header_shows_tag_commit_released_by_and_the_compare_link() -> None:
    html: str = _page()
    text: str = _reader_text(html)
    assert "Tag: v2.1.0 · Commit: abcdef0 · Released by: @BuxtonCalvin · Detailed changes: v2.0.0...v2.1.0" in text
    assert f'href="{REPO_URL}/compare/v2.0.0...v2.1.0"' in html


def test_page_escapes_hostile_entry_text() -> None:
    html: str = _page()
    assert "<b>message</b>" not in html
    assert "&lt;b&gt;message&lt;/b&gt;" in html
    assert "bad login" not in html


def test_releases_without_sections_still_show_their_notes() -> None:
    html: str = _page()
    assert "plain notes" in html  # v1.1.0 has only a body
    assert "No release notes were provided" in html  # v1.0.0 has neither sections nor a body


# --------------------------------------------------------------------------- notes format: every form of an entry
_ENTRY_CASES: list[tuple[str, dict[str, Any]]] = [
        (f"Add an Acme register map ([#42]({REPO_URL}/pull/42) by [@alice](https://github.com/alice), `2476c4b`)",
         {"title": "Add an Acme register map", "number": 42, "authors": ["alice"], "sha": "2476c4b"}),
        ("Add Bluetooth (by [@BuxtonCalvin](https://github.com/BuxtonCalvin), `dafb50d`)",
         {"title": "Add Bluetooth", "number": None, "authors": ["BuxtonCalvin"], "sha": "dafb50d"}),
        ("wip tweak (`f97a7b1`)", {"title": "wip tweak", "number": None, "authors": [], "sha": "f97a7b1"}),
        (f"bump requests ([#51]({REPO_URL}/pull/51) by [@dependabot[bot]](https://github.com/apps/dependabot), `078ee22`)",
         {"title": "bump requests", "number": 51, "authors": ["dependabot[bot]"], "sha": "078ee22"}),
        ("Old style (#7) @alice, @bob", {"title": "Old style", "number": 7, "authors": ["alice", "bob"]}),
        ("Plain title", {"title": "Plain title", "number": None, "authors": []}),
        ("Fix (legacy) behavior (by [@x](https://github.com/x), `abcdef1`)",
         {"title": "Fix (legacy) behavior", "number": None, "authors": ["x"], "sha": "abcdef1"}),
]


@pytest.mark.parametrize(("line", "expected"), _ENTRY_CASES)
def test_parse_entry_understands_every_form_of_a_note(line: str, expected: dict[str, Any]) -> None:
    assert gen.parse_entry(line) == expected


def test_parse_entry_ignores_an_empty_bullet() -> None:
    assert gen.parse_entry("   ") is None
    assert gen.parse_entry("(by [@x](https://github.com/x), `abcdef1`)") is None  # details with no title


def test_enrich_skips_entries_that_already_carry_a_hash() -> None:
    releases: list[dict[str, Any]] = _releases_with(
        {"title": "Has hash", "number": 1, "authors": ["alice"], "sha": "abcdef0"},
        {"title": "Needs hash", "number": 2, "authors": []},
    )
    calls: list[str] = []

    def fake(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        return _fake_pr("1234567", "bob")

    assert gen.enrich_entries("o/r", releases, None, fake) == 1
    assert calls == ["https://api.github.com/repos/o/r/pulls/2"]


# --------------------------------------------------------------------------- commits since the last release ("Unreleased")
def _make_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "master")


def _commit(path: Path, message: str, email: str = "1+BuxtonCalvin@users.noreply.github.com") -> None:
    (path / "f.txt").write_text(message + "\n", encoding="utf-8")  # different content every time
    _git(path, "add", "f.txt")
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", f"user.email={email}", "commit", "-q", "-m", message],  # noqa: S607
        cwd=path, check=True, capture_output=True, text=True,
    )


def test_build_unreleased_lists_the_commits_since_the_last_tag(tmp_path: Path) -> None:
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _make_repo(tmp_path)
    _commit(tmp_path, "feat: First thing")
    _git(tmp_path, "tag", "v1.4.2")
    _commit(tmp_path, "feat: Add Bluetooth functionality")
    _commit(tmp_path, "patch: Fix a typo in a log message")
    _commit(tmp_path, "chore: set VERSION to 2.0.0 [skip changelog]")
    result: dict[str, Any] | None = gen.build_unreleased(tmp_path, "o/r", None, enrich=False)
    assert result is not None
    assert result["previous_tag"] == "v1.4.2"
    assert result["projected_version"] == "1.5.0"  # a feature asks for a minor bump
    assert result["bump"] == "minor"
    assert re.fullmatch(r"[0-9a-f]{7}", result["commit"])
    assert [s["title"] for s in result["sections"]] == ["🚀 Features", "🐛 Bug Fixes"]
    features: dict[str, Any] = result["sections"][0]["entries"][0]
    assert features["title"] == "Add Bluetooth functionality"
    assert features["authors"] == ["BuxtonCalvin"]  # read from the GitHub no-reply address
    assert re.fullmatch(r"[0-9a-f]{7}", features["sha"])


def test_build_unreleased_projects_a_patch_for_routine_work(tmp_path: Path) -> None:
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _make_repo(tmp_path)
    _commit(tmp_path, "feat: First thing")
    _git(tmp_path, "tag", "v1.4.2")
    _commit(tmp_path, "chore(deps): bump requests from 2.31 to 2.32 (#51)", "49699333+dependabot[bot]@users.noreply.github.com")
    result: dict[str, Any] | None = gen.build_unreleased(tmp_path, "o/r", None, enrich=False)
    assert result is not None
    assert result["projected_version"] == "1.4.3"
    assert result["bump"] == "patch"
    assert result["sections"][0]["entries"][0]["authors"] == ["dependabot[bot]"]
    assert result["sections"][0]["entries"][0]["number"] == 51


def test_build_unreleased_is_none_without_a_tag_or_without_releasable_commits(tmp_path: Path) -> None:
    if not _git_is_installed():
        pytest.skip("git is not installed")
    _make_repo(tmp_path)
    _commit(tmp_path, "feat: First thing")
    assert gen.build_unreleased(tmp_path, "o/r", None, enrich=False) is None  # no release tag yet
    _git(tmp_path, "tag", "v1.0.0")
    assert gen.build_unreleased(tmp_path, "o/r", None, enrich=False) is None  # nothing after the tag
    _commit(tmp_path, "chore: set VERSION to 1.0.0 [skip changelog]")
    assert gen.build_unreleased(tmp_path, "o/r", None, enrich=False) is None  # only skipped commits after it
    assert gen.build_unreleased(tmp_path / "missing", "o/r", None, enrich=False) is None  # not a git checkout


def test_main_adds_the_unreleased_section_unless_told_not_to(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unreleased: dict[str, Any] = {
        "previous_tag": "v1.0.0", "projected_version": "1.1.0", "bump": "minor", "commit": "abcdef0",
        "sections": [{"title": "Features", "entries": [{"title": "Add X", "number": None, "authors": [], "sha": "abcdef0"}]}],
    }
    monkeypatch.setattr(gen, "fetch_releases", _releases_returning([_release("v1.0.0")]))
    monkeypatch.setattr(gen, "build_unreleased", _returns_value(unreleased))
    with_file: Path = tmp_path / "with.json"
    assert gen.main(["--output", str(with_file), "--repo", "o/r"]) == 0
    saved: dict[str, Any] = json.loads(with_file.read_text(encoding="utf-8"))
    assert saved["unreleased"]["projected_version"] == "1.1.0"
    assert parse_changelog(saved).unreleased is not None
    without_file: Path = tmp_path / "without.json"
    assert gen.main(["--output", str(without_file), "--repo", "o/r", "--no-unreleased"]) == 0
    assert "unreleased" not in json.loads(without_file.read_text(encoding="utf-8"))


def _returns_value(value: dict[str, Any]) -> Callable[..., dict[str, Any]]:
    """A stand-in that ignores its arguments and returns ``value``."""

    def build(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return value

    return build


def _doc_with_unreleased(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = _doc2()
    data["unreleased"] = {
        "previous_tag": "v2.1.0", "projected_version": "2.2.0", "bump": "minor", "commit": "0123456789abcdef",
        "sections": [
            {"title": "Features", "entries": [{"title": "Add `bluetooth` <b>now</b>", "number": None, "authors": ["alice"], "sha": "dafb50d"}]},
            {"title": "Bug Fixes", "entries": [{"title": "Fix X", "number": 7, "authors": ["bob"], "sha": "2476c4b"}]},
        ],
    }
    data["unreleased"].update(overrides)
    return data


def test_the_unreleased_section_is_parsed_with_validated_links() -> None:
    pending = parse_changelog(_doc_with_unreleased()).unreleased
    assert pending is not None
    assert pending.previous_tag == "v2.1.0"
    assert pending.projected_version == "2.2.0"
    assert pending.bump == "minor"
    assert pending.commit == "0123456"
    assert pending.commit_url == f"{REPO_URL}/commit/0123456"
    assert pending.compare_url == f"{REPO_URL}/compare/v2.1.0...0123456"
    assert pending.entry_count == 2
    assert pending.sections[0].entries[0].title_html == "Add <code>bluetooth</code> &lt;b&gt;now&lt;/b&gt;"
    assert pending.sections[1].entries[0].pr_url == f"{REPO_URL}/pull/7"


def test_the_unreleased_section_is_optional_and_rejects_bad_values() -> None:
    assert parse_changelog(_doc2()).unreleased is None
    assert parse_changelog(_doc_with_unreleased(sections=[])).unreleased is None  # nothing to show
    bad = parse_changelog(_doc_with_unreleased(previous_tag='v1"><script>', projected_version="soon", bump="huge", commit="not hex")).unreleased
    assert bad is not None
    assert bad.previous_tag == ""
    assert bad.projected_version == ""
    assert bad.bump == ""
    assert bad.commit == ""
    assert bad.compare_url == ""
    assert parse_changelog({**_doc2(), "unreleased": "nope"}).unreleased is None


def test_page_shows_an_unreleased_card_first_for_a_development_build() -> None:
    html: str = _page(_doc_with_unreleased(), is_dev_build=True, app_version="2.1.0+2.g0123456")
    assert html.count("data-unreleased") == 1
    assert re.search(r"<details class=\"card\" data-unreleased open", html)  # open, because this build is ahead of the release
    assert html.index("data-unreleased") < html.index("data-series")
    text: str = _reader_text(html)
    assert "Unreleased 2 changes, next release 2.2.0 (minor bump)" in text
    assert "Add bluetooth <b>now</b> (by @alice, dafb50d)" in text
    assert "Fix X (#7 by @bob, 2476c4b)" in text
    assert "Detailed changes: v2.1.0...0123456" in text
    assert "<b>now</b>" not in html  # escaped


def test_page_without_unreleased_changes_has_no_unreleased_card() -> None:
    assert "data-unreleased" not in _page()
