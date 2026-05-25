# Description: scanner.py — The authoritative source-of-truth builder for the staging DB.
# File: scanner.py
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

"""
scanner.py — The authoritative source-of-truth builder for the staging DB.

Runs on startup and whenever the file watcher fires.  Reads config.cfg and
the transports folder, then upserts Setting and ProtocolRegister rows using
a merge strategy:

  Merge:  New keys from disk are added to the DB with value_staged = value_disk.
          Existing rows keep their value_staged (user edits survive a rescan).
  Orphan: Keys in the DB that are no longer found in source are flagged
          is_orphan = True (not deleted — user must manually prune them).
"""

from __future__ import annotations

import ast
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Tuple

from sqlalchemy.engine.row import Row
from sqlalchemy.orm import Session

from .database import refresh_app_state, session_scope
from .models import AppState, DeviceProtocolSelection, ProtocolRegister, Setting

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known settings per transport base class  (used when AST scan finds nothing)
# These come from reading transport_base.__init__, modbus_base.__init__, etc.
# ---------------------------------------------------------------------------

TRANSPORT_BASE_KEYS: dict[str, str] = {
    "bridge": "",
    "device_location": "",
    "device_manufacturer": "",
    "device_model": "",
    "device_name": "",
    "device_serial_number": "",
    "log_level": "INFO",
    "max_precision": "2",
    "protocol_version": "",
    "read_interval": "15",
    "variable_mask": "",
    "variable_screen": "",
    "write_enabled": "false"
}

MODBUS_BASE_KEYS: dict[str, str] = {
    **TRANSPORT_BASE_KEYS,
    "batch_delay": "0.85",
    "disable_duration_hours": "12",
    "enable_register_failure_tracking": "true",
    "host": "",
    "max_failures_before_disable": "5",
    "max_retries_per_block": "3",
    "modbus_delay": "0.85",
    "port": "502",
    "retries": "3",
    "send_holding_register": "true",
    "send_input_register": "true",
    "timeout": "7",
}

MQTT_KEYS: dict[str, str] = {
    **TRANSPORT_BASE_KEYS,
    "base_topic": "home/device",
    "discovery_enabled": "false",
    "discovery_topic": "homeassistant",
    "error_topic": "/error",
    "holding_register_prefix": "",
    "host": "",
    "input_register_prefix": "",
    "json": "false",
    "pass": "",
    "port": "1883",
    "reconnect_attempts": "21",
    "reconnect_delay": "7",
    "user": "",
}

TIMESCALEDB_KEYS: dict[str, str] = {
    **TRANSPORT_BASE_KEYS,
    "auto_refresh_interval": "21600",
    "backlog_file_name": "no_connect_backlog",
    "backlog_storage_path": "timescaledb_backlog",
    "database": "solar1",
    "drop_after": "1 year",
    "enable_auto_refresh": "true",
    "enable_compression": "true",
    "enable_persistent_storage": "true",
    "enable_pushover": "false",
    "enable_rollups": "true",
    "force_float": "true",
    "host": "",
    "max_backlog_age": "86400",
    "max_backlog_size": "10000",
    "max_reconnect_delay": "300",
    "migrate_data": "true",
    "password": "",
    "port": "5431",
    "pushover_token": "",
    "pushover_user": "",
    "reconnect_attempts": "5",
    "reconnect_delay": "5",
    "stale_data_timeout": "300",
    "use_exponential_backoff": "true",
    "username": "",
}

INFLUXDB_KEYS: dict[str, str] ={
    "batch_size": "100",
    "batch_timeout": "10.0",
    "connection_timeout": "10",
    "database": "solar",
    "enable_persistent_storage": "True",
    "force_float": "True",
    "host": "localhost",
    "include_device_info": "True",
    "include_timestamp": "True",
    "max_backlog_age": "86400",
    "max_backlog_size": "10000",
    "max_reconnect_delay": "300.0",
    "measurement": "device_data",
    "password": "",
    "periodic_reconnect_interval": "14400.0",
    "persistent_storage_path": "influxdb_backlog",
    "port": "8086",
    "reconnect_attempts": "5",
    "reconnect_delay": "5.0",
    "use_exponential_backoff": "True",
    "username": ""
}

