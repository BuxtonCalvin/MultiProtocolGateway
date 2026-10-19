"""Tests that something which exists on a bridge's server but that no scraper writes through that bridge can't be selected.

A protocol's TimescaleDB table, or an InfluxDB device, can outlive the scraper->bridge link that fed it (the scraper was
re-pointed at another bridge, say). The Admin UI still lists it, since it is on the server, but disabled, and the endpoints
that stage or run something refuse a request for it that arrives anyway. Nothing here needs a database: templates are
rendered from the real files, and the service and router functions are driven with small fakes.
"""

# These tests deliberately exercise private helpers of the routers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import re
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

import classes.WebServer.services.bridge_service as bs
from classes.WebServer.routers import influxdb as influx_router
from classes.WebServer.routers import timescale as ts_router
from classes.WebServer.routers import timeshift as tshift_router

TEMPLATES: Path = Path(__file__).resolve().parents[1] / "classes" / "WebServer" / "templates"

# Your config: eg4_ll_s_2 (protocol eg4_ll_s) is bridged to TimescaleDB; Inverter_read (eg4_18kpv) is not.
FED: list[str] = ["shared_narrow", "eg4_ll_s"]
NOTHING_DISABLED: set[str] = set()

_env: Environment = Environment(
    loader=ChoiceLoader([
        DictLoader({"base.html": "{% block title %}{% endblock %}|{% block content %}{% endblock %}"}),  # the page frame is not under test
        FileSystemLoader(str(TEMPLATES)),
    ]),
    autoescape=True,  # as the app's Jinja2Templates does for .html
)


def _render(name: str, **context: Any) -> str:
    return _env.get_template(name).render(**context)


def _open_tags(html: str, tag: str, marker: str) -> list[str]:
    """Every opening <tag ...> in `html` that contains `marker`."""
    return [m for m in re.findall(rf"<{tag}\b[^>]*>", html, flags=re.S) if marker in m]


def _is_disabled(open_tag: str) -> bool:
    return re.search(r"\sdisabled\b", open_tag) is not None  # \s: aria-disabled does not count


def _attr(open_tag: str, name: str) -> str:
    match: re.Match[str] | None = re.search(rf'{name}="([^"]*)"', open_tag)
    assert match is not None, f"{name} not found in {open_tag!r}"
    return match.group(1)


# ---------------------------------------------------------------------------
# TimescaleDB: tables listed on the Metrics Edit and Delete Columns screens
# ---------------------------------------------------------------------------

_METRICS_EDIT_TABLES: list[dict[str, str | None]] = [
    {"table_kind": "narrow", "protocol_name": None, "table_name": "device_metrics_narrow"},
    {"table_kind": "wide", "protocol_name": "eg4_18kpv", "table_name": "device_metrics_wide__eg4_18kpv"},
    {"table_kind": "wide", "protocol_name": "eg4_ll_s", "table_name": "device_metrics_wide__eg4_ll_s"},
]


@pytest.mark.parametrize(
    ("linked", "expected_disabled"),
    [
        (FED, {"device_metrics_wide__eg4_18kpv"}),
        (None, NOTHING_DISABLED),  # the lookup failed: restrict nothing
        ([], {"device_metrics_narrow", "device_metrics_wide__eg4_18kpv", "device_metrics_wide__eg4_ll_s"}),
    ],
)
def test_metrics_edit_disables_only_tables_no_scraper_writes(linked: list[str] | None, expected_disabled: set[str]) -> None:
    html: str = _render("pages/timescale_metrics_edit.html", tables=_METRICS_EDIT_TABLES, linked_protocols=linked)
    buttons: list[str] = _open_tags(html, "button", "metrics-edit-table-btn")
    assert len(buttons) == 3
    assert {_attr(b, "data-table-name") for b in buttons if _is_disabled(b)} == expected_disabled
    for button in buttons:
        if _is_disabled(button):
            assert "hx-get" not in button and "onclick" not in button  # nothing to click: no request, no selection
        else:
            assert "hx-get" in button and "onclick" in button


