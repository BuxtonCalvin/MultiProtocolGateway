# Description: Dependency-free helpers that map protocol register metadata (unit, name, type) to Home Assistant attributes; shared by the MQTT bridge and tools/add_ha_columns.py.
# File: ha_metadata.py
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

"""Home Assistant attribute inference, standard library only.

Kept free of any gateway imports so command-line tools can use it without
loading the transports (and their circular protocol_settings import).
"""

from __future__ import annotations

import re

# Normalized unit (lower-case, whitespace removed) -> (HA unit, device_class, state_class).
#
# Deliberately conservative. Home Assistant refuses to create an entity whose
# unit_of_measurement is not valid for its device_class, so a class is only
# inferred for unit strings every supported HA release accepts. Anything not
# listed keeps its unit and gets no inferred class — the CSV columns
# "ha device class" / "ha state class" can still set one explicitly.
HA_UNIT_TABLE: dict[str, tuple[str, str, str]] = {
    "v": ("V", "voltage", "measurement"),
    "mv": ("mV", "voltage", "measurement"),
    "a": ("A", "current", "measurement"),
    "ma": ("mA", "current", "measurement"),
    "w": ("W", "power", "measurement"),
    "kw": ("kW", "power", "measurement"),
    "va": ("VA", "apparent_power", "measurement"),
    "var": ("var", "reactive_power", "measurement"),
    "kvar": ("kvar", "", "measurement"),
    "wh": ("Wh", "energy", "total_increasing"),
    "kwh": ("kWh", "energy", "total_increasing"),
    "mwh": ("MWh", "energy", "total_increasing"),
    "hz": ("Hz", "frequency", "measurement"),
    "c": ("°C", "temperature", "measurement"),
    "°c": ("°C", "temperature", "measurement"),
    "℃": ("°C", "temperature", "measurement"),
    "celsius": ("°C", "temperature", "measurement"),
    "ah": ("Ah", "", "measurement"),
    "mah": ("mAh", "", "measurement"),
    "%": ("%", "", "measurement"),
    "ms": ("ms", "duration", ""),
    "s": ("s", "duration", ""),
    "min": ("min", "duration", ""),
    "h": ("h", "duration", ""),
    "hour": ("h", "duration", ""),
    "hours": ("h", "duration", ""),
}

# Name tokens that mark a read-only register as device information rather
# than a live measurement (Home Assistant's "diagnostic" entity category).
_DIAGNOSTIC_TOKENS: frozenset[str] = frozenset({
    "serial", "firmware", "fw", "version", "model", "hardware", "hw", "mac",
    "ssid", "rssi", "build", "manufacturer", "bootloader", "checksum",
})

# Name tokens that mark a register as a setting, threshold or SunSpec scale
# factor rather than a live reading. Name-based refinements (battery,
# power_factor, ...) are skipped for these: "overcharge_soc" is a threshold,
# not a battery level, and "*_sf" holds a scale factor, not a measurement.
_SETTING_TOKENS: frozenset[str] = frozenset({
    "limit", "setpoint", "threshold", "overcharge", "overdischarge", "force",
    "backup", "target", "start", "stop", "cutoff", "alarm", "protection",
    "reserve", "min", "max", "floor", "ceiling", "recovery", "sf",
})

_TEXT_TYPE_MARKERS: tuple[str, ...] = ("ASCII", "STRING", "HEX", "FLAGS")