KNOWN_TRANSPORT_KEYS: dict[str, dict[str, str]] = {
    "modbus_tcp": MODBUS_BASE_KEYS,
    "modbus_eg4_ll_s_tcp": {**MODBUS_BASE_KEYS, "slave_id": ""},
    "mqtt": MQTT_KEYS,
    "timescaledb": TIMESCALEDB_KEYS,
    "influxdb_out": INFLUXDB_KEYS,
}


# ---------------------------------------------------------------------------
# Config parser  (reuses the CustomConfigParser from protocol_gateway)
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict[str, dict[str, str]]:
    """
    Parse config.cfg using the same CustomConfigParser as the gateway.
    Returns {section: {key: value}}.
    Falls back to stdlib ConfigParser if the import fails.
    """
    try:
        # Add project root to path so we can import from protocol_gateway
        root: Path = config_path.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from protocol_gateway import CustomConfigParser
        parser = CustomConfigParser()
    except ImportError:
        from configparser import ConfigParser
        parser = ConfigParser()

    parser.read(str(config_path))
    removed_keys = {"analyze_protocol", "analyze_protocol_save_load"}
    result: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        result[section] = {}
        for key, value in parser.items(section):
            if key in removed_keys:
                continue
            # Strip inline comments
            value = value.split("#")[0].strip()
            result[section][key] = value
    return result


# ---------------------------------------------------------------------------
# Transport classification
# ---------------------------------------------------------------------------

def _classify_transport(section: str, keys: dict[str, str], transports_dir: Path) -> str:
    """
    Returns "scraper", "bridge", or "general".

    Classification is declared by the transport class itself via its
    class-level transport_type attribute. This avoids relying on comments
    while still keeping the scanner import-free.
    """
    transport_type: str = keys.get("transport", "").strip()
    if transport_type:
        py_file: Path = transports_dir / f"{transport_type}.py"
        classification = _transport_type_from_ast(py_file)
        if classification in ("scraper", "bridge"):
            return classification

    return "general"


def _transport_type_from_ast(py_file: Path) -> str:
    """
    Read a transport module's class-level transport_type value without
    importing the module. Importing transport modules can touch hardware,
    network clients, or optional database libraries, so AST parsing is the
    safest source of truth for WebServer discovery.
    """
    if not py_file.exists():
        return "base class"

    try:
        source: str = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        _log.warning(f"AST transport_type parse failed for {py_file.name}: {exc}")
        return "base class"

    valid_types = {"scraper", "bridge", "base class", "general"}
    module_stem = py_file.stem
    discovered: list[tuple[str, str]] = []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            target_name = ""
            value_node: ast.AST | None = None
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "transport_type":
                        target_name = target.id
                        value_node = stmt.value
                        break
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "transport_type":
                    target_name = stmt.target.id
                    value_node = stmt.value

            if target_name and isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                value = value_node.value.strip().lower()
                if value in valid_types:
                    discovered.append((node.name, value))

    for class_name, value in discovered:
        if class_name == module_stem:
            return value

    if discovered:
        return discovered[0][1]

    return "base class"


# ---------------------------------------------------------------------------
# AST settings-proxy scanner
# ---------------------------------------------------------------------------

def _extract_settings_keys_from_ast(py_path: Path) -> dict[str, str]:
    """
    Walk the AST of a transport .py file and find all patterns like:
        settings.get("key", ...)
        settings.getint("key", fallback=...)
        settings.getfloat("key", ...)
        settings.getboolean("key", ...)

    Returns {key: default_value_string}.
    """
    found: dict[str, str] = {}
    try:
        source: str = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        _log.warning(f"AST parse failed for {py_path.name}: {exc}")
        return found

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_node = node.func
        # Match: settings.get / settings.getint / settings.getfloat / settings.getboolean
        if not (isinstance(func_node, ast.Attribute)
                and func_node.attr in ("get", "getint", "getfloat", "getboolean")
                and isinstance(func_node.value, ast.Name)
                and func_node.value.id == "settings"):
            continue

        # First positional arg is the key (may be a string or a list)
        if not node.args:
            continue
        first_arg = node.args[0]

        keys_to_add: list[str] = []
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            keys_to_add.append(first_arg.value)
        elif isinstance(first_arg, ast.List):
            for elt in first_arg.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys_to_add.append(elt.value)

        # Extract fallback value if present
        default_val = ""
        for kw in node.keywords:
            if kw.arg == "fallback" and isinstance(kw.value, ast.Constant):
                default_val = str(kw.value.value)
                break
        # Also check positional arg[1] as a fallback
        if not default_val and len(node.args) >= 2:
            arg2 = node.args[1]
            if isinstance(arg2, ast.Constant):
                default_val = str(arg2.value)

        for k in keys_to_add:
            if k not in found:
                found[k] = default_val

    return found