@pytest.mark.parametrize(
    ("linked", "expected_disabled", "auto_loads"),
    [
        (FED, {"eg4_18kpv"}, "eg4_ll_s"),  # the first table is disabled, so the first selectable one auto-loads
        (None, NOTHING_DISABLED, "eg4_18kpv"),
        ([], {"eg4_18kpv", "eg4_ll_s"}, None),
    ],
)
def test_delete_columns_disables_unfed_tables_and_never_auto_loads_one(
    linked: list[str] | None, expected_disabled: set[str], auto_loads: str | None
) -> None:
    wide: list[dict[str, str]] = [
        {"protocol_name": "eg4_18kpv", "wide_table_name": "device_metrics_wide__eg4_18kpv"},
        {"protocol_name": "eg4_ll_s", "wide_table_name": "device_metrics_wide__eg4_ll_s"},
    ]
    html: str = _render("pages/timescale_delete_columns.html", wide_tables=wide, linked_protocols=linked)
    buttons: list[str] = _open_tags(html, "button", "wide-table-btn")
    assert {_attr(b, "data-protocol") for b in buttons if _is_disabled(b)} == expected_disabled
    loaders: list[str] = [_attr(b, "data-protocol") for b in buttons if 'hx-trigger="load' in b]
    assert loaders == ([auto_loads] if auto_loads else [])
    assert all("hx-get" not in b for b in buttons if _is_disabled(b))


# ---------------------------------------------------------------------------
# TimescaleDB: rebuild screens, Timeshift
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("template", "checkbox_class"),
    [
        ("partials/timescale_rollup_view_list.html", "rollup-group-checkbox"),
        ("partials/timescale_compression_group_list.html", "compression-group-checkbox"),
    ],
)
@pytest.mark.parametrize(
    ("linked", "expected_disabled"),
    [(FED, {"eg4_18kpv"}), (None, NOTHING_DISABLED), ([], {"shared_narrow", "eg4_18kpv", "eg4_ll_s"})],
)
def test_rebuild_screens_disable_and_uncheck_groups_no_scraper_writes(
    template: str, checkbox_class: str, linked: list[str] | None, expected_disabled: set[str]
) -> None:
    groups: list[dict[str, object]] = [
        {
            "protocol_name": name, "wide_table_name": f"table_{name}", "views": [], "tables": [],
            "compressed_chunks": 1, "total_chunks": 2, "size_display": "1 kB", "raw_size_display": "1 kB",
        }
        for name in ("shared_narrow", "eg4_18kpv", "eg4_ll_s")
    ]
    html: str = _render(template, groups=groups, linked_protocols=linked)
    boxes: list[str] = _open_tags(html, "input", checkbox_class)
    assert len(boxes) == 3
    for box in boxes:
        disabled: bool = _is_disabled(box)
        assert disabled == (_attr(box, "data-protocol-name") in expected_disabled)
        # A disabled box is never checked, so it can't be submitted; an enabled one keeps the default of checked.
        assert (re.search(r"\n\s+checked\n", box) is not None) == (not disabled)


@pytest.mark.parametrize("page", ["pages/timescale_rebuild_rollups.html", "pages/timescale_rebuild_compression.html"])
def test_rebuild_pages_select_all_and_submit_skip_disabled_boxes(page: str) -> None:
    source: str = (TEMPLATES / page).read_text(encoding="utf-8")
    assert "if (!cb.disabled) { cb.checked = checked; }" in source  # "Select all" can't check a disabled box
    assert ":checked:not(:disabled)" in source  # and what gets submitted can't include one


@pytest.mark.parametrize(
    ("linked", "expected_disabled"),
    [(FED, {"device_metrics_wide__eg4_18kpv"}), (None, NOTHING_DISABLED), ([], {"device_metrics_narrow", "device_metrics_wide__eg4_18kpv", "device_metrics_wide__eg4_ll_s"})],
)
def test_timeshift_table_select_disables_options_no_scraper_writes(linked: list[str] | None, expected_disabled: set[str]) -> None:
    html: str = _render(
        "pages/timeshift_data.html", version="timescale", timescale_tables=_METRICS_EDIT_TABLES,
        standard_tag_keys=[], linked_protocols=linked,
    )
    options: list[str] = _open_tags(html, "option", "data-table-kind")
    assert len(options) == 3
    assert {_attr(o, "value") for o in options if _is_disabled(o)} == expected_disabled


# ---------------------------------------------------------------------------
# InfluxDB: measurements and devices
# ---------------------------------------------------------------------------

def test_influx_metrics_edit_disables_measurements_no_scraper_writes() -> None:
    html: str = _render(
        "pages/influxdb_metrics_edit.html", version="3", measurements=["device_data", "old_measurement"],
        unselectable_measurements=["old_measurement"], delete_offered=True, staged=[],
    )
    buttons: list[str] = _open_tags(html, "button", "influx-metrics-edit-measurement-btn")
    assert [(_attr(b, "data-measurement") if "data-measurement" in b else "device_data", _is_disabled(b)) for b in buttons] == [
        ("device_data", False), ("old_measurement", True),
    ]
    assert "hx-get" not in buttons[1] and "onclick" not in buttons[1]


