# Description: Unit tests for the Home Assistant attribute inference helpers and the tools/add_ha_columns.py CSV migration script.
# File: test_add_ha_columns.py
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

"""Tests for ``classes.ha_metadata`` and ``tools/add_ha_columns.py``."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from classes.ha_metadata import infer_ha_attributes, split_unit_symbol

ROOT = Path(__file__).resolve().parents[1]


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("add_ha_columns", ROOT / "tools" / "add_ha_columns.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["add_ha_columns"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


# ---------------------------------------------------------------------------
# inference
# ---------------------------------------------------------------------------
def test_split_unit_symbol_strips_multiplier_and_enum_descriptions() -> None:
    """The unit cell may carry a multiplier; enum-style descriptions have no symbol."""
    assert split_unit_symbol("0.1V") == "V"
    assert split_unit_symbol("-1A") == "A"
    assert split_unit_symbol("kWh") == "kWh"
    assert split_unit_symbol("0:Off or 1:On") == ""
    assert split_unit_symbol("") == ""


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("0.1V", ("voltage", "measurement", "")),
        ("kWH", ("energy", "total_increasing", "")),
        ("Â°C", ("temperature", "measurement", "")),
        ("0.01Hz", ("frequency", "measurement", "")),
        ("%", ("", "measurement", "")),
        ("cycles", ("", "", "")),
        ("", ("", "", "")),
    ],
)
def test_infer_from_unit(unit: str, expected: tuple[str, str, str]) -> None:
    """Unit symbols drive device_class and state_class."""
    assert infer_ha_attributes(variable_name="x", unit=unit) == expected


def test_infer_text_and_enum_values_get_no_classes() -> None:
    """A unit on a text/enum register must not become a numeric class."""
    assert infer_ha_attributes(variable_name="x", unit="V", data_type="ASCII") == ("", "", "")
    assert infer_ha_attributes(variable_name="x", unit="V", data_type="16BIT_FLAGS") == ("", "", "")
    assert infer_ha_attributes(variable_name="x", unit="V", values='{"0":"a"}') == ("", "", "")
    assert infer_ha_attributes(variable_name="x", unit="V", is_enum=True) == ("", "", "")


def test_infer_battery_only_for_real_soc_readings() -> None:
    """SOC readings are batteries; thresholds, setpoints and writable settings are not."""
    assert infer_ha_attributes(variable_name="battery_soc", unit="%")[0] == "battery"
    assert infer_ha_attributes(variable_name="state_of_charge", unit="%")[0] == "battery"
    for setting in ("overcharge_soc", "force_charge_soc", "backup_soc", "soc_limit"):
        assert infer_ha_attributes(variable_name=setting, unit="%")[0] == ""
    assert infer_ha_attributes(variable_name="soc", unit="%", writable="RW")[0] == ""
    assert infer_ha_attributes(variable_name="soc", unit="V")[0] == "voltage"  # unit wins over name


def test_infer_power_factor_skips_scale_factors_and_setpoints() -> None:
    """A SunSpec scale factor or a setpoint is not a power factor reading."""
    assert infer_ha_attributes(variable_name="power_factor", unit="") == ("power_factor", "measurement", "")
    assert infer_ha_attributes(variable_name="pf_t", unit="")[0] == "power_factor"
    assert infer_ha_attributes(variable_name="ac_power_factor_sf", unit="")[0] == ""
    assert infer_ha_attributes(variable_name="power_factor_setpoint", unit="")[0] == ""


def test_infer_entity_category() -> None:
    """Read-only device-information registers are diagnostic; settings are left alone unless asked."""
    assert infer_ha_attributes(variable_name="firmware_version", unit="")[2] == "diagnostic"
    assert infer_ha_attributes(variable_name="serial_number_part_1", unit="")[2] == "diagnostic"
    assert infer_ha_attributes(variable_name="vpv1", unit="V")[2] == ""
    assert infer_ha_attributes(variable_name="model", unit="", writable="RW")[2] == ""
    assert infer_ha_attributes(variable_name="charge_limit", unit="A", writable="RW")[2] == ""
    assert infer_ha_attributes(variable_name="charge_limit", unit="A", writable="RW", config_writable=True)[2] == "config"


# ---------------------------------------------------------------------------
# the script
# ---------------------------------------------------------------------------
HEADER = "register;variable_name;documented_name;unit;data_type;values;writable"
ROWS = [
    "1;vpv1;Vpv1;0.1V;;0-65535;R",
    "2;soc;Soc;%;;0-100;R",
    "3;fw_version;Firmware Version;;ASCII;0-65535;R",
    "4;work_mode;Work Mode;;;0-1;RW",
    "5;energy_today;Energy Today;0.1kWH;;0-65535;R",
    "6;temp;Temp;\u00c2\u00b0C;;0-65535;R",       # mojibake exactly as latin-1 reading of UTF-8 gives
    "#7;skipped;Skipped;V;;0-1;R",
    "8;disabled_thing;Disabled Thing;V;;0-1;D",
    "",
]


def _make_protocols(tmp_path: Path, *, eol: str = "\r\n") -> Path:
    folder = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True)
    (folder / "acme_x.json").write_text(json.dumps({"work_mode_codes": {"0": "Self", "1": "Grid"}}))
    (folder / "acme_x.input_registry_map.csv").write_bytes((eol.join([HEADER, *ROWS])).encode("latin-1"))
    (folder / "notes.csv").write_text("a,b\n1,2\n")  # not a registry map
    return tmp_path / "protocols"


def _read(path: Path) -> list[dict[str, str]]:
    text = path.read_bytes().decode("latin-1")
    return list(csv.DictReader(io.StringIO(text, newline=""), delimiter=";"))


def _csv(tmp_path: Path) -> Path:
    return tmp_path / "protocols" / "acme" / "acme_x.input_registry_map.csv"


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Without --apply the files are untouched and the summary says so."""
    protocols = _make_protocols(tmp_path)
    before = _csv(tmp_path).read_bytes()

    assert tool.main([str(protocols)]) == 0

    assert _csv(tmp_path).read_bytes() == before
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_adds_columns_and_fills_inferred_values(tmp_path: Path) -> None:
    """The three columns are appended (underscore style, like this file) and populated."""
    protocols = _make_protocols(tmp_path)

    assert tool.main([str(protocols), "--apply"]) == 0

    rows = {r["variable_name"]: r for r in _read(_csv(tmp_path))}
    assert list(rows["vpv1"])[-3:] == ["ha_device_class", "ha_state_class", "ha_entity_category"]
    assert (rows["vpv1"]["ha_device_class"], rows["vpv1"]["ha_state_class"]) == ("voltage", "measurement")
    assert rows["soc"]["ha_device_class"] == "battery"
    assert rows["energy_today"]["ha_state_class"] == "total_increasing"
    assert rows["temp"]["ha_device_class"] == "temperature"      # mojibake unit understood
    assert rows["fw_version"]["ha_entity_category"] == "diagnostic"
    assert rows["fw_version"]["ha_device_class"] == ""           # text value
    assert rows["work_mode"]["ha_device_class"] == ""            # enum from the protocol JSON
    assert rows["work_mode"]["ha_entity_category"] == ""         # writable: left alone by default
    assert rows["disabled_thing"]["ha_device_class"] == ""       # never published
    assert rows["skipped"]["ha_device_class"] == ""                # commented-out row ("#7" register)


