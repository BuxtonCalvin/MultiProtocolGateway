"""Tests for the FAQ: faq.json content rules, the loader/validator, and the page template (no running app needed)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from classes.WebServer.services import faq_service
from classes.WebServer.services.faq_service import FaqLoadError, load_faq, parse_faq

TEMPLATES = Path(__file__).resolve().parents[1] / "classes" / "WebServer" / "templates"

_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")  # shape of a Telegram bot token
_BOT_URL_RE = re.compile(r"api\.telegram\.org/(?:file/)?bot\d")


def _good() -> dict[str, Any]:
    return {
        "categories": [
            {
                "id": "c1",
                "title": "Category",
                "intro": "Intro *text*.",
                "items": [
                    {"id": "first-item", "q": "What <is> this?", "a": "Use `x` & **y**.", "log": ["Some log text 1"], "tags": ["alpha"]},
                    {"id": "second-item", "q": "Second?", "a": "- one\n- two"},
                ],
            }
        ]
    }


# --------------------------------------------------------------------------- the real faq.json
def test_shipped_faq_loads_and_follows_the_rules() -> None:
    cats = load_faq()
    ids = [i.id for c in cats for i in c.items]
    assert ids and len(ids) == len(set(ids))
    for cat in cats:
        assert cat.title and cat.items
        for item in cat.items:
            assert item.question.strip() and item.answer_html.strip()
            assert item.search_text == item.search_text.lower()


def test_shipped_faq_contains_no_secrets_or_device_identifiers() -> None:
    raw = faq_service.FAQ_PATH.read_text(encoding="utf-8")
    assert not _TOKEN_RE.search(raw), "a Telegram-bot-token-shaped string is in faq.json"
    assert not _BOT_URL_RE.search(raw), "a real bot URL (api.telegram.org/bot<digits>…) is in faq.json"
    assert not re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?:\.\d+)?\b", raw), "a private IP address is in faq.json"


def test_shipped_faq_links_point_at_real_pages() -> None:
    raw = faq_service.FAQ_PATH.read_text(encoding="utf-8")
    pages_src = (TEMPLATES.parent / "routers" / "pages.py").read_text(encoding="utf-8")
    for link in set(re.findall(r"\]\((/pages/[a-z0-9-/]+)\)", raw)):
        assert f'"{link}"' in pages_src or link == "/pages/help", f"{link} is not a known page route"


# --------------------------------------------------------------------------- parsing and validation
def test_parse_renders_markdown_and_builds_search_text() -> None:
    cat = parse_faq(_good())[0]
    first, second = cat.items
    assert "<code>x</code>" in first.answer_html and "<strong>y</strong>" in first.answer_html
    assert "<li>one</li>" in second.answer_html
    assert "<em>text</em>" in cat.intro_html
    assert "alpha" in first.search_text and "some log text 1" in first.search_text and "what <is> this?" in first.search_text
    assert first.log == ("Some log text 1",)


def _cat(d: dict[str, Any]) -> dict[str, Any]:
    return d["categories"][0]


def _item(d: dict[str, Any], n: int) -> dict[str, Any]:
    return _cat(d)["items"][n]


def _categories_not_a_list(d: dict[str, Any]) -> None:
    d["categories"] = "nope"


def _category_without_title(d: dict[str, Any]) -> None:
    del _cat(d)["title"]


def _bad_id_characters(d: dict[str, Any]) -> None:
    _item(d, 0)["id"] = "Bad Id"


def _duplicate_id(d: dict[str, Any]) -> None:
    _item(d, 1)["id"] = "first-item"


def _blank_question(d: dict[str, Any]) -> None:
    _item(d, 0)["q"] = "  "


def _missing_answer(d: dict[str, Any]) -> None:
    del _item(d, 0)["a"]


def _log_not_a_list(d: dict[str, Any]) -> None:
    _item(d, 0)["log"] = "not a list"


def _tags_not_strings(d: dict[str, Any]) -> None:
    _item(d, 0)["tags"] = [1]


def _items_not_a_list(d: dict[str, Any]) -> None:
    _cat(d)["items"] = {}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_categories_not_a_list, "categories"),
        (_category_without_title, "title"),
        (_bad_id_characters, "lower-case"),
        (_duplicate_id, "more than once"),
        (_blank_question, ".q"),
        (_missing_answer, ".a"),
        (_log_not_a_list, ".log"),
        (_tags_not_strings, ".tags"),
        (_items_not_a_list, "items"),
    ],
)
def test_parse_rejects_bad_content_with_a_pointer_to_the_problem(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data = _good()
    mutate(data)
    with pytest.raises(FaqLoadError, match=re.escape(message)):
        parse_faq(data)


def test_load_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(FaqLoadError, match="cannot read"):
        load_faq(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(FaqLoadError, match="not valid JSON"):
        load_faq(bad)


def test_load_picks_up_edits_without_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "faq.json"
    path.write_text(json.dumps(_good()), encoding="utf-8")
    assert load_faq(path) is load_faq(path)  # cached while unchanged
    edited = _good()
    edited["categories"][0]["items"][1]["q"] = "Changed question"
    path.write_text(json.dumps(edited), encoding="utf-8")
    os.utime(path, ns=(10**18, 10**18))  # guarantee a different mtime even on coarse clocks
    assert load_faq(path)[0].items[1].question == "Changed question"


# --------------------------------------------------------------------------- the template
def _render(**ctx: Any) -> str:
    base = "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"
    env = Environment(loader=ChoiceLoader([DictLoader({"base.html": base}), FileSystemLoader(str(TEMPLATES))]), autoescape=True)
    return env.get_template("pages/faq.html").render(**ctx)


def test_template_renders_every_entry_with_anchor_and_escapes_text() -> None:
    cats = parse_faq(_good())
    html = _render(faq_categories=cats, faq_error=None)
    assert html.count("<details") == 2
    assert 'id="faq-first-item"' in html and 'id="faq-second-item"' in html
    assert "What &lt;is&gt; this?" in html and "What <is> this?" not in html  # question text is escaped
    assert "<code>x</code>" in html  # answer HTML is rendered, not escaped
    assert "Appears in the log as:" in html and "Some log text 1" in html
    assert 'id="faq-search"' in html and "<script>" in html


def test_template_shows_the_error_instead_of_the_faq() -> None:
    html = _render(faq_categories=[], faq_error="faq.json is not valid JSON: boom")
    assert "could not be loaded" in html and "boom" in html
    assert "<details" not in html and "<script>" not in html


def test_shipped_faq_renders_completely() -> None:
    cats = load_faq()
    html = _render(faq_categories=cats, faq_error=None)
    assert html.count("<details") == sum(len(c.items) for c in cats)
    for cat in cats:
        for item in cat.items:
            assert f'id="faq-{item.id}"' in html