# Values of the CSV "writable" column that the loader treats as writable /
# disabled (mirrors WriteMode.fromString in protocol_settings.py).
WRITABLE_ALIASES: frozenset[str] = frozenset({"R/W", "RW", "W", "YES", "WO"})
DISABLED_ALIASES: frozenset[str] = frozenset({"RD", "READDISABLED", "DISABLED", "D"})


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 text that was decoded as latin-1 (``Â°C`` -> ``°C``).

    Protocol CSVs are read as latin-1, so any non-ASCII unit symbol arrives
    mangled. Returns the input unchanged if it does not round-trip cleanly.
    """
    if "Â" in text or "Ã" in text:
        try:
            return text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def lookup_ha_unit(raw_unit: str) -> tuple[str, str, str]:
    """Map a protocol-CSV unit symbol to ``(ha_unit, device_class, state_class)``.

    Matching ignores case and spacing (``kWH``, ``k Wh`` and ``kwh`` are all
    ``kWh``). Unrecognized units are returned unchanged with no classes.
    """
    unit: str = repair_mojibake((raw_unit or "").strip())
    if not unit:
        return ("", "", "")
    if unit == "S":
        # Ambiguous in the wild (seconds vs siemens); never guess.
        return (unit, "", "")
    return HA_UNIT_TABLE.get(re.sub(r"\s+", "", unit).lower(), (unit, "", ""))


def split_unit_symbol(raw_unit: str) -> str:
    """The symbol part of a CSV ``unit`` cell, without its numeric multiplier.

    ``0.1V`` -> ``V``; ``-1A`` -> ``A``; ``kWh`` -> ``kWh``. Cells such as
    ``0:Off or 1:On`` (enum descriptions) yield an empty symbol.
    """
    text: str = (raw_unit or "").strip()
    if "or" in text.lower() or ":" in text:
        return ""
    return re.sub(r"^-?[0-9.]+", "", text).strip()


def infer_ha_attributes(
    *,
    variable_name: str,
    unit: str,
    data_type: str = "",
    values: str = "",
    writable: str = "",
    is_enum: bool = False,
    config_writable: bool = False,
) -> tuple[str, str, str]:
    """Best-guess ``(device_class, state_class, entity_category)`` for one register.

    Uses only what the protocol CSV already says: the unit symbol, the data
    type, whether the value is an enum, the writable flag, and the variable
    name. Every result is a suggestion a person can review and edit; blank
    means "nothing confident to say" (the bridge then falls back to its own
    runtime inference).

    ``unit`` may include a numeric multiplier (``0.1V``).
    """
    name: str = variable_name.strip().lower().replace(" ", "_")
    tokens: set[str] = set(name.split("_"))
    writable_flag: bool = writable.strip().upper() in WRITABLE_ALIASES
    type_text: str = data_type.strip().upper()
    text_value: bool = (
        is_enum
        or values.strip().startswith("{")
        or any(marker in type_text for marker in _TEXT_TYPE_MARKERS)
    )

    device_class = state_class = ""
    symbol: str = split_unit_symbol(unit)

    if not text_value:
        _, device_class, state_class = lookup_ha_unit(symbol)
        normalized: str = lookup_ha_unit(symbol)[0]

        # Name-based refinements, only where the unit makes them valid in HA
        # and the register is a live reading rather than a setting.
        is_reading: bool = not writable_flag and not (tokens & _SETTING_TOKENS)
        if is_reading and not device_class and normalized == "%":
            if "soc" in tokens or "state_of_charge" in name:
                device_class = "battery"
            elif "humidity" in tokens:
                device_class = "humidity"
        elif is_reading and not device_class and normalized == "" and (
            "power_factor" in name or "pf" in tokens
        ):
            device_class, state_class = "power_factor", "measurement"

    category = ""
    if writable_flag:
        category = "config" if config_writable else ""
    elif tokens & _DIAGNOSTIC_TOKENS:
        category = "diagnostic"

    return device_class, state_class, category


# ---------------------------------------------------------------------------
# Permissible values for the three protocol-CSV columns.
#
# Single source of truth for server-side validation, the web UI dropdowns and
# the Create Protocol wizard, so a value the UI offers is always one the
# validator accepts and the MQTT bridge can publish.
# ---------------------------------------------------------------------------

HA_COLUMNS: tuple[str, ...] = ("ha_device_class", "ha_state_class", "ha_entity_category")

# Short table-header labels for the web UI.
HA_COLUMN_LABELS: dict[str, str] = {
    "ha_device_class": "HA Class",
    "ha_state_class": "HA State",
    "ha_entity_category": "HA Category",
}

# Longer descriptions used for tooltips / help text.
HA_COLUMN_TITLES: dict[str, str] = {
    "ha_device_class": (
        "Home Assistant device class. Blank = inferred from the unit; "
        "'none' = send no class."
    ),
    "ha_state_class": (
        "Home Assistant state class (sensors only). Blank = inferred from the unit; "
        "'none' = send no class."
    ),
    "ha_entity_category": (
        "Home Assistant entity category. Blank = none. 'config' only applies to "
        "writable controls; sensors accept 'diagnostic' only."
    ),
}

# Value that suppresses an inferred class (device / state class columns only).
HA_SUPPRESS: str = "none"

HA_STATE_CLASS_CHOICES: tuple[str, ...] = ("measurement", "total", "total_increasing")
HA_ENTITY_CATEGORY_CHOICES: tuple[str, ...] = ("diagnostic", "config")

# Deliberately a curated, conservative subset of Home Assistant's classes —
# the ones meaningful for energy / device-monitoring registers and stable across
# supported releases. Excluded on purpose: "enum" (requires an options list),
# "date" / "timestamp" (require specific payload formats) — publishing those
# from a plain numeric register would make Home Assistant reject the entity.
HA_SENSOR_DEVICE_CLASSES: tuple[str, ...] = (
    "apparent_power", "battery", "current", "duration", "energy", "frequency",
    "humidity", "illuminance", "irradiance", "power", "power_factor", "pressure",
    "reactive_power", "signal_strength", "temperature", "voltage",
)
HA_BINARY_SENSOR_DEVICE_CLASSES: tuple[str, ...] = (
    "battery", "battery_charging", "carbon_monoxide", "cold", "connectivity",
    "door", "garage_door", "gas", "heat", "light", "lock", "moisture", "motion",
    "moving", "occupancy", "opening", "plug", "power", "presence", "problem",
    "running", "safety", "smoke", "sound", "tamper", "update", "vibration",
    "window",
)
# The only device classes Home Assistant accepts on a switch.
HA_SWITCH_DEVICE_CLASSES: tuple[str, ...] = ("outlet", "switch")


def ha_device_class_choices(registry_type: str) -> tuple[str, ...]:
    """Device classes valid for the entity a register of this type becomes.

    Discrete inputs are always binary sensors. A coil is a binary sensor when
    read-only and a switch when write-enabled, so it may carry either set.
    Holding / input / other registers are sensors (or numbers, which accept
    the same classes).
    """
    kind: str = (registry_type or "").strip().lower()
    if kind == "discrete":
        return HA_BINARY_SENSOR_DEVICE_CLASSES
    if kind == "coil":
        return HA_BINARY_SENSOR_DEVICE_CLASSES + HA_SWITCH_DEVICE_CLASSES
    return HA_SENSOR_DEVICE_CLASSES


def ha_dropdown_options(
    column: str, registry_type: str = "", current: str | None = ""
) -> list[tuple[str, str]]:
    """``(value, label)`` pairs for the dropdown of one HA column.

    The first option is always the blank/auto value. If ``current`` is set but
    is not a permissible value (e.g. a hand-edited CSV using a class this list
    does not know) it is appended as ``<value> (custom)`` so the control shows
    the real stored value instead of silently displaying something else.
    """
    options: list[tuple[str, str]]
    if column == "ha_device_class":
        options = [("", "auto (from unit)"), (HA_SUPPRESS, "none (don't send)")]
        options += [(v, v) for v in ha_device_class_choices(registry_type)]
    elif column == "ha_state_class":
        options = [("", "auto (from unit)"), (HA_SUPPRESS, "none (don't send)")]
        options += [(v, v) for v in HA_STATE_CLASS_CHOICES]
    elif column == "ha_entity_category":
        options = [("", "— (none)")]
        options += [(v, v) for v in HA_ENTITY_CATEGORY_CHOICES]
    else:
        msg: str = f"Unknown Home Assistant column: {column!r}"
        raise ValueError(msg)

    stored: str = (current or "").strip().lower()
    if stored and stored not in {value for value, _ in options}:
        options.append((stored, f"{stored} (custom)"))
    return options


def validate_ha_value(
    column: str, value: str | None, *, registry_type: str = "", current: str | None = ""
) -> str:
    """Normalize and validate a value for one HA column.

    Returns the cleaned (stripped, lower-cased) value. Raises ``ValueError``
    with a user-presentable message if it is not permissible. An unchanged
    ``current`` value is always accepted so a row holding a legacy/custom value
    can still be edited elsewhere without being forced to change it.
    """
    cleaned: str = (value or "").strip().lower()
    if cleaned == "":
        return ""

    permitted: set[str] = {v for v, _ in ha_dropdown_options(column, registry_type) if v}
    if cleaned in permitted or cleaned == (current or "").strip().lower():
        return cleaned

    label: str = HA_COLUMN_LABELS.get(column, column)
    shown: str = ", ".join(sorted(permitted))
    msg: str = f"'{value}' is not a valid {label}. Choose one of: {shown}."
    raise ValueError(msg)