def test_apply_preserves_encoding_delimiter_and_line_endings(tmp_path: Path) -> None:
    """Existing cells and the file's byte-level conventions survive the rewrite."""
    protocols = _make_protocols(tmp_path)
    original = _read(_csv(tmp_path))

    tool.main([str(protocols), "--apply"])

    raw = _csv(tmp_path).read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no mixed line endings introduced
    assert "\u00c2\u00b0C".encode("latin-1") in raw  # the mis-encoded unit is byte-for-byte unchanged
    after = _read(_csv(tmp_path))
    for old, new in zip(original, after, strict=True):
        for key, value in old.items():
            assert new[key] == value


def test_second_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Idempotent: re-running (e.g. in CI) produces no further changes."""
    protocols = _make_protocols(tmp_path)
    tool.main([str(protocols), "--apply"])
    first = _csv(tmp_path).read_bytes()
    capsys.readouterr()

    tool.main([str(protocols), "--apply"])

    assert _csv(tmp_path).read_bytes() == first
    assert "files changed          0" in capsys.readouterr().out


def test_hand_edited_cells_survive_unless_overwrite(tmp_path: Path) -> None:
    """A value a person set is never replaced by a guess, unless --overwrite is passed."""
    protocols = _make_protocols(tmp_path)
    tool.main([str(protocols), "--apply"])
    path = _csv(tmp_path)
    text = path.read_bytes().decode("latin-1").replace(";voltage;measurement;", ";battery;total;", 1)
    path.write_bytes(text.encode("latin-1"))

    tool.main([str(protocols), "--apply"])
    kept = {r["variable_name"]: r for r in _read(path)}["vpv1"]
    assert (kept["ha_device_class"], kept["ha_state_class"]) == ("battery", "total")

    tool.main([str(protocols), "--apply", "--overwrite"])
    replaced = {r["variable_name"]: r for r in _read(path)}["vpv1"]
    assert (replaced["ha_device_class"], replaced["ha_state_class"]) == ("voltage", "measurement")


def test_config_writable_flag_marks_settings(tmp_path: Path) -> None:
    """--config-writable puts writable registers in HA's 'config' category."""
    protocols = _make_protocols(tmp_path)

    tool.main([str(protocols), "--apply", "--config-writable"])

    rows = {r["variable_name"]: r for r in _read(_csv(tmp_path))}
    assert rows["work_mode"]["ha_entity_category"] == "config"
    assert rows["vpv1"]["ha_entity_category"] == ""


def test_space_style_headers_get_space_style_columns(tmp_path: Path) -> None:
    """New column names follow the file's own convention (spaces vs underscores)."""
    folder = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True)
    (folder / "p.input_registry_map.csv").write_text(
        "register,variable name,documented name,unit\n1,vpv1,Vpv1,V\n", encoding="latin-1"
    )

    tool.main([str(tmp_path / "protocols"), "--apply"])

    header = (folder / "p.input_registry_map.csv").read_text(encoding="latin-1").splitlines()[0]
    assert header == "register,variable name,documented name,unit,ha device class,ha state class,ha entity category"


def test_rows_with_extra_cells_stay_extra(tmp_path: Path) -> None:
    """A row longer than the header keeps its extra cells after the new columns."""
    folder = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True)
    path = folder / "p.input_registry_map.csv"
    path.write_text("register,variable name,documented name,unit\n1,vpv1,Vpv1,V,stray\n", encoding="latin-1")

    tool.main([str(tmp_path / "protocols"), "--apply"])

    row = path.read_text(encoding="latin-1").splitlines()[1].split(",")
    assert row[:4] == ["1", "vpv1", "Vpv1", "V"]
    assert row[4] == "voltage"
    assert row[-1] == "stray"
