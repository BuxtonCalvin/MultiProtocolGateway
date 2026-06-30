# Description: debug_defaults.py — Drop-in debug instrumentation for the transport
#              defaults pipeline.  Import and call run_debug() once at startup
#              (after init_db but before scanner.run()) to get a full trace of
#              exactly what each stage produces for every transport in your config.
#
# Usage in main.py:
#     from classes.WebServer.debug_defaults import run_debug
#     run_debug(project_root=state.project_root, config_path=state.config_path)
#
# Remove the call (or set DEBUG_DEFAULTS = False) when the issue is resolved.

from __future__ import annotations

import ast
import configparser
import logging
from pathlib import Path
from sqlite3 import Connection
from typing import Any

_log: logging.Logger = logging.getLogger("debug_defaults")

# ── Set to False to silence all output once defaults are working ────────────
DEBUG_DEFAULTS: bool = True


def run_debug(project_root: Path, config_path: Path) -> None:
    if not DEBUG_DEFAULTS:
        return

    _log.warning("=" * 72)
    _log.warning("DEBUG_DEFAULTS: starting full defaults pipeline trace")
    _log.warning("=" * 72)

    transports_dir: Path = project_root / "classes" / "transports"

    _check_ast_scan(transports_dir)
    _check_inheritance_chain(transports_dir)
    _check_class_attr_fallbacks(transports_dir)
    _check_json_registry(config_path, transports_dir)
    _check_get_default_per_active_key(config_path, transports_dir)

    _log.warning("=" * 72)
    _log.warning("DEBUG_DEFAULTS: trace complete")
    _log.warning("=" * 72)


# ── Stage 1: Raw AST scan ────────────────────────────────────────────────────

def _check_ast_scan(transports_dir: Path) -> None:
    """
    Show exactly what _extract_settings_keys_from_ast returns for every
    transport file, including how many keys have None defaults vs real ones.
    Highlights the inheritance problem: modbus_base keys won't appear under
    modbus_tcp because the AST only scans one file at a time.
    """
    _log.warning("\n── STAGE 1: Raw per-file AST scan ──────────────────────────────────")

    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        keys: dict[str, str | None] = _ast_extract(py_file)
        none_keys: list[str] = [k for k, v in keys.items() if v is None]
        real_keys: dict[str, str] = {k: v for k, v in keys.items() if v is not None}
        _log.warning(
            f"  {py_file.name}: {len(keys)} total keys  "
            f"({len(real_keys)} with defaults, {len(none_keys)} with None)"
        )
        if real_keys:
            for k, v in sorted(real_keys.items()):
                _log.warning(f"      ✓ {k!r:40s} = {v!r}")
        if none_keys:
            for k in sorted(none_keys):
                _log.warning(f"      ✗ {k!r:40s} = None  ← no default in source")


# ── Stage 2: Inheritance chain ───────────────────────────────────────────────

def _check_inheritance_chain(transports_dir: Path) -> None:
    """
    For each transport file, follow the class inheritance chain through the
    transports directory and show what settings.get() calls live in each
    ancestor file.  This reveals which keys are MISSING from the leaf file's
    AST scan because they live in a base class.
    """
    _log.warning("\n── STAGE 2: Inheritance chain — base class key leakage ─────────────")

    # Build a map: class_name → (file, base_class_names, keys)
    file_info: dict[str, dict[str, Any]] = {}
    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        stem: str = py_file.stem
        bases: list[str] = _ast_get_bases(py_file)
        keys: dict[str, str | None] = _ast_extract(py_file)
        file_info[stem] = {"file": py_file, "bases": bases, "keys": keys}

    for stem, info in sorted(file_info.items()):
        bases_in_dir: list[Any] = [b for b in info["bases"] if b in file_info]
        if not bases_in_dir:
            continue  # no relevant base classes

        _log.warning(f"\n  {stem} inherits from: {info['bases']}")
        own_keys = set(info["keys"].keys())

        for base in bases_in_dir:
            base_keys = set(file_info[base]["keys"].keys())
            leaked = base_keys - own_keys
            if leaked:
                _log.warning(
                    f"    ⚠  {len(leaked)} keys in {base}.py NOT visible "
                    f"when scanning {stem}.py alone:"
                )
                for k in sorted(leaked):
                    v = file_info[base]["keys"][k]
                    _log.warning(f"        {k!r:40s} = {v!r}  (in {base}.py)")
            else:
                _log.warning(f"    ✓  {base}.py adds no extra keys beyond {stem}.py")


