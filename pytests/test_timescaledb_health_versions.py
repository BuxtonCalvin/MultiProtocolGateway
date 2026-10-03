"""Unit tests for the version rows of the TimescaleDB Bridge Health panel (no database needed)."""

from __future__ import annotations

from collections.abc import Sequence

# These tests deliberately exercise BridgeAdminManager internals with lightweight fakes.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.exc import OperationalError

import classes.transports.timescaledb as tsdb

TEMPLATES = Path(__file__).resolve().parents[1] / "classes" / "WebServer" / "templates"


class _Result:
    def __init__(self, rows: Sequence[Any] | None = None, scalar: Any = None, one: Any = None) -> None:
        self._rows, self._scalar, self._one = rows or [], scalar, one

    def fetchall(self) -> Sequence[Any]:
        return self._rows

    def scalar(self) -> Any:
        return self._scalar

    def one(self) -> Any:
        return self._one


class _Session:
    """Answers the three queries get_health_snapshot makes, by looking at the SQL text."""

    def __init__(self, ext_rows: Sequence[tuple[str, str, str | None]], pg: str | None, fail_ext: bool = False) -> None:
        self.ext_rows, self.pg, self.fail_ext = ext_rows, pg, fail_ext

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: Any) -> _Result:
        sql = str(statement)
        if "protocol_registry" in sql:
            return _Result(one=SimpleNamespace(complete=1, total=2))
        if "pg_extension" in sql:
            if self.fail_ext:
                raise OperationalError("SELECT ...", {}, Exception("boom"))
            return _Result(rows=self.ext_rows)
        if "server_version" in sql:
            return _Result(scalar=self.pg)
        raise AssertionError(sql)


def _discard(*_args: object, **_kwargs: object) -> None:
    return None


def _manager(session: _Session, connected: bool = True, auto_update: bool = False) -> Any:
    bridge = SimpleNamespace(
        tsdb_connected=connected, rollup_mgr=None, backlog=None, max_backlog_size=10000, max_backlog_age=86400,
        auto_update_extensions=auto_update,
    )
    mgr: Any = tsdb.BridgeAdminManager.__new__(tsdb.BridgeAdminManager)
    mgr._bridge = bridge
    mgr.SessionFactory = lambda: session
    mgr._log = SimpleNamespace(error=_discard)
    return mgr


def test_snapshot_reports_installed_available_and_status() -> None:
    rows = [("timescaledb", "2.25.1", "2.30.1"), ("timescaledb_toolkit", "1.26.0", "1.26.0")]
    snap = _manager(_Session(rows, "18.0 (Debian 18.0-1.pgdg13+1)")).get_health_snapshot()
    ts, tk = snap["extension_versions"]
    assert (ts["label"], ts["installed"], ts["available"], ts["up_to_date"]) == ("TimescaleDB", "2.25.1", "2.30.1", False)
    assert (tk["label"], tk["installed"], tk["available"], tk["up_to_date"]) == ("TimescaleDB Toolkit", "1.26.0", "1.26.0", True)
    assert snap["postgres_version"] == "18.0"  # distro suffix stripped
    assert snap["protocols_rollup_total"] == 2  # existing fields untouched


def test_extension_unknown_to_server_counts_as_up_to_date() -> None:
    snap = _manager(_Session([("timescaledb", "2.30.1", None)], "18.0")).get_health_snapshot()
    assert snap["extension_versions"][0]["up_to_date"] is True


def test_not_connected_makes_no_queries_and_returns_empty_versions() -> None:
    snap = _manager(_Session([], None), connected=False).get_health_snapshot()
    assert snap["extension_versions"] == [] and snap["postgres_version"] is None


def test_version_query_failure_does_not_break_the_rest_of_the_snapshot() -> None:
    snap = _manager(_Session([], "18.0", fail_ext=True)).get_health_snapshot()
    assert snap["extension_versions"] == [] and snap["postgres_version"] is None
    assert snap["protocols_rollup_total"] == 2


def _render(health: dict[str, Any]) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template("partials/bridge_timescale_health_panel.html").render(health=health)


def _health(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "migration_in_progress": False, "backlog_count": 0, "max_backlog_size": 10000, "enable_auto_refresh": True,
        "auto_refresh_interval": 21600, "protocols_rollup_complete": 2, "protocols_rollup_total": 2,
        "auto_update_extensions": False, "postgres_version": "18.0", "extension_versions": [],
    }
    return {**base, **over}


def _ext(name: str, installed: str, available: str) -> dict[str, Any]:
    return {"name": name, "label": name, "installed": installed, "available": available, "up_to_date": installed == available}


def test_template_shows_up_to_date_and_update_available() -> None:
    html = _render(_health(extension_versions=[_ext("timescaledb", "2.25.1", "2.30.1"), _ext("timescaledb_toolkit", "1.26.0", "1.26.0")]))
    assert "2.25.1" in html and "Update available" in html and "2.30.1" in html
    assert "Up to date" in html and "1.26.0" in html
    assert "18.0" in html
    assert "Set auto_update_extensions = true" in html and "ALTER EXTENSION timescaledb UPDATE;" in html  # hint while auto-update is off


def test_template_hint_reflects_auto_update_setting() -> None:
    html = _render(_health(auto_update_extensions=True, extension_versions=[_ext("timescaledb", "2.25.1", "2.30.1")]))
    assert "will update it on the next start" in html and "Set auto_update_extensions = true" not in html


def test_template_handles_missing_versions_and_disconnected_bridge() -> None:
    assert "Extension versions unavailable" in _render(_health(postgres_version=None))
    assert "Still connecting" in _render({})
