# Description: Updates the root VERSION file when a new MAJOR release is published (used by the update-version-file workflow).
# File: update_version_file.py
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

"""Keep the root ``VERSION`` file in step with the latest MAJOR release.

A downloaded source zip (GitHub's "Download ZIP") has no ``.git`` folder, so the application
cannot ask git for its version. It reads ``VERSION`` instead (see ``defs/app_version.py``).
To avoid a commit for every release, ``VERSION`` is rewritten **only when a new major version is
published**: ``1.4.2 -> 1.5.0`` leaves it alone, ``1.5.0 -> 2.0.0`` sets it to ``2.0.0``.

The rule, in one place (``decide``):

* only stable ``vMAJOR.MINOR.PATCH`` tags count (pre-releases such as ``v2.0.0-rc1`` never do);
* the new major must be higher than the major of the closest *lower* stable release tag
  (or there is no earlier release at all);
* ``VERSION`` never moves backwards, so publishing a fix for an old major later is harmless.

Usage (the workflow does this)::

    python tools/update_version_file.py --tag v2.0.0                  # edit VERSION if due
    python tools/update_version_file.py --tag v2.0.0 --dry-run        # only say what would happen
    python tools/update_version_file.py --tag v2.0.0 --github-output  # set changed= / version= step outputs
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from defs.app_version import VERSION_FILE_PATH  # noqa: E402

_log: logging.Logger = logging.getLogger("update_version_file")

_STABLE_RE: re.Pattern[str] = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


def parse_stable(text: str) -> Version | None:
    """``"v2.0.1"`` or ``"2.0.1"`` -> ``(2, 0, 1)``; anything else (pre-releases, junk) -> None."""
    match: re.Match[str] | None = _STABLE_RE.match(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def previous_stable(new: Version, tags: Iterable[str]) -> Version | None:
    """The highest stable version in ``tags`` that is strictly lower than ``new``, or None."""
    lower: list[Version] = []
    for tag in tags:
        parsed: Version | None = parse_stable(tag)
        if parsed is not None and parsed < new:
            lower.append(parsed)
    return max(lower) if lower else None


def decide(new_tag: str, existing_text: str, tags: Iterable[str]) -> str | None:
    """
    The text to write into VERSION (``"2.0.0"``), or None when it must stay as it is.

    ``existing_text`` is the current VERSION content ("" if absent) and ``tags`` every tag of the repository.
    """
    new: Version | None = parse_stable(new_tag)
    if new is None:
        return None
    existing: Version | None = parse_stable(existing_text)
    if existing is not None and new <= existing:
        return None  # never move backwards, never rewrite the same value
    previous: Version | None = previous_stable(new, tags)
    if previous is not None and new[0] <= previous[0]:
        return None  # minor or patch release
    return f"{new[0]}.{new[1]}.{new[2]}"


def list_tags(root: Path = PROJECT_ROOT) -> list[str]:
    """All tag names of the repository at ``root`` (empty if git is unavailable)."""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            ["git", "tag", "--list"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.split() if result.returncode == 0 else []


def write_github_outputs(changed: bool, version: str) -> None:
    """Append ``changed`` and ``version`` to the file named by ``$GITHUB_OUTPUT`` (GitHub Actions)."""
    target: str = os.environ.get("GITHUB_OUTPUT", "")
    if not target:
        _log.warning("--github-output was given but GITHUB_OUTPUT is not set; skipping")
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(f"changed={'true' if changed else 'false'}\n")
        stream.write(f"version={version}\n")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Update the VERSION file if the given release tag starts a new major version."
    )
    parser.add_argument("--tag", required=True, help="the release tag that was just published, e.g. v2.0.0")
    parser.add_argument("--file", type=Path, default=VERSION_FILE_PATH, help="VERSION file to maintain")
    parser.add_argument("--dry-run", action="store_true", help="report the decision without writing")
    parser.add_argument("--github-output", action="store_true", help="set changed/version step outputs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args: argparse.Namespace = parse_args(argv)
    try:
        existing: str = args.file.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    new_text: str | None = decide(args.tag, existing, list_tags())

    if new_text is None:
        _log.info("%s is not a new major version; %s is left as it is.", args.tag, args.file.name)
    elif args.dry_run:
        _log.info("Would set %s to %s.", args.file.name, new_text)
    else:
        args.file.write_text(f"{new_text}\n", encoding="utf-8", newline="\n")
        _log.info("Set %s to %s.", args.file.name, new_text)

    if args.github_output:
        write_github_outputs(new_text is not None and not args.dry_run, new_text or existing.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
