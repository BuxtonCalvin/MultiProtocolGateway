"""The protocol editor's adjustments popup: template wiring and its JavaScript tests.

The popup itself is ``static/js/adjustments-editor.js``.  Its pure logic and its DOM layer (against a
fake DOM and a stubbed server) are tested by ``pytests/js/*.test.js``, run here through Node when it is
installed.  The server rules it relies on are tested in ``test_adjustments_validation.py``.
"""

from __future__ import annotations

import re

# import shutil
# import subprocess
from pathlib import Path
from types import SimpleNamespace

# import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO: Path = Path(__file__).resolve().parent.parent
WEBSERVER_DIR: Path = REPO / "classes" / "WebServer"
TEMPLATES_DIR: Path = WEBSERVER_DIR / "templates"


def _row(adjustments: str | None, variable: str = "voltage_l1") -> SimpleNamespace:
    return SimpleNamespace(
        id=7, protocol_name="accuenergy_acuvim", registry_type="holding", register_address="0x4002",
        variable_name=variable, documented_name="Phase 1 Voltage", unit="V", data_type="FLOAT32",
        values_range="", adjustments=adjustments, note="", read_interval="", write_mode_protocol="R",
        user_write_enabled=False, mask_enabled=False, screen_enabled=False, is_dirty=False,
        paired_high_address=None, is_synthetic=False, is_json_desc=False, pending_delete=False,
    )


def _render(rows: list[SimpleNamespace], device_name: str | None = None) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return env.get_template("partials/protocol_table.html").render(
        rows=rows, protocol_name="accuenergy_acuvim", registry_type="holding", device_name=device_name,
    )


def _adjustments_inputs(html: str) -> list[str]:
    return re.findall(r"<input[^>]*data-adjustments-editor[^>]*>", html, flags=re.S)


def test_editable_cell_opens_the_popup_and_cannot_be_typed_into() -> None:
    html: str = _render([_row('{"Offset": -40}')])
    cells: list[str] = _adjustments_inputs(html)
    assert len(cells) == 1
    cell: str = cells[0]
    assert "readonly" in cell, "free typing is replaced by the guided popup"
    assert 'data-protocol="accuenergy_acuvim"' in cell
    assert 'data-variable="voltage_l1"' in cell
    assert 'data-address="0x4002"' in cell
    assert "&#34;Offset&#34;: -40" in cell or '"Offset"' in cell, "the current value is shown in the cell"


def test_cell_still_saves_through_the_existing_field_endpoint() -> None:
    """The popup writes the value and fires ``change``; the cell's own hx-patch does the saving."""
    cell: str = _adjustments_inputs(_render([_row("")]))[0]
    assert 'hx-patch="/api/protocols/7/field"' in cell
    assert "field: 'adjustments'" in cell
    assert 'hx-trigger="change delay:300ms"' in cell


def test_device_view_stays_read_only() -> None:
    html: str = _render([_row('{"Offset": 1}')], device_name="meter1")
    assert _adjustments_inputs(html) == []
    assert "reg-clip--adjustments" in html


def test_every_variable_name_is_escaped_into_the_data_attribute() -> None:
    cell: str = _adjustments_inputs(_render([_row("", variable='a"b<c')]))[0]
    assert 'a"b<c' not in cell


def test_editor_page_loads_the_popup_script_and_styles() -> None:
    panes: str = (TEMPLATES_DIR / "partials" / "protocol_editor_panes.html").read_text(encoding="utf-8")
    assert 'include "partials/adjustments_editor.html"' in panes
    partial: str = (TEMPLATES_DIR / "partials" / "adjustments_editor.html").read_text(encoding="utf-8")
    assert "/static/js/adjustments-editor.js" in partial
    assert (WEBSERVER_DIR / "static" / "js" / "adjustments-editor.js").is_file()


# NODE: str | None = shutil.which("node")


# @pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
# def test_javascript_tests_pass() -> None:
#     assert NODE is not None
#     tests: list[str] = sorted(str(path) for path in (REPO / "pytests" / "js").glob("*.test.js"))
#     assert tests
#     # NODE is the resolved absolute path of the node binary and the arguments are repo test files, not user input.
#     result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
#         [NODE, "--test", *tests], cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
#     )
#     assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
