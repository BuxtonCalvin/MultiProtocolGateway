# Description: Checks that a pull request title starts with a release keyword (used by the PR title workflow).
# File: check_pr_title.py
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

"""Tell contributors when a pull request title does not use a release keyword.

Pull requests are squash-merged, and the pull request title becomes the commit message that
``tools/release_notes.py`` reads. A title such as ``feat: Add an Acme register map`` therefore lands in the right
section of the release notes and asks for the right version bump; a title without a keyword is listed under
*Other Changes* and asks for a minor bump. This check explains the keywords and fails (so the pull request shows a
red check), but it never blocks the merge unless you make it a required check in the repository settings.

The title is read from the ``PR_TITLE`` environment variable (or the first argument), never interpolated into a
shell command, so a hostile title cannot run anything.

Usage::

    PR_TITLE="feat: Add a thing" python tools/check_pr_title.py
    python tools/check_pr_title.py "fix: Reset failure tracking"
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_notes  # noqa: E402


def _escape_annotation(text: str) -> str:
    """Escape a message for a GitHub Actions ``::error::`` annotation (which is a single line)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Check one title; return 0 when it is valid and 1 when it is not."""
    arguments: Sequence[str] = sys.argv[1:] if argv is None else argv
    environment: Mapping[str, str] = os.environ if env is None else env
    title: str = arguments[0] if arguments else environment.get("PR_TITLE", "")

    result: release_notes.TitleCheck = release_notes.check_title(title)
    summary_path: str = environment.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        heading: str = "Pull request title: OK" if result.ok else "Pull request title needs a keyword"
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(f"### {heading}\n\n```\n{result.message}\n```\n")
    if result.ok:
        sys.stdout.write(f"{result.message}\n")
        return 0
    sys.stdout.write(f"::error title=Pull request title::{_escape_annotation(result.message)}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
