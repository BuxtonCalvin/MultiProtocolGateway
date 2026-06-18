# Description: Help pages, screenshot annotations, and documentation browsing.
# File: help.py
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

"""Help pages, screenshot annotations, and documentation browsing."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import markdown as markdown_lib
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import session_scope
from ..services.device_service import NavData, get_nav_data
from ..services.protocol_service import get_protocol_groups

router = APIRouter(tags=["help"])

WEB_DIR: Path = Path(__file__).resolve().parents[1]
STATIC_DIR: Path = WEB_DIR / "static"
SCREENSHOT_DIR: Path = STATIC_DIR / "screenshots"
ANNOTATIONS_PATH: Path = STATIC_DIR / "annotations.json"
DOCUMENTATION_DIR: Path = WEB_DIR.parents[1] / "documentation"


class Annotation(BaseModel):
    id: str
    x_percent: float = Field(..., ge=0, le=100)
    y_percent: float = Field(..., ge=0, le=100)
    label: str
    shape_type: str = Field(default="dot")
    label_x_percent: float | None = Field(default=None, ge=0, le=100)
    label_y_percent: float | None = Field(default=None, ge=0, le=100)
    width_percent: float | None = Field(default=None, ge=0, le=100)
    height_percent: float | None = Field(default=None, ge=0, le=100)
    stroke_color: str | None = Field(default=None)
    fill_color: str | None = Field(default=None)


def _base_context(request: Request) -> dict[str, Any]:
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    return {
        "nav": nav,
        "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
    }


def _safe_child(base: Path, requested: str) -> Path:
    target: Path = (base / requested).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return target


def _load_annotations() -> dict[str, list[Annotation]]:
    if not ANNOTATIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid annotations.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="annotations.json must be an object.")
    return {
        str(image_id): [Annotation.model_validate(item) for item in items]
        for image_id, items in raw.items()
        if isinstance(items, list) and image_id != "_aliases"  # _aliases is a dict, not an annotation list
    }


def _load_aliases() -> dict[str, str]:
    """Return the _aliases map: { url-derived image_id -> canonical image_id }.

    Defined as a top-level key in annotations.json:
        "_aliases": {
            "device__inverter_read":  "device__scraper_generic",
            "device__eg4_ll_s_1":     "device__scraper_generic",
            "device__mqtt":           "device__bridge_generic",
            "device__timescaledb":    "device__bridge_generic",
            "protocol-editor__*":     "protocols"
        }

    Keys are matched against the normalized image_id. A trailing '*' acts as a
    prefix wildcard — the longest matching prefix wins.
    """
    if not ANNOTATIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    aliases = raw.get("_aliases", {})
    return {str(k): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {}


def _resolve_image_id(image_id: str) -> str:
    """Apply alias resolution to a normalized image_id.

    1. Exact match in the alias map.
    2. Prefix-wildcard match (key ending in '*') — longest prefix wins.
    3. Return the id unchanged if no alias found.
    """
    aliases: dict[str, str] = _load_aliases()

    # 1. Exact match
    if image_id in aliases:
        return aliases[image_id]

    # 2. Wildcard prefix — longest match wins
    best_len, best_target = -1, None
    for pattern, target in aliases.items():
        if pattern.endswith("*"):
            prefix: str = pattern[:-1]
            if image_id.startswith(prefix) and len(prefix) > best_len:
                best_len, best_target = len(prefix), target
    if best_target is not None:
        return best_target

    return image_id


def _doc_tree() -> list[dict[str, Any]]:
    if not DOCUMENTATION_DIR.exists():
        return []
    allowed: set[str] = {
        ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".txt", ".example",
        ".conf", ".yml", ".yaml", ".json",
    }
    docs: list[dict[str, Any]] = []
    for path in sorted(DOCUMENTATION_DIR.rglob("*")):
        path_parts: tuple[str, ...] = path.relative_to(DOCUMENTATION_DIR).parts
        if not path.is_file() or any(part.startswith(".") for part in path_parts[:-1]):
            continue
        if path.suffix.lower() not in allowed and ".example" not in path.name:
            continue
        rel: str = path.relative_to(DOCUMENTATION_DIR).as_posix()
        rel_parts: list[str] = rel.split("/")
        docs.append({
            "title": path.stem.replace("_", " ").replace("-", " ").title(),
            "name": path.name,
            "path": rel,
            "folder": " / ".join(rel_parts[:-1]) or "Top Level",
            "url": _doc_url(rel),
            "is_pdf": path.suffix.lower() == ".pdf",
        })
    return docs


def _doc_url(rel_path: str) -> str:
    route = "docs" if Path(rel_path.split("#", 1)[0]).suffix.lower() == ".md" else "files"
    return f"/pages/help/{route}/{quote(rel_path, safe='/#')}"


def _screenshot_list() -> list[dict[str, str]]:
    if not SCREENSHOT_DIR.exists():
        return []
    shots: list[dict[str, str]] = []
    for path in sorted(SCREENSHOT_DIR.glob("*.png")):
        image_id: str = path.stem
        shots.append({
            "id": image_id,
            "title": image_id.replace("__", " / ").replace("-", " ").title(),
            "url": f"/pages/help/screen/{quote(image_id)}",
            "src": f"/static/screenshots/{quote(path.name)}",
        })
    return shots


def _image_id_from_path(path: str) -> str:
    """Normalize a URL path to an image_id, then apply alias resolution.

    e.g. /device/Inverter_read           -> device__inverter_read  -> device__scraper_generic
         /protocol-editor/eg4/eg4_18kpv  -> protocol-editor__eg4__eg4-18kpv -> protocols
    """
    normalized: str = path.split("?", 1)[0].strip("/")
    if not normalized:
        raw_id = "dashboard"
    else:
        raw_id: str = re.sub(r"[^a-z0-9._-]+", "-", normalized.lower().replace("/", "__")).strip("-")
    return _resolve_image_id(raw_id)


def _resolve_doc_target(target: str, doc_path: str) -> str:
    if re.match(r"^[a-z]+://", target) or target.startswith("#"):
        return target
    doc_dir: str = Path(doc_path).parent.as_posix()
    target_path, fragment = target.split("#", 1) if "#" in target else (target, "")
    rel: str = (Path(doc_dir) / target_path).as_posix() if doc_dir != "." else target_path
    href: str = _doc_url(rel)
    if fragment:
        href = f"{href}#{quote(fragment, safe='')}"
    return href


def _open_pdfs_in_new_tabs(rendered_html: str) -> str:
    return re.sub(
        r'(<a\s+[^>]*href="[^"]+\.pdf(?:#[^"]*)?")([^>]*>)',
        lambda match: match.group(1)
        + (' target="_blank" rel="noopener"' if "target=" not in match.group(2) else "")
        + match.group(2),
        rendered_html,
        flags=re.IGNORECASE,
    )


def _rewrite_markdown_links(markdown: str, doc_path: str) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, target, suffix = match.groups()
        return f"{prefix}{_resolve_doc_target(target.strip(), doc_path)}{suffix}"

    return re.sub(r"(!?\[[^\]]*\]\()([^)]+)(\))", repl, markdown)


def _markdown_to_html(markdown: str, doc_path: str) -> str:
    markdown = _rewrite_markdown_links(markdown, doc_path)
    try:
        lib_rendered: str = markdown_lib.markdown(
            markdown,
            extensions=["fenced_code", "tables", "toc", "sane_lists", "codehilite"],
            extension_configs={
                "codehilite": {
                    "guess_lang": False,
                    "linenums": False,
                    "use_pygments": True,
                    "css_class": "codehilite",
                },
            },
            output_format="html",
        )
        return _open_pdfs_in_new_tabs(lib_rendered)
    except ImportError:
        pass

    lines: list[str] = markdown.splitlines()
    rendered: list[str] = []
    in_fenced_code = False
    in_ul = False
    in_ol = False
    in_blockquote = False


    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            rendered.append("</ul>")
        in_ul = False

    def close_ol() -> None:
        nonlocal in_ol
        if in_ol:
            rendered.append("</ol>")
        in_ol = False

    def close_lists() -> None:
        close_ul()
        close_ol()

    def close_blockquote() -> None:
        nonlocal in_blockquote
        if in_blockquote:
            rendered.append("</blockquote>")
        in_blockquote = False

    def resolve_link(target: str) -> str:
        return _resolve_doc_target(target, doc_path)

    # Strip YAML/TOML frontmatter (--- ... --- or +++ ... +++)
    if lines and lines[0].strip() in ("---", "+++"):
        fence_char: str = lines[0].strip()
        j = 1
        while j < len(lines) and lines[j].strip() != fence_char:
            j += 1
        lines = lines[j + 1:]

    for line in lines:
        # Fenced code blocks
        if line.strip().startswith("```"):
            close_lists()
            close_blockquote()
            if in_fenced_code:
                rendered.append("</code></pre>")
                in_fenced_code = False
            else:
                lang: str = line.strip()[3:].strip()
                lang_attr: str = f' class="language-{html.escape(lang)}"' if lang else ""
                rendered.append(f"<pre><code{lang_attr}>")
                in_fenced_code = True
            continue

        if in_fenced_code:
            rendered.append(html.escape(line))
            continue

        stripped: str = line.strip()

        # Horizontal rules
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_lists()
            close_blockquote()
            rendered.append("<hr>")
            continue

        # Blank line
        if not stripped:
            close_lists()
            close_blockquote()
            rendered.append("")
            continue

        # Blockquote
        if stripped.startswith(">"):
            close_lists()
            if not in_blockquote:
                rendered.append("<blockquote>")
                in_blockquote = True
            content: str = re.sub(r"^>\s?", "", stripped)
            rendered.append(f"<p>{_render_inline(content, resolve_link)}</p>")
            continue
        else:
            close_blockquote()

        # Headings
        heading: re.Match[str] | None = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_lists()
            level: int = len(heading.group(1))
            text: str = heading.group(2).rstrip("#").strip()
            # Generate an anchor id for TOC compatibility
            anchor: str = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            rendered.append(f'<h{level} id="{anchor}">{_render_inline(text, resolve_link)}</h{level}>')
            continue

        # Setext-style headings (underline with === or ---)
        # (handled by checking next line — skip for simplicity; --- already handled as <hr>)

        # Unordered list items
        ul_item: re.Match[str] | None = re.match(r"^[-*+]\s+(.*)$", stripped)
        if ul_item:
            close_ol()
            if not in_ul:
                rendered.append("<ul>")
                in_ul = True
            rendered.append(f"<li>{_render_inline(ul_item.group(1), resolve_link)}</li>")
            continue

        # Ordered list items
        ol_item: re.Match[str] | None = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ol_item:
            close_ul()
            if not in_ol:
                rendered.append("<ol>")
                in_ol = True
            content = re.sub(r"^\d+[.)]\s+", "", stripped)
            rendered.append(f"<li>{_render_inline(content, resolve_link)}</li>")
            continue

        close_lists()
        rendered.append(f"<p>{_render_inline(stripped, resolve_link)}</p>")

    close_lists()
    close_blockquote()
    if in_fenced_code:
        rendered.append("</code></pre>")

    final_html: str = "\n".join(rendered)
    return final_html


def _render_inline(text: str, link_resolver) -> str:
    escaped: str = html.escape(text)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: (
            f'<img src="{html.escape(link_resolver(html.unescape(m.group(2))))}" '
            f'alt="{html.escape(html.unescape(m.group(1)))}" />'
        ),
        escaped,
    )
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: (
            f'<a href="{html.escape(link_resolver(html.unescape(m.group(2))))}">'
            f"{html.escape(html.unescape(m.group(1)))}</a>"
        ),
        escaped,
    )
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    # Bold+italic: ***text*** or ___text___
    escaped = re.sub(r"\*{3}(.+?)\*{3}", r"<strong><em>\1</em></strong>", escaped)
    escaped = re.sub(r"_{3}(.+?)_{3}", r"<strong><em>\1</em></strong>", escaped)
    # Bold: **text** or __text__
    escaped = re.sub(r"\*{2}(.+?)\*{2}", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"_{2}(.+?)_{2}", r"<strong>\1</strong>", escaped)
    # Italic: *text* or _text_
    escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"_(.+?)_", r"<em>\1</em>", escaped)
    return escaped


@router.get("/annotations/{image_id}", response_model=list[Annotation])
async def get_annotations(image_id: str) -> list[Annotation]:
    resolved_id: str = _resolve_image_id(image_id)
    return _load_annotations().get(resolved_id, [])


@router.get("/pages/help", response_class=HTMLResponse, response_model=None)
async def help_index(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/help.html",
        context={
            **_base_context(request),
            "screenshots": _screenshot_list(),
            "docs": _doc_tree(),
            "mode": "library",
            "selected_image": None,
            "selected_doc": None,
        },
    )


@router.get("/pages/help/documentation", response_class=HTMLResponse, response_model=None)
async def help_documentation(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/help.html",
        context={
            **_base_context(request),
            "screenshots": [],
            "docs": _doc_tree(),
            "mode": "documentation",
            "selected_image": None,
            "selected_doc": None,
        },
    )


@router.get("/pages/help/context", response_class=HTMLResponse, response_model=None)
async def help_context(request: Request, path: str = "/"):
    if path.startswith("/pages/help"):
        return RedirectResponse(url="/pages/help", status_code=303)
    return await help_screen(request, _image_id_from_path(path))


@router.get("/pages/help/screen/{image_id}", response_class=HTMLResponse, response_model=None)
async def help_screen(request: Request, image_id: str):
    # Resolve alias so e.g. /pages/help/screen/device__inverter_read
    # serves the device__scraper_generic screenshot and annotations.
    resolved_id: str = _resolve_image_id(image_id)
    screenshot: Path = SCREENSHOT_DIR / f"{resolved_id}.png"
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/help.html",
        context={
            **_base_context(request),
            "screenshots": [],
            "docs": _doc_tree(),
            "mode": "screen",
            "selected_image": {
                "id": resolved_id,
                "src": f"/static/screenshots/{quote(screenshot.name)}",
                "exists": screenshot.exists(),
                "title": resolved_id.replace("__", " / ").replace("-", " ").title(),
            },
            "selected_doc": None,
        },
    )


@router.get("/pages/help/files/{doc_path:path}")
async def help_file(doc_path: str):
    doc_path = unquote(doc_path)
    doc_file: Path = _safe_child(DOCUMENTATION_DIR, doc_path)
    if not doc_file.exists() or not doc_file.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(doc_file)


@router.get("/pages/help/docs/{doc_path:path}", response_class=HTMLResponse, response_model=None)
async def help_doc(request: Request, doc_path: str):
    doc_path = unquote(doc_path)
    doc_file: Path = _safe_child(DOCUMENTATION_DIR, doc_path)
    if not doc_file.exists() or not doc_file.is_file():
        raise HTTPException(status_code=404, detail="Document not found")

    selected_doc: dict[str, Any] = {
        "path": doc_path,
        "title": doc_file.stem.replace("_", " ").replace("-", " ").title(),
        "is_markdown": doc_file.suffix.lower() == ".md",
        "is_image": doc_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"},
        "raw_url": f"/pages/help/files/{quote(doc_path, safe='/#')}",
    }
    if selected_doc["is_markdown"]:
        selected_doc["html"] = _markdown_to_html(doc_file.read_text(encoding="utf-8", errors="replace"), doc_path)
    elif not selected_doc["is_image"]:
        selected_doc["text"] = doc_file.read_text(encoding="utf-8", errors="replace")

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/help.html",
        context={
            **_base_context(request),
            "screenshots": [],
            "docs": _doc_tree(),
            "mode": "documentation",
            "selected_image": None,
            "selected_doc": selected_doc,
        },
    )