# ── Stage 3: Class-level attribute fallbacks ─────────────────────────────────

def _check_class_attr_fallbacks(transports_dir: Path) -> None:
    """
    Detect settings.get(key, fallback=self.attr) patterns where the fallback
    is a class attribute reference rather than a literal.  The AST scanner
    cannot resolve self.attr, so it returns None for those keys.
    The fix is to add the key+default to transport_defaults.json manually.
    """
    _log.warning("\n── STAGE 3: Non-literal fallbacks (class attr references) ──────────")

    found_any = False
    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        non_literals: dict[str, str] = _ast_find_non_literal_fallbacks(py_file)
        if non_literals:
            found_any = True
            _log.warning(f"\n  {py_file.name}:")
            for key, fallback_expr in sorted(non_literals.items()):
                _log.warning(
                    f"    {key!r:40s}  fallback={fallback_expr}  "
                    f"← AST returns None; add default to transport_defaults.json"
                )

    if not found_any:
        _log.warning("  (none found)")


# ── Stage 4: JSON registry vs resolved ───────────────────────────────────────

def _check_json_registry(config_path: Path, transports_dir: Path) -> None:
    """
    Show what transport_registry.get_known_transport_keys() actually resolves
    to for each transport used in config.cfg.  Highlights missing transports
    and missing keys.
    """
    _log.warning("\n── STAGE 4: JSON registry resolution per config transport ───────────")

    config_transports: dict[str, str] = _read_config_transports(config_path)

    try:
        from classes.WebServer.transport_registry import (
            get_known_transport_keys,
            get_transport_base_keys,
        )
        known: dict[str, dict[str, str]] = get_known_transport_keys()
        base: dict[str, str] = get_transport_base_keys()
        _log.warning(f"  Registry loaded: {len(known)} transports, {len(base)} base keys")
    except Exception as exc:
        _log.warning(f"  ✗ Could not load transport_registry: {exc}")
        return

    for section, transport_name in sorted(config_transports.items()):
        resolved: dict[str, str] | None = known.get(transport_name)
        if resolved is None:
            _log.warning(
                f"\n  ✗ {section} (transport={transport_name!r}): "
                f"NOT IN REGISTRY — all defaults will be empty"
            )
        else:
            none_vals: list[str] = [k for k, v in resolved.items() if v == ""]
            real_vals: dict[str, str] = {k: v for k, v in resolved.items() if v != ""}
            _log.warning(
                f"\n  ✓ {section} (transport={transport_name!r}): "
                f"{len(resolved)} keys  "
                f"({len(real_vals)} with defaults, {len(none_vals)} empty)"
            )
            for k, v in sorted(real_vals.items()):
                _log.warning(f"      {k!r:40s} = {v!r}")
            if none_vals:
                _log.warning(f"      (empty-default keys: {sorted(none_vals)})")


# ── Stage 5: _get_default simulation per active config key ───────────────────