def scan_transport_library(transports_dir: Path) -> dict[str, dict[str, Any]]:
    """
    Scan all .py files in the transports directory.
    Returns {filename_stem: {classification, keys: {key: default}}}.
    """
    result: dict[str, dict[str, Any]] = {}
    if not transports_dir.exists():
        _log.warning(f"Transports directory not found: {transports_dir}")
        return result

    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        stem: str = py_file.stem

        classification = _transport_type_from_ast(py_file)

        # Keys from AST scan of this file
        ast_keys = _extract_settings_keys_from_ast(py_file)

        if classification == "bridge":
            # Bridges: only expose the keys they explicitly read via settings.get(...).
            # Do NOT inject scraper-oriented base keys (protocol_version, read_interval,
            # variable_mask, device_location, bridge, analyze_protocol, etc.).
            merged: dict[str, str] = ast_keys
        else:
            # Scrapers and base classes: supplement AST with the known-keys table
            # so common Modbus/TCP keys are always present even if not in every file.
            known_keys = KNOWN_TRANSPORT_KEYS.get(stem, TRANSPORT_BASE_KEYS)
            merged = {**known_keys, **ast_keys}

        result[stem] = {
            "classification": classification,
            "keys": merged,
            "file": str(py_file),
        }

    return result


# ---------------------------------------------------------------------------
# Protocol scanner
# ---------------------------------------------------------------------------

def scan_protocols_dir(protocols_dir: Path) -> list[dict[str, Any]]:
    """
    Scan the protocols directory.
    Structure expected:  protocols/<group>/<protocol>.csv|json

    Returns a flat list of register dicts ready for DB upsert.
    """
    registers: list[dict[str, Any]] = []
    if not protocols_dir.exists():
        _log.warning(f"Protocols directory not found: {protocols_dir}")
        return registers

    for group_dir in sorted(protocols_dir.iterdir()):
        if not group_dir.is_dir():
            continue
        group_name: str = group_dir.name

        for proto_file in sorted(group_dir.iterdir()):
            if proto_file.suffix.lower() == ".csv":
                regs = _parse_protocol_csv(proto_file, group_name)
                registers.extend(regs)
            elif proto_file.suffix.lower() == ".json" and not proto_file.name.endswith(".override.json"):
                regs = _parse_protocol_json(proto_file, group_name)
                registers.extend(regs)

    _log.info(f"Protocol scan found {len(registers)} register entries across all protocols.")
    return registers


