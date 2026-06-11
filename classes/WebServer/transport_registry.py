# Description: transport_registry.py — Loads transport defaults and setting descriptions
#              from JSON files and keeps them in sync with the live AST-scanned transport library.
# File: transport_registry.py
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
transport_registry.py — Single source of truth for transport key defaults
and setting descriptions.

Replaces the inline KNOWN_TRANSPORT_KEYS / SEED_DESCRIPTIONS dicts in
scanner.py and setting_description_service.py with JSON-backed data that
can be edited without touching Python code.

Responsibilities
----------------
1.  Load  — read transport_defaults.json and setting_descriptions.json on
            first access (lazy, cached for the process lifetime).
2.  Resolve — expand "$extends" inheritance chains in transport_defaults.json
              so callers receive a plain flat dict per transport.
3.  Sync   — after an AST scan of the transports directory, write any newly
              discovered keys / transports back into both JSON files so they
              stay current automatically (call sync_from_library()).
4.  Expose — thin accessor functions used by scanner.py and
              setting_description_service.py.

JSON file locations
-------------------
Both files live alongside this module in the same package directory:
    <package_dir>/transport_defaults.json
    <package_dir>/setting_descriptions.json

Override the location at startup via:
    transport_registry.configure(registry_dir=Path("/custom/path"))
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default: files sit next to this module.  Call configure() to override.
_registry_dir: Path = Path(__file__).parent

# In-process caches — populated on first load, cleared by configure().
_defaults_cache: dict[str, Any] | None = None       # raw JSON (with $extends)
_resolved_cache: dict[str, dict[str, str]] | None = None  # flat per-transport
_descriptions_cache: dict[str, str] | None = None   # key → description string


def configure(registry_dir: Path) -> None:
    """
    Override the directory where the JSON files are looked up.
    Must be called before any accessor is used (e.g. during app startup,
    before Scanner is instantiated).
    Clears the in-process cache so the new files are loaded on next access.
    """
    global _registry_dir, _defaults_cache, _resolved_cache, _descriptions_cache
    _registry_dir = registry_dir
    _defaults_cache = None
    _resolved_cache = None
    _descriptions_cache = None
    _log.info("transport_registry configured: %s", registry_dir)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _defaults_path() -> Path:
    return _registry_dir / "transport_defaults.json"


def _descriptions_path() -> Path:
    return _registry_dir / "setting_descriptions.json"


def _load_json(path: Path) -> dict[str, Any]:
    """Read a JSON file and return its contents, stripping _comment keys."""
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except FileNotFoundError:
        _log.warning("transport_registry: file not found: %s — returning empty dict", path)
        return {}
    except json.JSONDecodeError as exc:
        _log.error("transport_registry: JSON parse error in %s: %s — returning empty dict", path, exc)
        return {}


def _save_json(path: Path, data: dict[str, Any], comment: str = "") -> None:
    """Write a JSON file, inserting the _comment key first if provided."""
    payload: dict[str, Any] = {}
    if comment:
        payload["_comment"] = comment
    payload.update(data)
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        _log.error("transport_registry: could not write %s: %s", path, exc)


def _resolve_transport(
    name: str,
    raw: dict[str, Any],
    visited: set[str] | None = None,
) -> dict[str, str]:
    """
    Recursively resolve $extends inheritance for a single transport entry.

    Rules
    -----
    - A transport dict may contain "$extends": "<parent_name>" pointing at
      another key in the raw dict (typically "_base" or "_modbus_base").
    - The resolved dict is: parent_keys | own_keys (own keys win).
    - "$extends" is stripped from the output.
    - Cycles are detected and broken with a warning.
    """
    if visited is None:
        visited = set()
    if name in visited:
        _log.warning("transport_registry: circular $extends detected at '%s' — breaking cycle", name)
        return {}
    visited.add(name)

    entry: dict[str, Any] = raw.get(name, {})
    parent_name: str | None = entry.get("$extends")

    own_keys: dict[str, str] = {
        k: str(v) for k, v in entry.items()
        if k != "$extends" and not k.startswith("_")
    }

    if parent_name:
        parent_keys = _resolve_transport(parent_name, raw, visited)
        return {**parent_keys, **own_keys}

    return own_keys


def _load_defaults() -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """
    Load and resolve transport_defaults.json.
    Returns (raw_dict, resolved_dict).
    raw_dict   — the unprocessed JSON (minus _comment), used for writes.
    resolved_dict — flat {transport_name: {key: default}} with inheritance applied.
                    Internal names starting with "_" are excluded from the resolved dict.
    """
    raw = _load_json(_defaults_path())
    resolved: dict[str, dict[str, str]] = {}
    for name in raw:
        if name.startswith("_"):
            continue  # private base/mixin entries — not a real transport
        resolved[name] = _resolve_transport(name, raw)
    return raw, resolved


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def get_known_transport_keys() -> dict[str, dict[str, str]]:
    """
    Return {transport_name: {key: default_value}} for all transports.
    Inheritance ($extends) is fully resolved; internal entries are excluded.
    Result is cached for the process lifetime.
    """
    global _defaults_cache, _resolved_cache
    if _resolved_cache is None:
        _defaults_cache, _resolved_cache = _load_defaults()
    return _resolved_cache


