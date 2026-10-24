"""Tests for the Flatpickr date/time boxes on the InfluxDB Metrics Edit and Timeshift Data pages.

Both pages (Timeshift serves InfluxDB 1.x / 3.x and TimescaleDB) now use the same compact "YYYY-MM-DD  HH:MM:SS" boxes as
the Timescale Metrics Edit page, driven by the shared static/js/mpg-datetime-picker.js. The server must accept that
format, and the End of a range must mean "through the end of that second" because stored timestamps carry sub-second
precision. The JavaScript helper is exercised in Node (skipped when Node isn't installed); the templates are rendered
from the real files.
"""

# These tests deliberately exercise private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import re
import shutil
import subprocess
import types
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from classes.WebServer.routers import influxdb as influx_router
from classes.WebServer.routers import timeshift as timeshift_router
from classes.WebServer.services import influxdb_service as influx_service

WEBSERVER: Path = Path(__file__).resolve().parents[1] / "classes" / "WebServer"
HELPER_JS: Path = WEBSERVER / "static" / "js" / "mpg-datetime-picker.js"
LA: ZoneInfo = ZoneInfo("America/Los_Angeles")

_env: Environment = Environment(
    loader=ChoiceLoader([
        DictLoader({"base.html": "{% block title %}{% endblock %}|{% block head %}{% endblock %}|{% block content %}{% endblock %}"}),
        FileSystemLoader(str(WEBSERVER / "templates")),
    ]),
    autoescape=True,
)

PARSERS: list[Any] = [influx_router._parse_local_datetime, timeshift_router._parse_local_datetime]


# ── server-side parsing ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("raw", [
    "2026-10-17  20:05:55",   # the picker's own format: two spaces
    "2026-10-17 20:05:55",    # one space, typed by hand
    "2026-10-17   20:05:55",  # any run of whitespace
    "2026-10-17T20:05:55",    # ISO, as an API client or the old datetime-local input would send
])
def test_parse_accepts_picker_and_iso_formats(parse: Any, raw: str) -> None:
    assert parse(raw, "America/Los_Angeles") == datetime(2026, 10, 17, 20, 5, 55, tzinfo=LA)


@pytest.mark.parametrize("parse", PARSERS)
def test_parse_minutes_only_and_garbage(parse: Any) -> None:
    assert parse("2026-10-17T20:05", "America/Los_Angeles") == datetime(2026, 10, 17, 20, 5, tzinfo=LA)
    with pytest.raises(HTTPException) as exc:
        parse("not a time", "America/Los_Angeles")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("parse", PARSERS)
def test_end_bound_covers_the_whole_second_and_start_is_exact(parse: Any) -> None:
    """Stored timestamps have sub-second precision; an END of ...:49 must include a point stamped ...:49.81."""
    point: datetime = datetime(2026, 10, 17, 20, 5, 49, 810000, tzinfo=LA)
    start: datetime = parse("2026-10-17  20:05:49", "America/Los_Angeles")
    end: datetime = parse("2026-10-17  20:05:49", "America/Los_Angeles", end_of_second=True)
    assert start.microsecond == 0 and start < point <= end and end.microsecond == 999999


def test_timeshift_time_shift_inputs_are_parsed_exactly() -> None:
    """Only the export's Source End is stretched to the end of its second; Source/Target Start stay exact so the
    shift (Target - Source) is the whole number of seconds the admin entered."""
    src: str = Path(timeshift_router.__file__).read_text(encoding="utf-8")
    assert src.count("end_of_second=True") == 1
    assert "end_dt: datetime = _parse_local_datetime(end_time, local_timezone, end_of_second=True)" in src


def test_influx_edit_preview_and_stage_both_stretch_the_end() -> None:
    src: str = Path(influx_router.__file__).read_text(encoding="utf-8")
    assert src.count("_parse_local_datetime(payload.end_time, tz_name, end_of_second=True)") == 2