def _parse_protocol_csv(csv_path: Path, group_name: str) -> list[dict[str, Any]]:
    """
    Parse a protocol CSV file into a list of register dicts.

    Robustness rules
    ----------------
    * Delimiter auto-detected (comma vs semicolon).
    * Quoted fields handled by csv.reader so embedded commas in note/values
      columns do not corrupt the register address column.
    * Duplicate register addresses within the same file are deduplicated:
      the last row for a given address wins (matches protocol_settings.py
      behavior where later definitions override earlier ones).
    * Column names are normalized to snake_case and matched by multiple
      possible spellings (e.g. "Variable Name" / "variable name" / "variable_name").
    """
    protocol_name: str = csv_path.stem  # e.g. "eg4_18kpv_holding"

    # Detect holding vs input from filename
    name_lower: str = protocol_name.lower()
    if "holding" in name_lower:
        registry_type = "holding"
    elif "input" in name_lower:
        registry_type = "input"
    else:
        registry_type = "input"  # default

    # Ordered dict so later rows overwrite earlier ones for the same address.
    # This silently resolves duplicate rows in source CSVs (like the PF_S case).
    rows_by_address: dict[str, dict[str, Any]] = {}

    try:
        with open(csv_path, newline="", encoding="latin-1") as f:
            # ── Detect delimiter ──────────────────────────────────────────
            sample = f.read(4096)
            f.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","

            reader = csv.DictReader(
                f,
                delimiter=delimiter,
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL,
                skipinitialspace=True,
            )
            if reader.fieldnames is None:
                _log.warning(f"CSV has no header row: {csv_path}")
                return []

            # ── Normalize header names ────────────────────────────────────
            # Maps original header → normalized snake_case key
            def _norm(s: str) -> str:
                return s.strip().lower().replace(" ", "_").replace("/", "_")

            norm_fields = {k: _norm(k) for k in reader.fieldnames if k is not None}

            # Canonical field aliases — maps normalized header → canonical key
            ALIASES: dict[str, str] = {
                # variable name spellings
                "variable_name":  "variable_name",
                "variable":       "variable_name",
                "var_name":       "variable_name",
                "varname":        "variable_name",
                # documented name
                "documented_name": "documented_name",
                "documented":      "documented_name",
                "doc_name":        "documented_name",
                "name":            "documented_name",
                # register
                "register":        "register",
                "reg":             "register",
                "address":         "register",
                "register_address":"register",
                # unit
                "unit":            "unit",
                "units":           "unit",
                # data type
                "data_type":       "data_type",
                "datatype":        "data_type",
                "type":            "data_type",
                # values / range
                "values":          "values",
                "value":           "values",
                "range":           "values",
                "values_range":    "values",
                # adjustments
                "adjustments":     "adjustments",
                # note
                "note":            "note",
                "notes":           "note",
                "description":     "note",
                "desc":            "note",
                # write mode
                "writable":        "write_mode",
                "write":           "write_mode",
                "r_w":             "write_mode",
                "r/w":             "write_mode",
                "write_mode":      "write_mode",
                # read interval
                "read_interval":   "read_interval",
                "read interval":   "read_interval",
                "interval":        "read_interval",
            }

            # Build final header → canonical mapping for this file
            header_to_canonical: dict[str, str] = {}
            for original, normed in norm_fields.items():
                canonical: str = ALIASES.get(normed, normed)
                header_to_canonical[original] = canonical

            # ── Parse rows ────────────────────────────────────────────────
            for raw_row in reader:
                # Map all headers to canonical names, strip whitespace
                row: dict[str, str] = {}
                for orig_key, value in raw_row.items():
                    if orig_key is None:
                        continue
                    canonical = header_to_canonical.get(orig_key, _norm(orig_key))
                    row[canonical] = (value or "").strip()

                register: str = row.get("register", "").strip()
                var_name: str = row.get("variable_name", "").strip()
                doc_name: str = row.get("documented_name", "").strip()

                # Skip empty, header-repeat, or comment rows
                if not register:
                    continue
                if not (var_name or doc_name):
                    continue
                if var_name.startswith("#") or doc_name.startswith("#"):
                    continue
                # Skip rows where register is literally the header word
                # (happens when CSV has a repeated header mid-file)
                if register.lower() in ("register", "reg", "address"):
                    continue

                clean_var: str = (var_name or doc_name).strip().lower().replace(" ", "_")

                write_mode: str = row.get("write_mode", "R").strip().upper() or "R"

                entry: dict[str, str] = {
                    "protocol_group":    group_name,
                    "protocol_name":     protocol_name,
                    "registry_type":     registry_type,
                    "register_address":  register,
                    "variable_name":     clean_var,
                    "documented_name":   doc_name or var_name,
                    "unit":              row.get("unit", ""),
                    "data_type":         row.get("data_type", ""),
                    "values_range":      row.get("values", ""),
                    "adjustments":       row.get("adjustments", ""),
                    "note":              row.get("note", ""),
                    "read_interval":     row.get("read_interval", ""),
                    "write_mode_protocol": write_mode,
                }

                # Last definition for a given address wins — resolves CSV duplicates
                rows_by_address[register] = entry

    except Exception as exc:
        _log.error(f"Error parsing protocol CSV {csv_path}: {exc}", exc_info=True)

    result = list(rows_by_address.values())
    _log.debug(f"Parsed {len(result)} unique registers from {csv_path.name}")
    return result