def _check_get_default_per_active_key(config_path: Path, transports_dir: Path) -> None:
    """
    Simulate exactly what _get_default() returns for every key currently
    active in config.cfg.  This is what gets written into default_value
    for existing DB rows.  A result of None means the DB value won't be
    updated — which is correct if the row already has a good value, but
    will leave it blank if the row was written blank by an earlier broken run.
    """
    _log.warning("\n── STAGE 5: _get_default() simulation for active config keys ────────")

    config_data: dict[str, dict[str, str]] = _read_full_config(config_path)
    ast_library: dict[str, dict[str, Any]] = {}
    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        ast_library[py_file.stem] = {"keys": _ast_extract(py_file)}

    try:
        from classes.WebServer.transport_registry import (
            get_known_transport_keys,
            get_transport_base_keys,
        )
        known: dict[str, dict[str, str]] = get_known_transport_keys()
        base_keys: dict[str, str] = get_transport_base_keys()
    except Exception as exc:
        _log.warning(f"  ✗ Could not load transport_registry: {exc}")
        return

    for section, keys in sorted(config_data.items()):
        if not section.startswith("transport."):
            continue
        transport_name = keys.get("transport", "").strip()
        _log.warning(f"\n  [{section}]  transport={transport_name!r}")

        lib_entry: dict[str, Any] = ast_library.get(transport_name, {})
        ast_keys: dict[str, Any] = lib_entry.get("keys", {})
        json_defaults: dict[str, str] = known.get(transport_name, base_keys)

        for key in sorted(keys.keys()):
            # Simulate _get_default priority chain
            if key in ast_keys and ast_keys[key] is not None:
                result = ast_keys[key]
                source = "AST (literal)"
            else:
                _MISSING = object()
                result = json_defaults.get(key, _MISSING)
                if result is not _MISSING:
                    source = "JSON registry"
                else:
                    result = None
                    source = "None ← NOT FOUND ANYWHERE"

            marker = "✓" if result is not None else "✗"
            _log.warning(
                f"    {marker} {key!r:35s}  default={result!r:20}  [{source}]"
            )

    _log.warning(
        "\n  NOTE: Keys that show 'None ← NOT FOUND ANYWHERE' will leave "
        "default_value unchanged in DB.\n"
        "  If the DB row was already blank (from earlier broken run), "
        "those keys will continue showing '—' in the UI.\n"
        "  Fix: add the key+default to the transport entry in transport_defaults.json."
    )


# ── Stage 5b: Stale DB check ─────────────────────────────────────────────────

def check_stale_db_rows(db_path: Path, config_path: Path, transports_dir: Path) -> None:
    """
    Standalone function — call separately with the DB path to show which
    Setting rows have default_value='' that should have a real default.
    This identifies rows that were written blank by earlier broken scans
    and won't be fixed by a re-scan (because _upsert_setting skips None
    updates, but rows with '' won't get overwritten once '' is stored).

    Usage:
        from classes.WebServer.debug_defaults import check_stale_db_rows
        check_stale_db_rows(
            db_path=Path("config/data-db/mpg_staging.db"),
            config_path=state.config_path,
            transports_dir=state.transports_dir,
        )
    """
    import sqlite3

    _log.warning("\n── STAGE 5b: Stale DB rows with blank default_value ────────────────")

    try:
        conn: Connection = sqlite3.connect(str(db_path))
        rows: list[Any] = conn.execute(
            "SELECT section, key, default_value, is_active "
            "FROM settings WHERE default_value = '' OR default_value IS NULL "
            "ORDER BY section, key"
        ).fetchall()
        conn.close()
    except Exception as exc:
        _log.warning(f"  ✗ Could not read DB: {exc}")
        return

    try:
        from classes.WebServer.transport_registry import (
            get_known_transport_keys,
            get_transport_base_keys,
        )
        known: dict[str, dict[str, str]] = get_known_transport_keys()
        base_keys: dict[str, str] = get_transport_base_keys()
    except Exception as exc:
        _log.warning(f"  ✗ Could not load transport_registry: {exc}")
        return

    config_data: dict[str, dict[str, str]] = _read_full_config(config_path)
    ast_library: dict[str, dict[str, Any]] = {}
    for py_file in sorted(transports_dir.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        ast_library[py_file.stem] = {"keys": _ast_extract(py_file)}

    fixable: list[tuple[str, str, str]] = []   # (section, key, suggested_default)
    unfixable: list[tuple[str, str]] = []

    for section, key, default_val, is_active in rows:
        transport_name: str = config_data.get(section, {}).get("transport", "")
        lib_entry = ast_library.get(transport_name, {})
        ast_val = lib_entry.get("keys", {}).get(key)
        json_val: str | None = known.get(transport_name, base_keys).get(key)

        if ast_val is not None:
            fixable.append((section, key, ast_val))
        elif json_val is not None and json_val != "":
            fixable.append((section, key, json_val))
        else:
            unfixable.append((section, key))

    if fixable:
        _log.warning(f"\n  {len(fixable)} rows have blank default but a value IS available:")
        _log.warning(
            "  These should be fixed by a fresh scan — if they're still blank "
            "after restart, the _upsert_setting update path has a bug."
        )
        for section, key, suggested in fixable:
            _log.warning(f"    {section}.{key!r:35s}  suggested={suggested!r}")

    if unfixable:
        _log.warning(
            f"\n  {len(unfixable)} rows have blank default AND no value is available "
            f"anywhere:\n"
            f"  Add these to transport_defaults.json manually."
        )
        for section, key in unfixable:
            transport_name = config_data.get(section, {}).get("transport", "")
            _log.warning(f"    transport={transport_name!r}  key={key!r}")

    if not fixable and not unfixable:
        _log.warning("  ✓ No rows with blank default_value found — DB looks clean.")


# ── Internal AST helpers ─────────────────────────────────────────────────────

def _ast_extract(py_path: Path) -> dict[str, str | None]:
    """Same logic as scanner._extract_settings_keys_from_ast."""
    found: dict[str, str | None] = {}
    try:
        source: str = py_path.read_text(encoding="utf-8")
        tree: ast.Module = ast.parse(source)
    except Exception as exc:
        _log.warning(f"    AST parse failed for {py_path.name}: {exc}")
        return found

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr in ("get", "getint", "getfloat", "getboolean")
                and isinstance(func.value, ast.Name)
                and func.value.id == "settings"):
            continue
        if not node.args:
            continue
        first: ast.expr = node.args[0]
        keys_to_add: list[str] = []
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys_to_add.append(first.value)
        elif isinstance(first, ast.List):
            for elt in first.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys_to_add.append(elt.value)

        default_val: str | None = None
        for kw in node.keywords:
            if kw.arg == "fallback" and isinstance(kw.value, ast.Constant):
                default_val = str(kw.value.value)
                break
        if default_val is None and len(node.args) >= 2:
            arg2: ast.expr = node.args[1]
            if isinstance(arg2, ast.Constant):
                default_val = str(arg2.value)

        for k in keys_to_add:
            if k not in found:
                found[k] = default_val
    return found