def test_influx_devices_partial_disables_devices_no_scraper_writes() -> None:
    devices: list[dict[str, str | None]] = [
        {"device_identifier": "4066670074", "device_name": "EG4 18kpv1"},
        {"device_identifier": "2001ab", "device_name": None},
    ]
    html: str = _render(
        "partials/influxdb_metrics_edit_devices.html", version="3", measurement="device_data",
        devices=devices, unselectable_devices=["2001ab"],
    )
    options: list[str] = [o for o in _open_tags(html, "option", "value=") if _attr(o, "value")]
    assert [(_attr(o, "value"), _is_disabled(o)) for o in options] == [("4066670074", False), ("2001ab", True)]


# ---------------------------------------------------------------------------
# service helpers
# ---------------------------------------------------------------------------

def _linked_lookup(result: list[str]) -> Callable[[object, object], list[str]]:
    def lookup(_gateway: object, _db: object) -> list[str]:
        return result

    return lookup


def _failing_lookup(_gateway: object, _db: object) -> list[str]:
    raise RuntimeError("settings database unavailable")


def test_is_protocol_linked() -> None:
    assert bs.is_protocol_linked(FED, "eg4_ll_s")
    assert not bs.is_protocol_linked(FED, "eg4_18kpv")
    assert bs.is_protocol_linked(FED, None)  # None is the shared narrow table
    assert not bs.is_protocol_linked([], None)  # nothing writes to this bridge at all
    assert bs.is_protocol_linked(None, "eg4_18kpv")  # unknown: restrict nothing


def test_require_timescale_protocols_linked_names_every_offender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "get_timescale_linked_protocols", _linked_lookup(FED))
    bs.require_timescale_protocols_linked(None, _FAKE_DB, ["eg4_ll_s", None])  # all fed: fine
    with pytest.raises(bs.NotBridgedError) as raised:
        bs.require_timescale_protocols_linked(None, _FAKE_DB, ["eg4_ll_s", "eg4_18kpv", "other"])
    assert "eg4_18kpv" in str(raised.value) and "other" in str(raised.value) and "eg4_ll_s" not in str(raised.value)


def test_a_failed_lookup_restricts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "get_timescale_linked_protocols", _failing_lookup)
    assert bs.timescale_linked_protocols_or_none(None, _FAKE_DB) is None
    bs.require_timescale_protocols_linked(None, _FAKE_DB, ["eg4_18kpv"])  # does not raise


def _gateway(*scrapers: tuple[list[str], str]) -> Any:
    transports: list[types.SimpleNamespace] = [types.SimpleNamespace(bridges=bridges, device_identifier=identifier) for bridges, identifier in scrapers]
    return types.SimpleNamespace(_Protocol_Gateway__transports=transports)


_INFLUX_BRIDGE: Any = types.SimpleNamespace(transport_name="transport.influxdb3", measurement="device_data")


def test_influx_feed_collects_the_identifiers_of_scrapers_linked_to_the_bridge() -> None:
    gateway = _gateway(
        (["transport.influxdb3"], "4066670074"),
        (["transport.influxdb3", "transport.timescaledb"], "2001AB"),  # identifiers are compared lower-cased
        (["transport.timescaledb"], "ffff"),  # linked to another bridge only
        (["transport.influxdb3"], ""),  # serial not known yet: no identifier to match
    )
    feed: bs.InfluxBridgeFeed | None = bs.get_influxdb_feed(gateway, _INFLUX_BRIDGE)
    assert feed is not None
    assert feed.measurement == "device_data"
    assert feed.has_scrapers
    assert feed.device_identifiers == frozenset({"4066670074", "2001AB"})


def test_influx_selectability_rules() -> None:
    feed = bs.InfluxBridgeFeed(measurement="device_data", device_identifiers=frozenset({"4066670074"}), has_scrapers=True)
    assert bs.influx_measurement_selectable(feed, "device_data")
    assert not bs.influx_measurement_selectable(feed, "old_measurement")
    assert bs.influx_device_selectable(feed, "device_data", "4066670074")
    assert not bs.influx_device_selectable(feed, "device_data", "2001ab")  # its scraper no longer links here
    assert not bs.influx_device_selectable(feed, "old_measurement", "4066670074")  # right device, measurement nobody writes
    unfed = bs.InfluxBridgeFeed(measurement="device_data", device_identifiers=frozenset(), has_scrapers=False)
    assert not bs.influx_measurement_selectable(unfed, "device_data")  # no scraper writes through this bridge at all
    assert bs.influx_measurement_selectable(None, "anything")  # no gateway to ask: restrict nothing
    assert bs.influx_device_selectable(None, "anything", "anything")


