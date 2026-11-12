"""The adjustments popup's routes must be matched before the catch-all ``GET /{protocol_name}/{registry_type}``.

FastAPI matches routes in registration order.  ``/api/protocols/adjustments/options`` also fits
``/{protocol_name}/{registry_type}`` (protocol "adjustments", registry type "options"), so if that
route is registered first the popup silently receives an empty register list instead of its options.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.routing import BaseRoute

ROUTER: Path = Path(__file__).resolve().parent.parent / "classes" / "WebServer" / "routers" / "protocols.py"
DECORATOR: re.Pattern[str] = re.compile(r'^@router\.(get|post|patch|put|delete)\("([^"]+)"', re.MULTILINE)
ADJUSTMENT_ROUTES: list[tuple[str, str]] = [
    ("get", "/adjustments/options"),
    ("post", "/adjustments/validate"),
    ("post", "/adjustments/preview"),
]


def _routes() -> list[tuple[str, str]]:
    return [(method, path) for method, path in DECORATOR.findall(ROUTER.read_text(encoding="utf-8"))]


def _as_regex(path: str) -> re.Pattern[str]:
    """``/{a}/{b}/table`` -> a regex that fullmatches any concrete path of that shape."""
    return re.compile(re.sub(r"\{[^/]+\}", "[^/]+", path))


@pytest.mark.parametrize(("method", "path"), ADJUSTMENT_ROUTES)
def test_adjustments_route_is_registered(method: str, path: str) -> None:
    assert (method, path) in _routes()


@pytest.mark.parametrize(("method", "path"), ADJUSTMENT_ROUTES)
def test_no_earlier_route_can_swallow_an_adjustments_route(method: str, path: str) -> None:
    routes: list[tuple[str, str]] = _routes()
    position: int = routes.index((method, path))
    shadowing: list[str] = [
        earlier_path
        for earlier_method, earlier_path in routes[:position]
        if earlier_method == method and _as_regex(earlier_path).fullmatch(path)
    ]
    assert shadowing == [], f"{method.upper()} {path} is shadowed by {shadowing}; keep the adjustments routes above them"


def test_the_generic_route_that_caused_the_bug_still_exists_after_them() -> None:
    """Guards the guard: if list_registers is renamed this test file needs updating, not silently passing."""
    routes: list[tuple[str, str]] = _routes()
    assert ("get", "/{protocol_name}/{registry_type}") in routes
    assert routes.index(("get", "/adjustments/options")) < routes.index(("get", "/{protocol_name}/{registry_type}"))


def test_the_live_router_serves_options_from_the_adjustments_route() -> None:
    """With FastAPI installed, ask the real router which route answers the options URL."""
    pytest.importorskip("fastapi")
    from starlette.routing import Match

    from classes.WebServer.routers.protocols import router

    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/api/protocols/adjustments/options", "root_path": ""}
    full: list[BaseRoute] = [route for route in router.routes if route.matches(scope)[0] == Match.FULL]
    assert full, "no route matches"
    assert getattr(full[0], "path", "") == "/api/protocols/adjustments/options", f"first match is {getattr(full[0], 'path', '')}"
