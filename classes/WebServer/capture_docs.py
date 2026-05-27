# Description: Capture documentation screenshots for the MPG Web UI.
# File: capture_docs.py
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

"""Capture documentation screenshots for the MPG Web UI.

Usage:
    python capture_docs.py --base-url http://10.17.x.y:1717

The crawler follows internal links, waits for network idle on every route, and
writes full-page PNGs to static/screenshots. By default it injects a small
"clean UI" stylesheet before capture so admin chrome does not dominate docs.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

WEB_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = WEB_DIR / "static" / "screenshots"

CLEAN_UI_CSS = """
header[role="banner"],
footer[role="contentinfo"],
#commit-result,
#diff-overlay,
#analysis-panel,
.admin-bar,
.debug-toolbar,
[data-docs-hide],
[data-capture-hide] {
  display: none !important;
}
main#main-content {
  padding: 16px !important;
}
"""


def route_to_filename(path: str) -> str:
    """Convert a URL path to a stable screenshot filename."""
    if path in {"", "/"}:
        return "dashboard.png"
    slug = path.strip("/").lower()
    slug = slug.replace("/", "__")
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return f"{slug or 'dashboard'}.png"


def remove_file(path: Path) -> None:
    """Remove a file, including long generated paths on Windows."""
    if os.name == "nt":
        os.remove("\\\\?\\" + str(path.resolve()))
    else:
        path.unlink()


def same_site(base_url: str, candidate_url: str) -> bool:
    base = urlparse(base_url)
    candidate = urlparse(candidate_url)
    return (
        candidate.scheme in {"http", "https"}
        and candidate.scheme == base.scheme
        and candidate.netloc == base.netloc
    )


def is_crawlable_path(path: str) -> bool:
    if not path or path.startswith(("/static/", "/api/", "/annotations/")):
        return False
    if path.startswith((
        "/docs/files/",
        "/help/files/",
        "/pages/help/context",
        "/pages/help/docs/",
        "/pages/help/files/",
        "/pages/help/screen/",
    )):
        return False
    return "." not in Path(path).name


def wait_until_ready(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        print(f"Network idle timed out for {url}; capturing current rendered state.")


def collect_internal_links(page: Page, base_url: str) -> set[str]:
    hrefs = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(a => a.href)",
    )
    links: set[str] = set()
    for href in hrefs:
        absolute, _fragment = urldefrag(urljoin(base_url, href))
        parsed = urlparse(absolute)
        if same_site(base_url, absolute) and is_crawlable_path(parsed.path):
            links.add(absolute)
    return links


def capture_site(base_url: str, output_dir: Path, clean_ui: bool, fresh: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if fresh:
        for old_capture in output_dir.glob("*.png"):
            remove_file(old_capture)
    start_url, _fragment = urldefrag(base_url.rstrip("/") + "/")
    visited: set[str] = set()
    pending: deque[str] = deque([start_url])
    captured: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()

        while pending:
            url = pending.popleft()
            if url in visited:
                continue
            visited.add(url)

            parsed = urlparse(url)
            if not is_crawlable_path(parsed.path):
                continue

            print(f"Capturing {url}", flush=True)
            try:
                wait_until_ready(page, url)
                if clean_ui:
                    page.add_style_tag(content=CLEAN_UI_CSS)
                screenshot_path = output_dir / route_to_filename(parsed.path)
                page.screenshot(path=str(screenshot_path), full_page=True)
                captured.append(screenshot_path)

                for link in sorted(collect_internal_links(page, start_url)):
                    if link not in visited:
                        pending.append(link)
            except Exception as exc:
                print(f"Skipping {url}: {exc}")

        context.close()
        browser.close()

    return captured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture MPG Web UI screenshots.")
    parser.add_argument(
        "--base-url",
        default="http://10.17.1.100:1717",
        help="Base URL of the running MPG Web UI.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for generated PNG screenshots.",
    )
    parser.add_argument(
        "--no-clean-ui",
        action="store_true",
        help="Disable CSS injection that hides admin bars/chrome.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not remove existing PNGs before capture.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    captured = capture_site(
        base_url=args.base_url,
        output_dir=args.output_dir,
        clean_ui=not args.no_clean_ui,
        fresh=not args.keep_existing,
    )
    print(f"Captured {len(captured)} screenshot(s) in {args.output_dir}")


if __name__ == "__main__":
    main()
