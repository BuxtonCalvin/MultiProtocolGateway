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

IMPORTANT — private base/mixin entries
---------------------------------------
Entries whose names start with "_" (e.g. "_base", "_modbus_base") are
internal inheritance templates.  They are kept in the raw dict during
resolution but are NEVER exposed to callers as real transports.

The literal "_comment" key (written by _save_json for human readability)
is stripped on load so it does not interfere with resolution.
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

# In-process caches — populated on first load, cleared by configure() or sync.
_defaults_cache: dict[str, Any] | None = None       # raw JSON (with $extends, with _base etc.)
_resolved_cache: dict[str, dict[str, str]] | None = None  # flat per-transport (public transports only)
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
    """
    Read a JSON file and return its contents.

    Only the literal "_comment" key is stripped — private base/mixin entries
    like "_base" and "_modbus_base" are intentionally kept so that $extends
    resolution works correctly after a round-trip through _save_json.
    """
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k != "_comment"}
    except FileNotFoundError:
        _log.warning("transport_registry: file not found: %s — returning empty dict", path)
        return {}
    except json.JSONDecodeError as exc:
        _log.error("transport_registry: JSON parse error in %s: %s — returning empty dict", path, exc)
        return {}


def _save_json(path: Path, data: dict[str, Any], comment: str = "") -> None:
    """
    Write a JSON file, inserting the _comment key first if provided.

    Private base entries (keys starting with "_") are written back to the
    file so $extends chains survive the round-trip.
    """
    payload: dict[str, Any] = {}
    if comment:
        payload["_comment"] = comment
    payload.update(data)
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        _log.error("transport_registry: could not write %s: %s", path, exc)


def _resolve_transport(name: str, raw: dict[str, Any], visited: set[str] | None = None,) -> dict[str, str]:
    """
    Recursively resolve $extends inheritance for a single transport entry.

    Rules
    -----
    - A transport dict may contain "$extends": "<parent_name>" pointing at
      another key in the raw dict (e.g. "_base" or "_modbus_base").
    - The resolved dict is: parent_keys | own_keys (own keys win).
    - "$extends" is stripped from the output.
    - Cycles are detected and broken with a warning.
    - Keys named "$extends" or starting with "$" are never included in output.
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
        if not k.startswith("$")
    }

    if parent_name:
        parent_keys: dict[str, str] = _resolve_transport(parent_name, raw, visited)
        return {**parent_keys, **own_keys}

    return own_keys


def _load_defaults() -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """
    Load and resolve transport_defaults.json.

    Returns
    -------
    raw_dict     : unprocessed JSON minus _comment, used for writes.
                   Includes private "_base" / "_modbus_base" entries so that
                   $extends chains remain intact across save/load cycles.
    resolved_dict: flat {transport_name: {key: default}} with inheritance
                   applied.  Only public (non-"_") transport names are included.
    """
    raw: dict[str, Any] = _load_json(_defaults_path())
    resolved: dict[str, dict[str, str]] = {}
    for name in raw:
        if name.startswith("_"):
            continue  # private base/mixin — not a real transport
        resolved[name] = _resolve_transport(name, raw)
    return raw, resolved


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def get_known_transport_keys() -> dict[str, dict[str, str]]:
    """
    Return {transport_name: {key: default_value}} for all transports.
    Inheritance ($extends) is fully resolved; internal "_" entries are excluded.
    Result is cached for the process lifetime (invalidated by sync or configure).
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
    Result is cached for the process lifetime (invalidated by sync or configure).
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
    known: dict[str, dict[str, str]] = get_known_transport_keys()
    transport_defaults: dict[str, str] = known.get(transport_name, get_transport_base_keys())
    return transport_defaults.get(key, "")


def get_description_for(key: str) -> str:
    """Return the description for a single setting key, or '' if unknown."""
    return get_setting_descriptions().get(key, "")