# ── templates ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _assert_picker_assets(html: str) -> None:
    assert "/static/css/flatpickr.min.css" in html
    i_fp: int = html.index("/static/js/flatpickr.min.js")
    i_helper: int = html.index("/static/js/mpg-datetime-picker.js")
    assert i_fp < i_helper, "the helper needs flatpickr loaded first"
    assert 'type="datetime-local"' not in html


def test_influx_metrics_edit_page_uses_text_pickers() -> None:
    html: str = _env.get_template("pages/influxdb_metrics_edit.html").render(version="1")
    _assert_picker_assets(html)
    for box in ("influx-metrics-edit-start-time", "influx-metrics-edit-end-time"):
        assert re.search(rf'<input type="text"\s+id="{box}"', html)
        assert f"id: '{box}'" in html
    assert "end: true" in html
    assert "MpgDateTime.read('influx-metrics-edit-start-time')" in html


@pytest.mark.parametrize("version", ["1", "3", "timescale"])
def test_timeshift_page_uses_text_pickers_for_every_version(version: str) -> None:
    html: str = _env.get_template("pages/timeshift_data.html").render(version=version, standard_tag_keys=[], eg4_available=True)
    _assert_picker_assets(html)
    boxes: list[str] = ["ts-export-start", "ts-export-end", "ts-export-target", "ts-import-start", "ts-import-target"]
    for box in boxes:
        assert f'<input type="text" id="{box}"' in html
        assert f"id: '{box}'" in html
    assert "{ id: 'ts-export-end', end: true }" in html
    # every read of a time box goes through the helper; none still reads .value directly
    for box in boxes:
        assert f"getElementById('{box}').value" not in html


def test_influx_staged_list_shows_seconds() -> None:
    edit: dict[str, Any] = {
        "edit_id": "x", "version": "1", "measurement": "m", "device_identifier": "d", "device_label": "dev",
        "field_name": "pload", "start_time": datetime(2026, 10, 17, 20, 5, 49, tzinfo=LA),
        "end_time": datetime(2026, 10, 17, 20, 5, 49, 999999, tzinfo=LA), "action": "delete", "new_value": None,
    }
    html: str = _env.get_template("partials/influxdb_metrics_edit_staged.html").render(staged_edits=[edit])
    assert "2026-10-17  20:05:49 → 2026-10-17  20:05:49" in html


# ── the shared JavaScript helper (run in Node) ───────────────────────────────────────────────────────────────────────

_NODE_HARNESS: str = r"""
const fs = require('fs');
const boxes = {};
function box(id, value) { boxes[id] = { id, value: value || '' }; }
global.window = global;
global.document = { getElementById: (id) => boxes[id] || null, readyState: 'complete' };
eval(fs.readFileSync(process.argv[2], 'utf8'));          // defines window.MpgDateTime
const out = {};
out.parse = ['2026-10-17  20:05:55', '2026-10-17 20:05:55', '2026-10-17T20:05:55', '2026-10-17T20:05',
             '2026-02-30  10:00:00', '2026-13-01  10:00:00', '', 'garbage'].map((t) => {
  const d = MpgDateTime.parse(t); return d ? d.getHours() + ':' + d.getMinutes() + ':' + d.getSeconds() : null; });
box('a', '2026-10-17  20:05:55'); box('b', '2026-10-17T08:30:15'); box('c', ''); box('d', 'nope');
out.read = ['a', 'b', 'c', 'd', 'missing'].map((id) => MpgDateTime.read(id));
out.tidied = boxes.a.value;                              // no flatpickr loaded -> box text normalized to display format
MpgDateTime.set('c', '2026-10-17T20:05:55');             // what the EG4 "earliest" endpoint returns
out.set = boxes.c.value;
console.log(JSON.stringify(out));
"""


NODE: str | None = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_helper_parse_read_and_set_in_node(tmp_path: Path) -> None:
    harness: Path = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    # Fixed arguments only (the installed node binary, a script this test wrote, and a file in this repo).
    proc: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        [str(NODE), str(harness), str(HELPER_JS)], capture_output=True, text=True, timeout=30, check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    assert out["parse"] == ["20:5:55", "20:5:55", "20:5:55", "20:5:0", None, None, None, None]
    assert out["read"] == ["2026-10-17 20:05:55", "2026-10-17 08:30:15", "", None, ""]
    assert out["tidied"] == "2026-10-17  20:05:55"
    assert out["set"] == "2026-10-17  20:05:55"