def _parse_protocol_json(json_path: Path, group_name: str) -> list[dict[str, Any]]:
    """Parse a protocol JSON file. Returns a minimal single-row entry per JSON."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        # JSON files are config, not register maps — represent as one entry
        return [{
            "protocol_group": group_name,
            "protocol_name": json_path.stem,
            "registry_type": "json",
            "register_address": "0",
            "variable_name": "_json_config",
            "documented_name": json_path.stem,
            "unit": "",
            "data_type": "json",
            "values_range": "",
            "adjustments": f"JSON config file: {len(data)} keys",
            "note": f"JSON config file: {len(data)} keys",
            "read_interval": "",
            "write_mode_protocol": "R",
        }]
    except Exception as exc:
        _log.error(f"Error parsing protocol JSON {json_path}: {exc}")
        return []


# ---------------------------------------------------------------------------
# DB upsert helpers
# ---------------------------------------------------------------------------

def _upsert_setting(
    db: Session,
    section: str,
    key: str,
    value_disk: str,
    transport_type: str,
    default_value: str = "",
    is_active: bool = True,
    cfg_is_truth: bool = False,
) -> Setting:
    """
    Upsert a Setting row using merge strategy:
    - If row exists: update value_disk, recompute is_dirty, leave value_staged alone.
    - If row is new: value_staged = value_disk (no staged edit yet).
    - If cfg_is_truth=True (startup scan when config changed, or post-rollback):
      also sync value_staged = value_disk so config is ground truth with no stale edits.
    """
    existing: Setting | None = (
        db.query(Setting)
        .filter(Setting.section == section, Setting.key == key)
        .first()
    )

    if existing:
        existing.value_disk = value_disk
        existing.default_value = default_value or existing.default_value
        existing.transport_type = transport_type
        existing.is_orphan = False
        existing.is_active = is_active
        if cfg_is_truth:
            existing.value_staged = value_disk
            existing.is_dirty = False
        else:
            existing.mark_dirty()
    else:
        existing = Setting(
            section=section,
            key=key,
            value_disk=value_disk,
            value_staged=value_disk,
            default_value=default_value,
            transport_type=transport_type,
            is_active=is_active,
            is_dirty=False,
            is_orphan=False,
        )
        db.add(existing)

    return existing


def _mark_orphaned_settings(db: Session, seen_keys: set[tuple[str, str]]) -> int:
    """
    Mark any Setting rows not in seen_keys as orphaned.
    Returns count of newly orphaned rows.
    """
    count = 0
    for row in db.query(Setting).all():
        key_tuple: tuple[str, str] = (row.section, row.key)
        if key_tuple not in seen_keys:
            if not row.is_orphan:
                row.is_orphan = True
                count += 1
    return count


def _upsert_protocol_register(db: Session, reg: dict[str, Any]) -> ProtocolRegister | None:
    """
    Upsert a ProtocolRegister row using a check-then-insert/update pattern.

    - If a matching row exists: update protocol fields, preserve user toggles.
    - If no match: insert a new row.
    - Uses a nested savepoint so a constraint error on one row does NOT roll
      back the entire scan session — the bad row is skipped with a warning.

    Returns the upserted row, or None if the row was skipped due to an error.
    """
    # Check-first approach avoids hitting the UNIQUE constraint on INSERT.
    # The constraint can still fire in a concurrent scenario, so we also
    # catch IntegrityError inside a savepoint as a backstop.
    existing = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == reg["protocol_name"],
            ProtocolRegister.registry_type == reg["registry_type"],
            ProtocolRegister.register_address == reg["register_address"],
        )
        .first()
    )

    if existing:
        # Row found — update protocol-sourced fields only, preserve user toggles
        existing.protocol_group    = reg["protocol_group"]
        if not existing.is_dirty:
            existing.variable_name     = reg["variable_name"]
            existing.documented_name   = reg["documented_name"]
            existing.unit              = reg.get("unit", "")
            existing.data_type         = reg.get("data_type", "")
            existing.values_range      = reg.get("values_range", "")
            existing.adjustments       = reg.get("adjustments", "")
            existing.note              = reg.get("note", "")
            existing.read_interval     = reg.get("read_interval", "")
            existing.write_mode_protocol = reg["write_mode_protocol"]
        # Note: user_write_enabled / mask_enabled / screen_enabled intentionally
        # not touched here — they are user-controlled toggles.
        return existing

    # Row not found — attempt INSERT inside a savepoint so a race-condition
    # duplicate (or any other IntegrityError) skips only this row.
    try:
        with db.begin_nested():   # SQLAlchemy SAVEPOINT
            new_row = ProtocolRegister(
                protocol_group       = reg["protocol_group"],
                protocol_name        = reg["protocol_name"],
                registry_type        = reg["registry_type"],
                register_address     = reg["register_address"],
                variable_name        = reg["variable_name"],
                documented_name      = reg["documented_name"],
                unit                 = reg.get("unit", ""),
                data_type            = reg.get("data_type", ""),
                values_range         = reg.get("values_range", ""),
                adjustments          = reg.get("adjustments", ""),
                note                 = reg.get("note", ""),
                read_interval        = reg.get("read_interval", ""),
                write_mode_protocol  = reg["write_mode_protocol"],
                # User-controlled toggle defaults
                user_write_enabled       = False,
                mask_enabled             = True,
                screen_enabled           = False,
                user_write_enabled_disk  = False,
                mask_enabled_disk        = True,
                screen_enabled_disk      = False,
                is_dirty                 = False,
            )
            db.add(new_row)
            db.flush()   # flush inside savepoint — raises here on constraint error

    except Exception as exc:
        # Savepoint automatically rolled back; outer transaction is still alive.
        _log.warning(
            f"Skipping register {reg['protocol_name']}/{reg['registry_type']}"
            f"@{reg['register_address']} ({reg.get('variable_name','?')}): {exc}"
        )
        return None

    else:
        return new_row


def _load_filter_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return {
            line.strip().lower().replace(" ", "_")
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        }
    except Exception as exc:
        _log.warning(f"Could not read filter file {path}: {exc}")
        return set()


def _load_override_names(
    protocols_dir: Path,
    protocol_name: str,
    config_dir: Path | None = None,
) -> set[str]:
    """
    Load write-enabled register names from the override CSV.
    Checks config_dir first (user override location), falls back to protocols_dir.
    This matches the JSON override lookup pattern so overrides survive updates.
    """
    # Check config_dir first (where commit writes overrides)
    override_path: Path | None = None
    if config_dir is not None:
        candidate = config_dir / f"{protocol_name}.override.csv"
        if candidate.exists():
            override_path = candidate
            _log.debug(f"Loading override from config_dir: {override_path}")

    # Fall back to protocols_dir search
    if override_path is None:
        for candidate in protocols_dir.rglob(f"{protocol_name}.override.csv"):
            override_path = candidate
            break

    if override_path is None or not override_path.exists():
        return set()

    names: set[str] = set()
    try:
        with open(override_path, newline="", encoding="utf-8", errors="replace") as f:
            reader: csv.DictReader[str] = csv.DictReader(f)
            for row in reader:
                value = (row.get("documented name") or row.get("variable_name") or "").strip()
                if value:
                    names.add(value.lower().replace(" ", "_"))
    except Exception as exc:
        _log.warning(f"Could not read override file {override_path}: {exc}")
    return names


def _upsert_device_protocol_selection(
    db: Session,
    device_name: str,
    protocol_row: ProtocolRegister,
    mask_names: set[str],
    screen_names: set[str],
    write_names: set[str],
) -> None:
    existing = (
        db.query(DeviceProtocolSelection)
        .filter(
            DeviceProtocolSelection.device_name == device_name,
            DeviceProtocolSelection.protocol_name == protocol_row.protocol_name,
            DeviceProtocolSelection.registry_type == protocol_row.registry_type,
            DeviceProtocolSelection.register_address == protocol_row.register_address,
        )
        .first()
    )

    var_key: str = protocol_row.variable_name.strip().lower().replace(" ", "_")
    doc_key: str = protocol_row.documented_name.strip().lower().replace(" ", "_")
    mask_enabled: bool = var_key in mask_names or doc_key in mask_names
    screen_enabled: bool = var_key in screen_names or doc_key in screen_names
    user_write_enabled: bool = var_key in write_names or doc_key in write_names

    if existing:
        existing.mask_enabled_disk = mask_enabled
        existing.screen_enabled_disk = screen_enabled
        existing.user_write_enabled_disk = user_write_enabled
        if not existing.is_dirty:
            existing.mask_enabled = mask_enabled
            existing.screen_enabled = screen_enabled
            existing.user_write_enabled = user_write_enabled
        existing.mark_dirty()
        return

    row = DeviceProtocolSelection(
        device_name=device_name,
        protocol_name=protocol_row.protocol_name,
        registry_type=protocol_row.registry_type,
        register_address=protocol_row.register_address,
        user_write_enabled=user_write_enabled,
        mask_enabled=mask_enabled,
        screen_enabled=screen_enabled,
        user_write_enabled_disk=user_write_enabled,
        mask_enabled_disk=mask_enabled,
        screen_enabled_disk=screen_enabled,
        is_dirty=False,
    )
    db.add(row)


def _sync_device_protocol_selections(
    db: Session,
    config_data: dict[str, dict[str, str]],
    project_root: Path,
    protocols_dir: Path,
) -> None:
    config_dir: Path = project_root / "config"

    for section, keys in config_data.items():
        if not section.startswith("transport."):
            continue

        device_name: str = section.removeprefix("transport.")
        protocol_version: str = keys.get("protocol_version", "").strip()
        if not protocol_version:
            continue

        mask_file: str = keys.get("variable_mask", f"variable_mask_{device_name}.txt").strip() or f"variable_mask_{device_name}.txt"
        screen_file: str = keys.get("variable_screen", f"variable_screen_{device_name}.txt").strip() or f"variable_screen_{device_name}.txt"

        mask_names: set[str] = _load_filter_names(config_dir / mask_file)
        screen_names: set[str] = _load_filter_names(config_dir / screen_file)

        write_names: set[str] = set()
        device_write_enabled: bool = keys.get("write_enabled", "false").strip().lower() == "true"
        if device_write_enabled:
            protocol_names: List[Row[Tuple[str]]] = (
                db.query(ProtocolRegister.protocol_name)
                .filter(ProtocolRegister.protocol_name.like(f"{protocol_version}%"))
                .distinct()
                .all()
            )
            for (protocol_name,) in protocol_names:
                write_names |= _load_override_names(protocols_dir, protocol_name, config_dir=config_dir)

        protocol_rows = (
            db.query(ProtocolRegister)
            .filter(ProtocolRegister.protocol_name.like(f"{protocol_version}%"))
            .all()
        )
        for protocol_row in protocol_rows:
            _upsert_device_protocol_selection(
                db,
                device_name,
                protocol_row,
                mask_names,
                screen_names,
                write_names,
            )


# ---------------------------------------------------------------------------
# Main scanner entry point
# ---------------------------------------------------------------------------

class Scanner:
    """
    Orchestrates a full scan of config.cfg + transports + protocols.
    Designed to be instantiated once and called on startup and from the
    file watcher.
    """

    def __init__(self, config_path: Path, project_root: Path) -> None:
        self.config_path: Path = config_path
        self.project_root: Path = project_root
        self.transports_dir: Path = project_root / "classes" / "transports"
        self.protocols_dir: Path = project_root / "protocols"

    @property
    def _cfg_is_truth(self) -> bool:
        """
        Returns True when the scanner should treat config.cfg as ground truth
        and sync value_staged = value_disk (used on startup when the file has
        been modified externally, or after a rollback).
        Set via scanner.set_cfg_is_truth(True) before calling run().
        """
        return getattr(self, "_cfg_truth_flag", False)

    def set_cfg_is_truth(self, value: bool = True) -> None:
        """Enable/disable cfg-as-truth mode for the next run() call."""
        self._cfg_truth_flag: bool = value

    def run(self, db: Session | None = None) -> dict[str, int]:
        """
        Full scan.  Accepts an optional existing session (for tests);
        otherwise opens its own session_scope.
        After run() completes, _cfg_is_truth resets to False.
        """
        try:
            if db is not None:
                return self._scan(db)
            with session_scope() as db:
                return self._scan(db)
        finally:
            self._cfg_truth_flag = False   # always reset after a scan

    def _scan(self, db: Session) -> dict[str, int]:
        _log.info(f"Starting scanner scan: {self.config_path}")

        # Update AppState scanner status
        state = db.get(AppState, 1)
        if state:
            state.scanner_status = "running"
            db.flush()

        stats: dict[str, int] = {
            "settings_upserted": 0,
            "settings_orphaned": 0,
            "registers_upserted": 0,
        }

        seen_setting_keys: set[tuple[str, str]] = set()

        try:
            # ----------------------------------------------------------------
            # 1. Scan config.cfg
            # ----------------------------------------------------------------
            config_data: dict[str, dict[str, str]] = _load_config(self.config_path)
            transport_library: dict[str, dict[str, Any]] = scan_transport_library(self.transports_dir)

            for section, keys in config_data.items():
                transport_type = "general"

                if section.startswith("transport."):
                    transport_type: str = _classify_transport(section, keys, self.transports_dir)
                elif section.lower() == "logging":
                    transport_type = "logging"

                for key, value in keys.items():
                    _upsert_setting(
                        db, section, key, value, transport_type,
                        default_value=self._get_default(section, key, keys, transport_library),
                        cfg_is_truth=self._cfg_is_truth,
                    )
                    seen_setting_keys.add((section, key))
                    stats["settings_upserted"] += 1

            # ----------------------------------------------------------------
            # 2. Add known-but-unset keys for each transport section
            #    (so the UI can show all possible config options)
            # ----------------------------------------------------------------
            for section, keys in config_data.items():
                if not section.startswith("transport."):
                    continue
                transport_name: str = keys.get("transport", "").strip()
                known_keys: dict[str, str] = KNOWN_TRANSPORT_KEYS.get(transport_name, {})
                transport_type = _classify_transport(section, keys, self.transports_dir)

                for k, default_v in known_keys.items():
                    if k not in keys:  # not already in config
                        _upsert_setting(
                            db, section, k,
                            value_disk="",
                            transport_type=transport_type,
                            default_value=default_v,
                            is_active=False,  # not currently in config
                        )
                        seen_setting_keys.add((section, k))
                        stats["settings_upserted"] += 1

            # ----------------------------------------------------------------
            # 3. Mark orphans
            # ----------------------------------------------------------------
            stats["settings_orphaned"] = _mark_orphaned_settings(db, seen_setting_keys)

            # ----------------------------------------------------------------
            # 4. Scan protocol CSV/JSON files
            # ----------------------------------------------------------------
            registers = scan_protocols_dir(self.protocols_dir)
            skipped = 0
            for reg in registers:
                result = _upsert_protocol_register(db, reg)
                if result is not None:
                    stats["registers_upserted"] += 1
                else:
                    skipped += 1

            if skipped:
                _log.warning(f"Scanner: {skipped} protocol register row(s) skipped due to errors.")

            _sync_device_protocol_selections(
                db,
                config_data,
                self.project_root,
                self.protocols_dir,
            )

            # Flush all pending INSERTs/UPDATEs in one shot.
            # Each INSERT already ran inside its own savepoint, so this flush
            # only covers the UPDATE path and is safe to call here.
            db.flush()

            # ----------------------------------------------------------------
            # 5. Refresh AppState
            # ----------------------------------------------------------------
            state = db.get(AppState, 1)
            if state is None:
                state = AppState(id=1)
                db.add(state)

            state.last_scan_at = datetime.now().astimezone()
            state.scanner_status = "idle"
            state.scanner_last_error = None
            db.commit()

            refresh_app_state(db)

            _log.info(
                f"Scanner complete: {stats['settings_upserted']} settings, "
                f"{stats['settings_orphaned']} orphaned, "
                f"{stats['registers_upserted']} registers."
            )
        except Exception as exc:
            _log.exception(f"Scanner error: {exc}")
            state = db.get(AppState, 1)
            if state:
                state.scanner_status = "error"
                state.scanner_last_error = str(exc)
                db.commit()
            raise

        return stats

    def _get_default(self, section: str, key: str, section_keys: dict[str, str], transport_library: dict[str, dict[str, Any]]) -> str:
        """Look up the default value for a key from the transport's known-keys table."""

        transport_name: str = section_keys.get("transport", "").strip()
        known: dict[str, str] = KNOWN_TRANSPORT_KEYS.get(transport_name, TRANSPORT_BASE_KEYS)
        return known.get(key, "")