def write_descriptions_to_json(descriptions: dict[str, str]) -> None:
    """
    Persist a complete {key: description} mapping to setting_descriptions.json.

    Called by commit_descriptions() so that user-edited descriptions written
    through the Transport Settings UI are flushed to the JSON file on every
    commit.  This keeps setting_descriptions.json in sync with the DB without
    requiring any manual developer action, and ensures the file is always a
    usable distribution master for the application.

    Merges with the existing file so that keys not present in the caller's
    dict (e.g. keys the caller didn't touch) are preserved unchanged.
    The file is written in alphabetical key order for clean diffs.
    """
    global _descriptions_cache
    existing: dict[str, str] = dict(_load_json(_descriptions_path()))
    # Overlay: caller's values win; unmentioned keys are preserved
    merged: dict[str, str] = {**existing, **descriptions}
    sorted_merged: dict[str, str] = dict(sorted(merged.items()))
    _save_json(
        _descriptions_path(),
        sorted_merged,
        comment=(
            "Human-readable descriptions for every known transport setting key. "
            "Edit this file to update the descriptions shown in the Transport Settings UI. "
            "Keys discovered by the AST scanner that have no entry here will show an empty "
            "description until one is added."
        ),
    )
    # Invalidate cache so next read picks up the freshly written file
    _descriptions_cache = None
    _log.info(
        "transport_registry: wrote %d descriptions to %s",
        len(sorted_merged), _descriptions_path().name,
    )


# ---------------------------------------------------------------------------
# Sync — keeps JSON files current after an AST scan
# ---------------------------------------------------------------------------