# ── Timescale Metrics Edit now uses the shared helper too ────────────────────────────────────────────────────────────

def test_timescale_metrics_edit_has_no_inline_copy_of_the_picker() -> None:
    html: str = _env.get_template("pages/timescale_metrics_edit.html").render(tables=[], staged_edits=[], linked_protocols=None)
    _assert_picker_assets(html)
    for gone in ("parseMetricsEditTimestamp", "readMetricsEditTimestamp", "METRICS_EDIT_TS_RE", "initMetricsEditPickers = "):
        assert gone not in html
    assert "MpgDateTime.read('metrics-edit-start-time')" in html and "MpgDateTime.read('metrics-edit-end-time')" in html
    assert "{ id: 'metrics-edit-end-time', end: true }" in html
    assert "function setMetricsEditRangeFromRow(row)" in html


# ── InfluxDB preview: local (machine timezone) vs UTC ────────────────────────────────────────────────────────────────

def test_preview_sample_gives_machine_timezone_and_utc_forms() -> None:
    s = influx_service._preview_sample("2026-10-17T20:05:49.810365-07:00", 634)
    assert s["time_local"] == "2026-10-17  20:05:49"          # the zone the Start/End boxes are read in
    assert s["time_utc"] == "2026-10-18  03:05:49"            # same instant as InfluxDB stores it (date rolls over)
    assert s["time_iso"] == "2026-10-17T20:05:49.810365-07:00" and s["value"] == 634


def test_preview_sample_with_utc_machine_timezone_and_with_junk() -> None:
    s = influx_service._preview_sample("2026-10-17T20:05:49+00:00", 1)
    assert s["time_local"] == s["time_utc"] == "2026-10-17  20:05:49"
    blank = influx_service._preview_sample("", None)          # a point with no time comes back as ""
    assert blank["time_local"] == blank["time_utc"] == ""