def test_influx_feed_is_unknown_without_a_gateway() -> None:
    assert bs.get_influxdb_feed(None, _INFLUX_BRIDGE) is None


def test_require_influx_device_fed() -> None:
    feed = bs.InfluxBridgeFeed(measurement="device_data", device_identifiers=frozenset({"4066670074"}), has_scrapers=True)
    bs.require_influx_device_fed(feed, "device_data", "4066670074")
    bs.require_influx_device_fed(None, "device_data", "whatever")
    with pytest.raises(bs.NotBridgedError, match="2001ab"):
        bs.require_influx_device_fed(feed, "device_data", "2001ab")


# ---------------------------------------------------------------------------
# endpoints refuse a request for something that isn't selectable
# ---------------------------------------------------------------------------

class _NoSession:
    """Stands in for `with session_scope() as db`: yields a placeholder, since the lookup it feeds is faked."""

    def __enter__(self) -> Any:
        return _FAKE_DB

    def __exit__(self, *_exc: object) -> bool:
        return False


_FAKE_DB: Any = object()


def _no_session() -> _NoSession:
    return _NoSession()


def _template_response(**kwargs: object) -> dict[str, object]:
    """Stands in for Jinja2Templates.TemplateResponse: hands back what it was asked to render."""
    return dict(kwargs)


def _request() -> Any:
    state = types.SimpleNamespace(gateway=None, templates=types.SimpleNamespace(TemplateResponse=_template_response))
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _return_bridge(*_args: object, **_kwargs: object) -> object:
    return types.SimpleNamespace()


def _zero(_state: object) -> int:
    return 0


def _table_t(*_args: object, **_kwargs: object) -> str:
    return "t"


def _no_field_types(*_args: object, **_kwargs: object) -> dict[str, str]:
    return {}