def sync_from_library(
    library: dict[str, dict[str, Any]],
    *,
    write_defaults: bool = True,
    write_descriptions: bool = True,
    purge_removed: bool = False,
) -> dict[str, int]:
    """
    Merge newly discovered keys from an AST transport scan into the JSON files
    and optionally purge keys that are no longer present in any transport.

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
    purge_removed : bool
        When True, remove from both JSON files any key that is no longer
        present in any transport in the library.  Safe to enable at startup
        once the codebase is stable — makes deletion of settings.get() calls
        fully automatic.

        Purge rules:
        - A key is only removed from transport_defaults.json if it is absent
          from ALL transports' resolved key sets (including inherited keys).
          Keys that belong to _base / _modbus_base are never auto-purged from
          those templates — only from leaf transport entries.
        - A transport entry is removed from transport_defaults.json if the
          transport file no longer exists in the library at all.
        - setting_descriptions.json entries are purged when the key is no
          longer in any transport.

    Returns
    -------
    dict with counts: new_transports, new_default_keys, new_description_keys,
                      purged_transport_keys, purged_transports, purged_description_keys
    """
    stats: dict[str, int] = {
        "new_transports": 0,
        "new_default_keys": 0,
        "new_description_keys": 0,
        "purged_transport_keys": 0,
        "purged_transports": 0,
        "purged_description_keys": 0,
    }

    # Build the complete set of all keys seen across every transport in the
    # library scan.  Used for purge decisions.
    all_live_keys: set[str] = set()
    for info in library.values():
        all_live_keys.update(info.get("keys", {}).keys())

    # ---- Sync transport_defaults.json ----
    if write_defaults or purge_removed:
        global _defaults_cache, _resolved_cache
        # Always reload from disk so we include any hand-edits made since startup.
        # _load_json keeps "_base" / "_modbus_base" entries so $extends chains
        # survive the round-trip intact.
        raw, _ = _load_defaults()

        changed = False

        # -- Add new transports / keys --
        if write_defaults:
            for transport_name, info in sorted(library.items()):
                # ast_keys values may be None when settings.get("key") had no
                # second argument in source.  We never write None into JSON —
                # absent entries stay absent so the user can fill them in via the UI.
                ast_keys_raw: dict[str, str | None] = info.get("keys", {})
                ast_all_keys: set[str] = set(ast_keys_raw.keys())

                if not ast_all_keys:
                    continue

                if transport_name not in raw:
                    # Entirely new transport — add all keys we have real defaults for.
                    # Keys with no AST default are added as "" so they appear in the UI.
                    raw[transport_name] = {
                        k: (ast_keys_raw[k] if ast_keys_raw[k] is not None else "")
                        for k in ast_all_keys
                    }
                    stats["new_transports"] += 1
                    stats["new_default_keys"] += len(ast_all_keys)
                    changed = True
                    _log.info("transport_registry: added new transport '%s' (%d keys)", transport_name, len(ast_all_keys))
                else:
                    # Known transport — only add keys not already covered
                    # (either directly or via $extends inheritance).
                    entry: dict[str, Any] = raw[transport_name]
                    resolved_existing: dict[str, str] = _resolve_transport(transport_name, raw)
                    for key in sorted(ast_all_keys):
                        if key not in resolved_existing:
                            # Use the AST default if available, otherwise ""
                            entry[key] = ast_keys_raw[key] if ast_keys_raw[key] is not None else ""
                            stats["new_default_keys"] += 1
                            changed = True
                            _log.debug("transport_registry: added key '%s' to transport '%s'",key, transport_name)

        # -- Purge removed transports and keys --
        if purge_removed:
            live_transport_names: set[str] = set(library.keys())

            # Remove entire transport entries that no longer exist as files.
            # Never remove private base/mixin entries (names starting with "_").
            to_remove_transports: list[str] = [
                name for name in list(raw.keys())
                if not name.startswith("_") and name not in live_transport_names
            ]
            for name in to_remove_transports:
                del raw[name]
                stats["purged_transports"] += 1
                changed = True
                _log.info("transport_registry: purged removed transport '%s'", name)

            # Remove individual keys from leaf transport entries that are no
            # longer in any transport's live key set.
            # Never touch private base/mixin entries directly — they are managed
            # manually since the AST cannot trace inheritance.
            for transport_name, entry in raw.items():
                if transport_name.startswith("_"):
                    continue  # leave _base / _modbus_base alone
                keys_to_purge: list[str] = [
                    k for k in list(entry.keys())
                    if not k.startswith("$") and k not in all_live_keys
                ]
                for k in keys_to_purge:
                    del entry[k]
                    stats["purged_transport_keys"] += 1
                    changed = True
                    _log.info(
                        "transport_registry: purged removed key '%s' from transport '%s'",
                        k, transport_name,
                    )

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
            # Invalidate cache so callers pick up the updated data on next access.
            _defaults_cache = None
            _resolved_cache = None

    # ---- Sync setting_descriptions.json ----
    if write_descriptions or purge_removed:
        global _descriptions_cache
        descs: dict[str, str] = dict(_load_json(_descriptions_path()))

        changed = False

        if write_descriptions:
            for info in library.values():
                for key in info.get("keys", {}):
                    if key not in descs:
                        descs[key] = ""   # empty stub — user fills in via the UI
                        stats["new_description_keys"] += 1
                        changed = True
                        _log.debug("transport_registry: added description stub for key '%s'", key)

        if purge_removed:
            stale_desc_keys: list[str] = [
                k for k in list(descs.keys()) if k not in all_live_keys
            ]
            for k in stale_desc_keys:
                del descs[k]
                stats["purged_description_keys"] += 1
                changed = True
                _log.info("transport_registry: purged description for removed key '%s'", k)

        if changed:
            sorted_descs: dict[str, str] = dict(sorted(descs.items()))
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

    if any(stats.values()):
        _log.info(
            "transport_registry sync: "
            "%d new transports, %d new default keys, %d new description stubs | "
            "purged: %d transports, %d transport keys, %d description keys",
            stats["new_transports"], stats["new_default_keys"], stats["new_description_keys"],
            stats["purged_transports"], stats["purged_transport_keys"], stats["purged_description_keys"],
        )

    return stats