def get_transport_base_keys() -> dict[str, str]:
    """
    Return the resolved "_base" entry — the common keys shared by all
    transports.  Used as a fallback when a transport is not in the registry.
    """
    global _defaults_cache, _resolved_cache
    if _defaults_cache is None:
        _defaults_cache, _resolved_cache = _load_defaults()
    raw: dict[str, Any] = _defaults_cache or {}
    return _resolve_transport("_base", raw)


def get_setting_descriptions() -> dict[str, str]:
    """
    Return {key: description_string} for all known setting keys.
    Result is cached for the process lifetime.
    """
    global _descriptions_cache
    if _descriptions_cache is None:
        _descriptions_cache = {
            k: str(v) for k, v in _load_json(_descriptions_path()).items()
        }
    return _descriptions_cache


def get_default_for(transport_name: str, key: str) -> str:
    """
    Convenience: look up the default value for a single key in a transport.
    Falls back to the _base defaults if the transport is not registered.
    Returns "" if the key is not found anywhere.
    """
    known = get_known_transport_keys()
    transport_defaults = known.get(transport_name, get_transport_base_keys())
    return transport_defaults.get(key, "")


def get_description_for(key: str) -> str:
    """Return the description for a single setting key, or '' if unknown."""
    return get_setting_descriptions().get(key, "")


# ---------------------------------------------------------------------------
# Sync — keeps JSON files current after an AST scan
# ---------------------------------------------------------------------------

def sync_from_library(
    library: dict[str, dict[str, Any]],
    *,
    write_defaults: bool = True,
    write_descriptions: bool = True,
) -> dict[str, int]:
    """
    Merge newly discovered keys from an AST transport scan into the JSON files.

    Parameters
    ----------
    library : result of scanner.scan_transport_library()
        {transport_stem: {"classification": str, "keys": {key: default}, ...}}
    write_defaults : bool
        When True, add any transport / key not yet in transport_defaults.json.
        Existing entries are never overwritten so hand-edited defaults survive.
    write_descriptions : bool
        When True, add any key not yet in setting_descriptions.json.
        Existing descriptions are never overwritten.

    Returns
    -------
    dict with counts: new_transports, new_default_keys, new_description_keys
    """
    stats = {"new_transports": 0, "new_default_keys": 0, "new_description_keys": 0}

    # ---- Sync transport_defaults.json ----
    if write_defaults:
        global _defaults_cache, _resolved_cache
        # Always reload from disk so we include any hand-edits made since startup
        raw, _ = _load_defaults()

        changed = False
        for transport_name, info in sorted(library.items()):
            ast_keys: dict[str, str] = info.get("keys", {})
            if not ast_keys:
                continue

            if transport_name not in raw:
                # Entirely new transport — add it with all its AST keys
                raw[transport_name] = dict(ast_keys.items())
                stats["new_transports"] += 1
                stats["new_default_keys"] += len(ast_keys)
                changed = True
                _log.info("transport_registry: added new transport '%s' (%d keys)", transport_name, len(ast_keys))

            else:
                # Known transport — only add keys that are absent
                entry: dict[str, Any] = raw[transport_name]
                # Resolve to flat to check what's already covered via inheritance
                resolved_existing = _resolve_transport(transport_name, raw)
                for key, default_val in sorted(ast_keys.items()):
                    if key not in resolved_existing:
                        # Add directly to this transport's own entry (not the parent)
                        entry[key] = default_val
                        stats["new_default_keys"] += 1
                        changed = True
                        _log.debug("transport_registry: added key '%s' to transport '%s'", key, transport_name)

        if changed:
            _save_json(
                _defaults_path(),
                raw,
                comment=(
                    "Default values for every known setting key, grouped by transport class name. "
                    "Edit this file to change defaults shown in the UI. The scanner merges these "
                    "with live AST-scanned keys on every startup so the file stays in sync automatically."
                ),
            )
            # Invalidate cache so callers pick up the new data
            _defaults_cache = None
            _resolved_cache = None

    # ---- Sync setting_descriptions.json ----
    if write_descriptions:
        global _descriptions_cache
        descs: dict[str, str] = dict(_load_json(_descriptions_path()))

        changed = False
        for info in library.values():
            for key in info.get("keys", {}):
                if key not in descs:
                    descs[key] = ""   # empty — user fills in via the UI
                    stats["new_description_keys"] += 1
                    changed = True
                    _log.debug("transport_registry: added description stub for key '%s'", key)

        if changed:
            # Write alphabetically for easy human editing
            sorted_descs = dict(sorted(descs.items()))
            _save_json(
                _descriptions_path(),
                sorted_descs,
                comment=(
                    "Human-readable descriptions for every known transport setting key. "
                    "Edit this file to update the descriptions shown in the Transport Settings UI. "
                    "Keys discovered by the AST scanner that have no entry here will show an empty "
                    "description until one is added."
                ),
            )
            _descriptions_cache = None

    if stats["new_transports"] or stats["new_default_keys"] or stats["new_description_keys"]:
        _log.info(
            "transport_registry sync: %d new transports, %d new default keys, %d new description stubs",
            stats["new_transports"], stats["new_default_keys"], stats["new_description_keys"],
        )

    return stats