def _ast_find_non_literal_fallbacks(py_path: Path) -> dict[str, str]:
    """
    Find settings.get() calls where the fallback is a non-literal expression
    (e.g. self.reconnect_attempts, self.host).  Returns {key: repr(fallback_ast)}.
    """
    found: dict[str, str] = {}
    try:
        source: str = py_path.read_text(encoding="utf-8")
        tree: ast.Module = ast.parse(source)
    except Exception:
        return found

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func: ast.expr = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr in ("get", "getint", "getfloat", "getboolean")
                and isinstance(func.value, ast.Name)
                and func.value.id == "settings"):
            continue
        if not node.args:
            continue
        first: ast.expr = node.args[0]
        keys_to_check: list[str] = []
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys_to_check.append(first.value)
        elif isinstance(first, ast.List):
            for elt in first.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys_to_check.append(elt.value)

        # Check for non-literal fallback
        fallback_node = None
        for kw in node.keywords:
            if kw.arg == "fallback":
                fallback_node = kw.value
                break
        if fallback_node is None and len(node.args) >= 2:
            fallback_node = node.args[1]

        if fallback_node is not None and not isinstance(fallback_node, ast.Constant):
            expr: str = ast.unparse(fallback_node) if hasattr(ast, "unparse") else repr(fallback_node)
            for k in keys_to_check:
                found[k] = expr

    return found


def _ast_get_bases(py_path: Path) -> list[str]:
    """Return the base class names declared in the first class in the file."""
    try:
        source: str = py_path.read_text(encoding="utf-8")
        tree: ast.Module = ast.parse(source)
    except Exception:
        return []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            return [
                b.id if isinstance(b, ast.Name) else
                b.attr if isinstance(b, ast.Attribute) else
                ast.unparse(b) if hasattr(ast, "unparse") else "?"
                for b in node.bases
            ]
    return []


def _read_config_transports(config_path: Path) -> dict[str, str]:
    """Return {section: transport_name} for all transport.* sections."""
    parser = configparser.ConfigParser()
    parser.read(str(config_path))
    result: dict[str, str] = {}
    for section in parser.sections():
        if section.startswith("transport."):
            result[section] = parser.get(section, "transport", fallback="")
    return result


def _read_full_config(config_path: Path) -> dict[str, dict[str, str]]:
    """Return full {section: {key: value}} for config.cfg."""
    parser = configparser.ConfigParser()
    parser.read(str(config_path))
    result: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        result[section] = dict(parser.items(section))
    return result
