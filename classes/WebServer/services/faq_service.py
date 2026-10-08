# Description: services/faq_service.py
# File: faq_service.py
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

"""services/faq_service.py

Loads the FAQ shown on the admin UI's FAQ page from ``classes/WebServer/faq.json``.

Keeping the questions and answers in JSON, instead of inside the template, means:

* the FAQ is edited as plain data (answers are Markdown) without touching HTML or Jinja;
* every entry is validated when it is loaded (required fields, unique ids), so a typo shows
  up as a clear message on the page and in the log rather than as a half-rendered page;
* the page can offer search and deep links (``/pages/faq#interleaved-cycle``) because every
  entry has a stable id and searchable text;
* the same file can feed other consumers later (docs generation, a log viewer that links a
  message to its explanation) without parsing HTML.

The file is re-read automatically when it changes on disk, so edits show up without a restart.

File format::

    {"categories": [
        {"id": "log", "title": "...", "intro": "markdown, optional",
         "items": [
            {"id": "unique-slug", "q": "Question?", "a": "Markdown answer",
             "log": ["literal log text the entry explains"],   # optional, shown and searchable
             "tags": ["extra", "search", "words"]}             # optional, searchable only
         ]}
    ]}
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Any, cast

import markdown as markdown_lib

_log: logging.Logger = logging.getLogger(__name__)

FAQ_PATH: Path = Path(__file__).resolve().parents[1] / "faq.json"

_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")
_WS_RE: re.Pattern[str] = re.compile(r"\s+")


class FaqLoadError(ValueError):
    """faq.json is missing, is not valid JSON, or does not follow the documented format."""


@dataclass(frozen=True)
class FaqItem:
    """One question and answer, with the answer already rendered to HTML."""

    id: str
    question: str
    answer_html: str
    log: tuple[str, ...]
    tags: tuple[str, ...]
    search_text: str  # lower-cased question + answer text + log snippets + tags, for the page's search box


@dataclass(frozen=True)
class FaqCategory:
    id: str
    title: str
    intro_html: str
    items: tuple[FaqItem, ...]


def render_markdown(text: str) -> str:
    """Render FAQ Markdown to HTML (the answers are written by the maintainers, so raw HTML is allowed)."""
    return markdown_lib.markdown(text, extensions=["fenced_code", "tables", "sane_lists"], output_format="html")


def _plain_text(html_text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_text))).strip()


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg: str = f"{where} must be a non-empty string"
        raise FaqLoadError(msg)
    return value.strip()


def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in cast(list[Any], value)):
        msg: str = f"{where} must be a list of non-empty strings"
        raise FaqLoadError(msg)
    return tuple(v.strip() for v in cast(list[str], value))


def parse_faq(data: Any) -> list[FaqCategory]:
    """Validate the parsed JSON and turn it into rendered categories. Raises FaqLoadError with a precise location."""
    if not isinstance(data, dict) or not isinstance(cast(dict[str, Any], data).get("categories"), list):
        msg = 'top level must be an object with a "categories" list'
        raise FaqLoadError(msg)

    categories: list[FaqCategory] = []
    seen_ids: set[str] = set()
    for c_index, raw_cat in enumerate(cast(list[Any], cast(dict[str, Any], data)["categories"]), start=1):
        where_cat: str = f"categories[{c_index}]"
        if not isinstance(raw_cat, dict):
            msg: str = f"{where_cat} must be an object"
            raise FaqLoadError(msg)
        cat: dict[str, Any] = cast(dict[str, Any], raw_cat)
        cat_id: str = _require_str(cat.get("id"), f"{where_cat}.id")
        title: str = _require_str(cat.get("title"), f"{where_cat}.title")
        raw_items: Any | None = cat.get("items")
        if not isinstance(raw_items, list):
            msg = f"{where_cat}.items must be a list"
            raise FaqLoadError(msg)

        items: list[FaqItem] = []
        for i_index, raw_item in enumerate(cast(list[Any], raw_items), start=1):
            where: str = f'{where_cat}.items[{i_index}] ("{cat_id}")'
            if not isinstance(raw_item, dict):
                msg = f"{where} must be an object"
                raise FaqLoadError(msg)
            item: dict[str, Any] = cast(dict[str, Any], raw_item)
            item_id: str = _require_str(item.get("id"), f"{where}.id")
            if not _ID_RE.match(item_id):
                msg = f"{where}.id {item_id!r} must be lower-case letters, digits and single hyphens"
                raise FaqLoadError(msg)
            if item_id in seen_ids:
                msg = f"{where}.id {item_id!r} is used more than once"
                raise FaqLoadError(msg)
            seen_ids.add(item_id)
            question: str = _require_str(item.get("q"), f"{where}.q")
            answer_html: str = render_markdown(_require_str(item.get("a"), f"{where}.a"))
            log: tuple[str, ...] = _string_list(item.get("log"), f"{where}.log")
            tags: tuple[str, ...] = _string_list(item.get("tags"), f"{where}.tags")
            search_text: str = " ".join([question, _plain_text(answer_html), *log, *tags]).lower()
            items.append(FaqItem(item_id, question, answer_html, log, tags, search_text))

        intro: Any | None = cat.get("intro")
        intro_html: str = render_markdown(_require_str(intro, f"{where_cat}.intro")) if intro is not None else ""
        categories.append(FaqCategory(cat_id, title, intro_html, tuple(items)))
    return categories


_cache_lock: threading.Lock = threading.Lock()
_cache: dict[Path, tuple[tuple[int, int], list[FaqCategory]]] = {}


def load_faq(path: Path = FAQ_PATH) -> list[FaqCategory]:
    """
    The FAQ categories, parsed and rendered. Cached per file and refreshed when the file's
    modification time or size changes.

    Raises FaqLoadError if the file is missing or invalid; the page shows that message
    instead of failing.
    """
    try:
        stat: stat_result = path.stat()
    except OSError as exc:
        msg: str = f"cannot read {path.name}: {exc}"
        raise FaqLoadError(msg) from exc
    signature: tuple[int, int] = (stat.st_mtime_ns, stat.st_size)

    with _cache_lock:
        cached: tuple[tuple[int, int], list[FaqCategory]] | None = _cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg: str = f"{path.name} is not valid JSON: {exc}"
            raise FaqLoadError(msg) from exc
        categories: list[FaqCategory] = parse_faq(data)
        _cache[path] = (signature, categories)
        _log.debug("FAQ loaded: %d entries in %d categories", sum(len(c.items) for c in categories), len(categories))
        return categories
