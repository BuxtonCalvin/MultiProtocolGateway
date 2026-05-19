"""Help pages, screenshot annotations, and documentation browsing."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import session_scope
from ..services.device_service import NavData, get_nav_data
from ..services.protocol_service import get_protocol_groups

router = APIRouter(tags=["help"])

WEB_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = WEB_DIR / "static"
SCREENSHOT_DIR = STATIC_DIR / "screenshots"
ANNOTATIONS_PATH = STATIC_DIR / "annotations.json"
DOCUMENTATION_DIR = WEB_DIR.parents[1] / "documentation"


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


def _base_context(request: Request) -> dict[str, Any]:
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    return {
        "nav": nav,
        "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
    }


def _safe_child(base: Path, requested: str) -> Path:
    target = (base / requested).resolve()
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
        if isinstance(items, list)
    }


def _doc_tree() -> list[dict[str, Any]]:
    if not DOCUMENTATION_DIR.exists():
        return []
    allowed = {
        ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".txt", ".example",
        ".conf", ".yml", ".yaml", ".json",
    }
    docs: list[dict[str, Any]] = []
    for path in sorted(DOCUMENTATION_DIR.rglob("*")):
        parts = path.relative_to(DOCUMENTATION_DIR).parts
        if not path.is_file() or any(part.startswith(".") for part in parts[:-1]):
            continue
        if path.suffix.lower() not in allowed and ".example" not in path.name:
            continue
        rel = path.relative_to(DOCUMENTATION_DIR).as_posix()
        parts = rel.split("/")
        docs.append({
            "title": path.stem.replace("_", " ").replace("-", " ").title(),
            "name": path.name,
            "path": rel,
            "folder": " / ".join(parts[:-1]) or "Top Level",
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
        image_id = path.stem
        shots.append({
            "id": image_id,
            "title": image_id.replace("__", " / ").replace("-", " ").title(),
            "url": f"/pages/help/screen/{quote(image_id)}",
            "src": f"/static/screenshots/{quote(path.name)}",
        })
    return shots


def _image_id_from_path(path: str) -> str:
    normalized = path.split("?", 1)[0].strip("/")
    if not normalized:
        return "dashboard"
    return re.sub(r"[^a-z0-9._-]+", "-", normalized.lower().replace("/", "__")).strip("-")


def _resolve_doc_target(target: str, doc_path: str) -> str:
    if re.match(r"^[a-z]+://", target) or target.startswith("#"):
        return target
    doc_dir = Path(doc_path).parent.as_posix()
    target_path, fragment = target.split("#", 1) if "#" in target else (target, "")
    rel = (Path(doc_dir) / target_path).as_posix() if doc_dir != "." else target_path
    href = _doc_url(rel)
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
        import markdown as markdown_lib
        # FIX: Renamed 'rendered' to 'lib_rendered' to avoid type conflict
        lib_rendered = markdown_lib.markdown(
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

    lines = markdown.splitlines()
    rendered: list[str] = []  # This is safe now!
    in_code = False
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            rendered.append("</ul>")
        in_list = False

    def resolve_link(target: str) -> str:
        return _resolve_doc_target(target, doc_path)

    for line in lines:
        if line.strip().startswith("```"):
            close_list()
            rendered.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            rendered.append(html.escape(line))
            continue
        stripped = line.strip()
        if not stripped:
            close_list()
            rendered.append("")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            rendered.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
            continue
        item = re.match(r"^[-*]\s+(.*)$", stripped)
        if item:
            if not in_list:
                rendered.append("<ul>")
                in_list = True
            rendered.append(f"<li>{_render_inline(item.group(1), resolve_link)}</li>")
            continue
        close_list()
        rendered.append(f"<p>{_render_inline(stripped, resolve_link)}</p>")

    close_list()
    if in_code:
        rendered.append("</code></pre>")

    final_html: str = "\n".join(rendered)
    return final_html


def _render_inline(text: str, link_resolver) -> str:
    escaped = html.escape(text)
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
    return escaped


@router.get("/annotations/{image_id}", response_model=list[Annotation])
async def get_annotations(image_id: str) -> list[Annotation]:
    return _load_annotations().get(image_id, [])


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
    screenshot = SCREENSHOT_DIR / f"{image_id}.png"
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/help.html",
        context={
            **_base_context(request),
            "screenshots": [],
            "docs": _doc_tree(),
            "mode": "screen",
            "selected_image": {
                "id": image_id,
                "src": f"/static/screenshots/{quote(screenshot.name)}",
                "exists": screenshot.exists(),
                "title": image_id.replace("__", " / ").replace("-", " ").title(),
            },
            "selected_doc": None,
        },
    )


@router.get("/pages/help/files/{doc_path:path}")
async def help_file(doc_path: str):
    doc_path = unquote(doc_path)
    doc_file = _safe_child(DOCUMENTATION_DIR, doc_path)
    if not doc_file.exists() or not doc_file.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(doc_file)


@router.get("/pages/help/docs/{doc_path:path}", response_class=HTMLResponse, response_model=None)
async def help_doc(request: Request, doc_path: str):
    doc_path = unquote(doc_path)
    doc_file = _safe_child(DOCUMENTATION_DIR, doc_path)
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