@pytest.mark.parametrize("version", ["1", "3"])
def test_preview_metric_edit_builds_both_forms_for_v1_and_v3(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    def preview_metric_edit(*_args: Any) -> Any:
        return types.SimpleNamespace(
            row_count=1, sample=[types.SimpleNamespace(time_iso="2026-10-17T20:05:49.5-07:00", value=555)])

    fake: Any = types.SimpleNamespace(preview_metric_edit=preview_metric_edit)

    def manager(_gateway: object) -> Any:
        return fake

    monkeypatch.setattr(influx_service, "_v1_admin_manager", manager)
    monkeypatch.setattr(influx_service, "_v3_admin_manager", manager)
    out = influx_service.preview_metric_edit(None, version, "m", "dev", "pload", datetime(2026, 1, 1, tzinfo=LA), datetime(2026, 1, 2, tzinfo=LA))
    assert out["sample"][0]["time_local"] == "2026-10-17  20:05:49" and out["sample"][0]["time_utc"] == "2026-10-18  03:05:49"


def _influx_preview_html(timezone_name: str) -> str:
    preview: dict[str, Any] = {"row_count": 1, "sample": [
        {"time_iso": "x", "time_local": "2026-10-17  20:05:49", "time_utc": "2026-10-18  03:05:49", "value": 634}]}
    return _env.get_template("partials/influxdb_metrics_edit_preview.html").render(preview=preview, timezone_name=timezone_name)


def test_influx_preview_defaults_to_machine_timezone_with_a_utc_checkbox() -> None:
    html: str = _influx_preview_html("America/Los_Angeles")
    assert 'id="influx-preview-show-utc"' in html and 'onchange="setInfluxPreviewUtc(this.checked)"' in html
    assert "Time (America/Los_Angeles)" in html
    assert 'data-local="2026-10-17  20:05:49" data-utc="2026-10-18  03:05:49">2026-10-17  20:05:49</td>' in html


def test_influx_preview_has_no_checkbox_when_the_machine_timezone_is_already_utc() -> None:
    html: str = _influx_preview_html("UTC")
    assert "influx-preview-show-utc" not in html and "Time (UTC)" in html


_TOGGLE_HARNESS: str = r"""
const fs = require('fs');
const page = fs.readFileSync(process.argv[2], 'utf8');
// the page's own state object plus the two toggle functions, exactly as shipped
const code = page.slice(page.indexOf('const InfluxEditState = {'), page.indexOf('function selectInfluxMeasurement'));
global.window = { location: { pathname: '/pages/influxdb-metrics-edit/1' } };
function cell(local, utc) { return { dataset: { local, utc }, textContent: local }; }
const cells = [cell('2026-10-17  20:05:49', '2026-10-18  03:05:49'), cell('2026-10-17  20:05:34', '2026-10-18  03:05:34')];
const box = { checked: false };
const heading = { dataset: { localTz: 'America/Los_Angeles' }, textContent: 'Time (America/Los_Angeles)' };
let hasBox = true;
global.document = {
  getElementById: (id) => id === 'influx-preview-show-utc' ? (hasBox ? box : null) : id === 'influx-preview-time-heading' ? heading : null,
  querySelectorAll: () => cells,
};
eval(code + '; global.setInfluxPreviewUtc = setInfluxPreviewUtc; global.applyInfluxPreviewTz = applyInfluxPreviewTz; global.S = InfluxEditState;');
const out = { start: [cells[0].textContent, heading.textContent, S.showUtc] };
setInfluxPreviewUtc(true);
out.utc = [cells[0].textContent, cells[1].textContent, heading.textContent, box.checked];
setInfluxPreviewUtc(false);
out.local = [cells[0].textContent, heading.textContent, box.checked];
setInfluxPreviewUtc(true);                      // choice persists when a new preview is swapped in and re-applied
cells.forEach((c) => { c.textContent = c.dataset.local; }); box.checked = false; heading.textContent = 'Time (America/Los_Angeles)';
applyInfluxPreviewTz();
out.persisted = [cells[0].textContent, heading.textContent, box.checked];
hasBox = false; S.showUtc = true;               // machine timezone already UTC -> no checkbox -> never flips anything
cells.forEach((c) => { c.textContent = c.dataset.local; });
applyInfluxPreviewTz();
out.noBox = cells[0].textContent;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_influx_preview_toggle_script_in_node(tmp_path: Path) -> None:
    harness: Path = tmp_path / "toggle.js"
    harness.write_text(_TOGGLE_HARNESS, encoding="utf-8")
    page: Path = WEBSERVER / "templates" / "pages" / "influxdb_metrics_edit.html"
    proc: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        [str(NODE), str(harness), str(page)], capture_output=True, text=True, timeout=30, check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    assert out["start"] == ["2026-10-17  20:05:49", "Time (America/Los_Angeles)", False]
    assert out["utc"] == ["2026-10-18  03:05:49", "2026-10-18  03:05:34", "Time (UTC)", True]
    assert out["local"] == ["2026-10-17  20:05:49", "Time (America/Los_Angeles)", False]
    assert out["persisted"] == ["2026-10-18  03:05:49", "Time (UTC)", True]
    assert out["noBox"] == "2026-10-17  20:05:49"


# ── InfluxDB preview: click a row to fill Start / End ────────────────────────────────────────────────────────────────

def test_influx_preview_rows_are_clickable_and_carry_the_bridge_timezone_second() -> None:
    preview: dict[str, Any] = {"row_count": 2, "sample": [
        {"time_iso": "x", "time_local": "2026-10-17  20:05:49", "time_utc": "2026-10-18  03:05:49", "value": 634},
        {"time_iso": "y", "time_local": "2026-10-17  20:05:34", "time_utc": "2026-10-18  03:05:34", "value": 555},
    ]}
    html: str = _env.get_template("partials/influxdb_metrics_edit_preview.html").render(
        preview=preview, timezone_name="America/Los_Angeles")
    # data-ts is always the bridge-timezone value (what the Start/End boxes are read in), never the UTC form
    assert 'data-ts="2026-10-17  20:05:49"' in html and 'data-ts="2026-10-17  20:05:34"' in html
    assert 'data-ts="2026-10-18' not in html
    assert html.count('onclick="setInfluxRangeFromRow(this)"') == 2
    assert html.count('tabindex="0"') == 2 and "Click a row to set Start and End" in html


def test_influx_page_defines_the_row_click_handler() -> None:
    html: str = _env.get_template("pages/influxdb_metrics_edit.html").render(version="1")
    assert "function setInfluxRangeFromRow(row)" in html


_CLICK_HARNESS: str = r"""
const fs = require('fs');
const page = fs.readFileSync(process.argv[2], 'utf8');
const helper = fs.readFileSync(process.argv[3], 'utf8');
const boxes = {
  'influx-metrics-edit-start-time': { value: '' },
  'influx-metrics-edit-end-time': { value: '' },
  'influx-preview-time-heading': { dataset: { localTz: 'America/Los_Angeles' } },
};
const status = [];
global.window = global;                                  // the helper attaches MpgDateTime to window
global.location = { pathname: '/pages/influxdb-metrics-edit/1' };
global.document = { getElementById: (id) => boxes[id] || null, readyState: 'complete', querySelectorAll: () => [] };
function showInfluxStatus(message, isError) { status.push([message, isError]); }
eval(helper);
const code = page.slice(page.indexOf('const InfluxEditState = {'), page.indexOf('function selectInfluxMeasurement'));
eval(code + '; global.setInfluxRangeFromRow = setInfluxRangeFromRow; global.S = InfluxEditState;');

const out = {};
// 1. normal click
setInfluxRangeFromRow({ dataset: { ts: '2026-10-17  20:05:49' } });
out.normal = [boxes['influx-metrics-edit-start-time'].value, boxes['influx-metrics-edit-end-time'].value, status[0]];
out.read = [MpgDateTime.read('influx-metrics-edit-start-time'), MpgDateTime.read('influx-metrics-edit-end-time')];

// 2. "Show times in UTC" is ticked: the screen shows UTC, but the row's data-ts is still the bridge-timezone second
S.showUtc = true;
boxes['influx-metrics-edit-start-time'].value = ''; boxes['influx-metrics-edit-end-time'].value = '';
setInfluxRangeFromRow({ dataset: { ts: '2026-10-17  20:05:34', utc: '2026-10-18  03:05:34' } });
out.whileUtc = [boxes['influx-metrics-edit-start-time'].value, boxes['influx-metrics-edit-end-time'].value];

// 3. a row without a usable timestamp changes nothing and says nothing
const before = status.length;
boxes['influx-metrics-edit-start-time'].value = 'keep'; boxes['influx-metrics-edit-end-time'].value = 'keep';
setInfluxRangeFromRow({ dataset: { ts: '' } });
setInfluxRangeFromRow({ dataset: { ts: 'garbage' } });
out.invalid = [boxes['influx-metrics-edit-start-time'].value, boxes['influx-metrics-edit-end-time'].value, status.length - before];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_influx_row_click_script_in_node(tmp_path: Path) -> None:
    harness: Path = tmp_path / "click.js"
    harness.write_text(_CLICK_HARNESS, encoding="utf-8")
    page: Path = WEBSERVER / "templates" / "pages" / "influxdb_metrics_edit.html"
    proc: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        [str(NODE), str(harness), str(page), str(HELPER_JS)], capture_output=True, text=True, timeout=30, check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    assert out["normal"][:2] == ["2026-10-17  20:05:49", "2026-10-17  20:05:49"]
    assert out["normal"][2] == ["Start and End set to 2026-10-17  20:05:49 (America/Los_Angeles).", False]
    assert out["read"] == ["2026-10-17 20:05:49", "2026-10-17 20:05:49"]          # what is sent to the server
    assert out["whileUtc"] == ["2026-10-17  20:05:34", "2026-10-17  20:05:34"]     # local value, NOT the UTC 03:05:34
    assert out["invalid"] == ["keep", "keep", 0]
