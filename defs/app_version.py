# Description: Resolves the running application's version string from git tags, the container build, or package metadata.
# File: app_version.py
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

"""defs/app_version.py

Single place that answers "which version of MPG is this?".

The version is *driven by git*: it is the nearest release tag (``v1.1.10``) and, when the
code is ahead of that tag or has uncommitted edits, a PEP 440 style local suffix::

    1.1.10                  exactly the commit tagged v1.1.10
    1.1.10+5.gabc1234       5 commits after v1.1.10
    1.1.10+5.gabc1234.dirty ... with uncommitted changes (only seen on a developer machine)
    0.0.0+gabc1234          no release tag is reachable yet

Where the answer comes from, first match wins:

1. ``MPG_VERSION`` environment variable. The Docker image bakes this in at build time from
   ``git describe`` (see ``Dockerfile`` and ``tools/generate_changelog.py``), because a
   running container has no ``.git`` folder.
2. ``git describe`` against the checkout this file lives in (developer machines).
3. The ``build.version`` field of ``classes/WebServer/changelog.json`` (a copied tree that was
   prepared with ``tools/generate_changelog.py``).
4. The ``VERSION`` file at the project root, reported as ``<version>+src``. This is what a
   downloaded source zip (GitHub's "Download ZIP") has, because a zip contains no ``.git``.
   The file is updated automatically, and **only when a new major version is released**
   (``.github/workflows/update-version-file.yml``), so it is the last major release rather than
   the exact release the zip was cut from; the ``+src`` suffix says so.
5. The installed ``multi-protocol-gateway`` package metadata (``pip install``).
6. ``"unknown"``.

This module is deliberately stdlib-only so build tooling can import it without installing
the application's requirements.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

_log: logging.Logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
BUILD_INFO_PATH: Path = PROJECT_ROOT / "classes" / "WebServer" / "changelog.json"
VERSION_FILE_PATH: Path = PROJECT_ROOT / "VERSION"
SOURCE_SNAPSHOT_SUFFIX: str = "+src"
DISTRIBUTION_NAME: str = "multi-protocol-gateway"
VERSION_ENV_VAR: str = "MPG_VERSION"
UNKNOWN_VERSION: str = "unknown"

# ``git describe --tags --long --dirty --always`` -> "v1.1.10-5-gabc1234" or "v1.1.10-5-gabc1234-dirty".
# The tag itself may contain hyphens (v1.2.0-rc1), hence the non-greedy tag group.
_DESCRIBE_RE: re.Pattern[str] = re.compile(
    r"^(?P<tag>.+?)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?$"
)
_BARE_SHA_RE: re.Pattern[str] = re.compile(r"^(?P<sha>[0-9a-f]{4,40})(?P<dirty>-dirty)?$")
_LEADING_V_RE: re.Pattern[str] = re.compile(r"^v(?=\d)")
_VERSION_FILE_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+$")


def format_git_describe(raw: str) -> str:
    """
    Convert ``git describe --tags --long --dirty --always`` output into a version string.

    ``--long`` makes git always print the commit distance, so "exactly on the tag" (distance
    0, clean tree) is distinguishable from "ahead of the tag". Output that is only an
    abbreviated hash means no tag is reachable. Unrecognized output returns ``"unknown"``.
    """
    text: str = raw.strip()
    match: re.Match[str] | None = _DESCRIBE_RE.match(text)
    if match is not None:
        tag: str = _LEADING_V_RE.sub("", match.group("tag"))
        distance: int = int(match.group("distance"))
        local: list[str] = []
        if distance:
            local.extend([str(distance), f"g{match.group('sha')}"])
        if match.group("dirty"):
            local.append("dirty")
        return f"{tag}+{'.'.join(local)}" if local else tag

    bare: re.Match[str] | None = _BARE_SHA_RE.match(text)
    if bare is not None:
        suffix: str = ".dirty" if bare.group("dirty") else ""
        return f"0.0.0+g{bare.group('sha')}{suffix}"
    return UNKNOWN_VERSION


def _run_git(args: list[str], root: Path) -> str | None:
    """Run ``git <args>`` in ``root``; return stripped stdout, or None if git is missing or fails."""
    if not (root / ".git").exists():
        return None
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_version(root: Path = PROJECT_ROOT) -> str | None:
    """The version derived from the git checkout at ``root``, or None when there is no usable git."""
    raw: str | None = _run_git(["describe", "--tags", "--long", "--dirty", "--always"], root)
    if raw is None:
        return None
    version: str = format_git_describe(raw)
    return None if version == UNKNOWN_VERSION else version


def git_commit(root: Path = PROJECT_ROOT) -> str | None:
    """The full commit hash of HEAD in the checkout at ``root``, or None when there is no usable git."""
    return _run_git(["rev-parse", "HEAD"], root)


def git_tag_commit(tag: str, root: Path = PROJECT_ROOT) -> str | None:
    """The full commit hash a tag points at, or None when the tag or git is unavailable."""
    if not tag or tag.startswith("-"):
        return None  # never let a tag name be read as a git option
    return _run_git(["rev-list", "-n", "1", tag], root)


def build_info_version(path: Path = BUILD_INFO_PATH) -> str | None:
    """The ``build.version`` recorded in changelog.json by tools/generate_changelog.py, if present."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    build: Any = cast(dict[str, Any], data).get("build")
    if not isinstance(build, dict):
        return None
    version: Any = cast(dict[str, Any], build).get("version")
    if isinstance(version, str) and version.strip() and version.strip() != UNKNOWN_VERSION:
        return version.strip()
    return None


def version_file_version(path: Path = VERSION_FILE_PATH) -> str | None:
    """
    The version recorded in the ``VERSION`` file as ``"<version>+src"``, or None if it is missing or malformed.

    The file holds one plain ``MAJOR.MINOR.PATCH`` line. The ``+src`` suffix marks the value as
    "the last major release this source tree is based on" rather than an exact release.
    """
    try:
        text: str = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _VERSION_FILE_RE.match(text):
        return None
    return f"{text}{SOURCE_SNAPSHOT_SUFFIX}"


def _metadata_version() -> str | None:
    """The version of the pip-installed distribution, if this code was installed that way."""
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def app_version() -> str:
    """
    The running application's version (see the module docstring for the lookup order).

    Cached for the life of the process, because the answer cannot change while it runs.
    Tests that need to re-resolve call ``app_version.cache_clear()``.
    """
    env_value: str = os.environ.get(VERSION_ENV_VAR, "").strip()
    if env_value and env_value != UNKNOWN_VERSION:
        return env_value
    for resolver in (git_version, build_info_version, version_file_version, _metadata_version):
        resolved: str | None = resolver()
        if resolved:
            return resolved
    _log.debug("Could not determine the application version; reporting %r", UNKNOWN_VERSION)
    return UNKNOWN_VERSION


def base_version(version: str) -> str:
    """The release part of a version string: ``"1.1.10+5.gabc1234"`` -> ``"1.1.10"``."""
    return version.split("+", 1)[0]


def is_development_build(version: str) -> bool:
    """True when the version has a local suffix, i.e. it is not exactly a tagged release."""
    return "+" in version