def _no_fields(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
    return []


@pytest.fixture
def fed_timescale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TimescaleDB bridge is up, and only eg4_ll_s (plus the shared narrow table) is written to it."""
    monkeypatch.setattr(bs, "get_timescale_linked_protocols", _linked_lookup(FED))
    monkeypatch.setattr(ts_router, "session_scope", _no_session)
    monkeypatch.setattr(ts_router, "_require_bridge", _return_bridge)
    monkeypatch.setattr(tshift_router, "session_scope", _no_session)


def test_the_guard_helper_returns_409_naming_the_unfed_table(fed_timescale: None) -> None:
    ts_router._require_protocols_linked(_request(), ["eg4_ll_s", None])  # fine
    with pytest.raises(HTTPException) as raised:
        ts_router._require_protocols_linked(_request(), ["eg4_ll_s", "eg4_18kpv"])
    assert raised.value.status_code == 409
    assert "eg4_18kpv" in str(raised.value.detail)


@pytest.mark.parametrize("endpoint", ["rebuild_rollups", "refresh_rollups", "rebuild_compression_endpoint"])
def test_rebuild_and_refresh_endpoints_refuse_an_unfed_group_and_accept_a_fed_one(fed_timescale: None, endpoint: str) -> None:
    models: dict[str, Any] = {
        "rebuild_rollups": ts_router.RebuildRollupsRequest,
        "refresh_rollups": ts_router.RefreshRollupsRequest,
        "rebuild_compression_endpoint": ts_router.RebuildCompressionRequest,
    }
    handler: Callable[..., Any] = getattr(ts_router, endpoint)
    with pytest.raises(HTTPException) as raised:
        handler(models[endpoint](protocol_names=["eg4_ll_s", "eg4_18kpv"]), _request())
    assert raised.value.status_code == 409
    assert isinstance(handler(models[endpoint](protocol_names=["eg4_ll_s"]), _request()), StreamingResponse)


def test_staging_a_column_deletion_is_refused_for_an_unfed_table_but_unstaging_is_not(
    fed_timescale: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def record(_state: object, *, checked: bool, **_kwargs: object) -> None:
        calls.append(checked)

    monkeypatch.setattr(ts_router, "stage_field_deletion", record)
    monkeypatch.setattr(ts_router, "staged_deletion_count", _zero)
    body = ts_router.StageFieldRequest

    with pytest.raises(HTTPException) as raised:
        ts_router.stage_field("eg4_18kpv", "col", body(checked=True, wide_table_name="w"), _request())
    assert raised.value.status_code == 409
    assert calls == []

    ts_router.stage_field("eg4_18kpv", "col", body(checked=False, wide_table_name="w"), _request())  # un-staging: always fine
    ts_router.stage_field("eg4_ll_s", "col", body(checked=True, wide_table_name="w"), _request())
    assert calls == [False, True]


def test_staging_a_metrics_edit_is_refused_for_an_unfed_table(fed_timescale: None) -> None:
    def stage(table_kind: str, protocol_name: str | None) -> Any:
        payload = ts_router.MetricEditStageRequest(
            table_kind=table_kind, protocol_name=protocol_name, table_name="t", device_info_id=1, device_label="d",
            field_names=["f"], start_time="2026-01-01T00:00", end_time="2026-01-02T00:00", action="set_value", new_value="1",
        )
        return ts_router.timescale_metrics_edit_stage(payload, _request())

    for kind, protocol in (("wide", "eg4_18kpv"),):
        with pytest.raises(HTTPException) as raised:
            stage(kind, protocol)
        assert raised.value.status_code == 409


def _timeshift_payload(table_kind: str, protocol_name: str | None) -> Any:
    return tshift_router.TimeshiftRunRequest(
        upload_id="u", version="timescale", measurement="t", time_column="time", mapping_json="{}", local_timezone="UTC",
        source_is_local=False, source_start="2026-01-01T00:00", target_start="2026-01-01T00:00",
        table_kind=table_kind, protocol_name=protocol_name, device_info_id=1,
    )


def test_timeshift_refuses_an_unfed_table_and_accepts_a_fed_one(fed_timescale: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tshift_router, "_require_bridge", _return_bridge)
    monkeypatch.setattr(tshift_router, "_require_upload", _return_bridge)
    monkeypatch.setattr(tshift_router, "_resolve_timescale_table_name", _table_t)
    monkeypatch.setattr(tshift_router, "load_timescale_field_types", _no_field_types)
    monkeypatch.setattr(tshift_router, "timescale_fields_for", _no_fields)

    with pytest.raises(HTTPException) as raised:
        tshift_router._run_common_timescale(_timeshift_payload("wide", "eg4_18kpv"), _request())
    assert raised.value.status_code == 409
    assert "eg4_18kpv" in str(raised.value.detail)

    tshift_router._run_common_timescale(_timeshift_payload("wide", "eg4_ll_s"), _request())
    tshift_router._run_common_timescale(_timeshift_payload("narrow", None), _request())  # the shared narrow table is fed


def _influx_stage_payload(measurement: str, device_identifier: str) -> Any:
    return influx_router.InfluxMetricEditStageRequest(
        measurement=measurement, device_identifier=device_identifier, device_label="d", field_name="f",
        start_time="2026-01-01T00:00", end_time="2026-01-02T00:00", action="set_value", new_value="1",
    )


def test_influx_staging_is_refused_for_a_device_or_measurement_no_scraper_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = bs.InfluxBridgeFeed(measurement="device_data", device_identifiers=frozenset({"4066670074"}), has_scrapers=True)
    staged: list[str] = []

    def stage(*_args: object, **kwargs: object) -> None:
        staged.append(str(kwargs["device_identifier"]))

    def bridge(*_args: object, **_kwargs: object) -> Any:
        return _INFLUX_BRIDGE

    def machine_timezone(*_args: object, **_kwargs: object) -> str:
        return "UTC"

    def no_staged_edits(_state: object) -> list[object]:
        return []

    def the_feed(*_args: object, **_kwargs: object) -> bs.InfluxBridgeFeed:
        return feed

    monkeypatch.setattr(influx_router, "_require_bridge", bridge)
    monkeypatch.setattr(influx_router, "get_influxdb_feed", the_feed)
    monkeypatch.setattr(influx_router, "_machine_timezone", machine_timezone)
    monkeypatch.setattr(influx_router, "stage_metric_edit", stage)
    monkeypatch.setattr(influx_router, "get_staged_metric_edits", no_staged_edits)

    for measurement, identifier in (("device_data", "2001ab"), ("old_measurement", "4066670074")):
        with pytest.raises(HTTPException) as raised:
            influx_router.influxdb_metrics_edit_stage(_influx_stage_payload(measurement, identifier), _request(), "3")
        assert raised.value.status_code == 409
    assert staged == []

    influx_router.influxdb_metrics_edit_stage(_influx_stage_payload("device_data", "4066670074"), _request(), "3")
    assert staged == ["4066670074"]
