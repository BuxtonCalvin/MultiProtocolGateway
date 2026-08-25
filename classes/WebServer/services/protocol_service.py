# Description: services/protocol_service.py — Protocol register queries and toggle mutations.
# File: protocol_service.py
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
services/protocol_service.py — Protocol register queries and toggle mutations.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.engine.row import Row
from sqlalchemy.orm import Query, Session

from ...protocol_settings import Registry_Type, registry_map_entry
from ...transports.transport_base import transport_base
from ..database import refresh_app_state
from ..models import DeviceProtocolSelection, ProtocolRegister

_log: logging.Logger = logging.getLogger(__name__)

# One entry of transport.synthetic_fields_metadata — either
# (variable_name, data_type, unit_mod, note) or the newer
# (variable_name, data_type, unit_mod, note, registry_type) form, where the
# 5th element is a lowercase registry-type string. See
# build_synthetic_rows()'s docstring / transport_base.synthetic_fields_metadata.
SyntheticFieldMetadata = (
    tuple[str, str | None, float, str | None]
    | tuple[str, str | None, float, str | None, str]
)


@dataclass
class DeviceRegisterView:
    id: int
    protocol_name: str
    registry_type: str
    register_address: str
    variable_name: str
    documented_name: str
    unit: str | None
    data_type: str | None
    values_range: str | None
    adjustments: str | None
    note: str | None
    read_interval: str | None
    write_mode_protocol: str
    user_write_enabled: bool
    mask_enabled: bool
    screen_enabled: bool
    is_dirty: bool
    # Paired-register fields — populated when this row is the merged stem of
    # a _l/_h pair.  paired_high_address holds the _h register address so the
    # UI can render the range "40-41" and show the expand/collapse detail rows.
    paired_high_address: str | None = None
    # True for fields injected by post_process_data rather than read from
    # a protocol CSV register.  Synthetic rows are display-only — they have
    # no ProtocolRegister DB row, no toggle endpoints, and are never written
    # to mask / screen files.
    is_synthetic: bool = False
    # True for auto-generated "<name>_desc" rows (see
    # protocol_settings._add_code_description_entries) — a companion text
    # description for a register that carries a code/enum dict. Like
    # synthetic rows, these exist only on the live transport's registry_map
    # (keyed off registry_map_entry.description_source), never as a
    # ProtocolRegister DB row — see build_json_desc_rows().
    is_json_desc: bool = False
    # Only meaningful when is_json_desc is True — see ProtocolRegister
    # .source_variable_name. Threaded from build_json_desc_rows through to
    # the template's create-and-toggle payload so the API layer can find
    # (or materialize) the paired code row for the mask/screen auto-link.
    source_variable_name: str | None = None
    # Staged for deletion via the protocol editor's DELETE column — see
    # ProtocolRegister.pending_delete. Read directly off the ProtocolRegister
    # row (not device-scoped like is_dirty above), since a pending deletion
    # is shared protocol-definition state, the same scope as the row's other
    # editable fields.
    pending_delete: bool = False

    @property
    def is_paired(self) -> bool:
        """True when this row represents a merged _l/_h register pair."""
        return bool(self.paired_high_address)

    @property
    def is_writable_by_protocol(self) -> bool:
        return self.write_mode_protocol in ("RW", "W", "WO", "WRITE", "R/W")


def _safe_paired_address(row: ProtocolRegister) -> str | None:
    """
    Safely read paired_high_address from a ProtocolRegister ORM row.

    SQLAlchemy raises InvalidRequestError (not AttributeError) when accessing
    a mapped attribute that doesn't exist as a column in the current DB schema.
    Python's getattr(obj, name, default) only catches AttributeError, so it
    would re-raise here.  We first try the instance __dict__ directly to bypass
    any descriptor magic, then fall back to attribute access, swallowing all
    exceptions until the migration adds the column.
    """
    # Fast path: check instance dict directly, bypassing SQLAlchemy descriptors
    instance_state = getattr(row, "__dict__", {})
    if "paired_high_address" in instance_state:
        return instance_state["paired_high_address"]
    # Slow path: attempt instrumented access, catch anything SQLAlchemy raises
    try:
        val = row.paired_high_address
    except Exception:
        return None
    else:
        return val


def _virtual_register_address(kind: str, variable_name: str) -> str:
    """
    Synthetic and JSON-desc metrics have no real CSV register address, but
    ProtocolRegister.register_address is part of the uniqueness constraint
    (protocol_name, registry_type, register_address) and DeviceProtocolSelection
    keys off the same triple + device_name, so they still need *some* unique,
    stable string. Real CSV addresses are numeric ("21") or dotted-bit
    ("27.b14") — never "~"-prefixed — so this can't collide with one no
    matter what a future protocol's CSV looks like.
    """
    return f"~{kind}:{variable_name}"


def _safe_flag(row: ProtocolRegister, name: str) -> bool:
    """
    Same defensive-read reasoning as _safe_paired_address, generalized for
    is_synthetic / is_json_desc — both added in the same migration as
    paired_high_address was originally, so an un-migrated DB would raise
    InvalidRequestError (not AttributeError) on plain attribute access.
    """
    instance_state = getattr(row, "__dict__", {})
    if name in instance_state:
        return bool(instance_state[name])
    try:
        return bool(getattr(row, name))
    except Exception:
        return False


def _safe_str(row: ProtocolRegister, name: str) -> str | None:
    """String-valued counterpart to _safe_flag — same un-migrated-DB reasoning."""
    instance_state = getattr(row, "__dict__", {})
    if name in instance_state:
        return instance_state[name]
    try:
        return getattr(row, name)
    except Exception:
        return None


def get_protocol_registers(
    db: Session,
    protocol_name: str,
    registry_type: str,
    page: int = 1,
    page_size: int = 50,
    device_name: str | None = None,
) -> dict[str, str | int | list[DeviceRegisterView]]:
    """
    Returns a paginated list of ProtocolRegister rows for a given
    protocol_name and registry_type (input | holding | coil | discrete | json).
    """
    query: Query[ProtocolRegister] = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
        )
        .order_by(ProtocolRegister.register_address)
    )

    total: int = query.count()
    _log.debug("get_protocol_registers: %s/%s page=%d total=%d", protocol_name, registry_type, page, total)
    protocol_rows: List[ProtocolRegister] = query.offset((page - 1) * page_size).limit(page_size).all()

    rows: list[DeviceRegisterView]

    if device_name:
        selections: dict[tuple[str, str, str], DeviceProtocolSelection] = {
            (row.protocol_name, row.registry_type, row.register_address): row
            for row in (
                db.query(DeviceProtocolSelection)
                .filter(
                    DeviceProtocolSelection.device_name == device_name,
                    DeviceProtocolSelection.protocol_name == protocol_name,
                    DeviceProtocolSelection.registry_type == registry_type,
                )
                .all()
            )
        }
    else:
        selections = {}

    view_rows: list[DeviceRegisterView] = []
    for row in protocol_rows:
        try:
            s: DeviceProtocolSelection | None = selections.get(
                (row.protocol_name, row.registry_type, row.register_address)
            )
            view_rows.append(
                DeviceRegisterView(
                    id=row.id,
                    protocol_name=row.protocol_name,
                    registry_type=row.registry_type,
                    register_address=row.register_address,
                    variable_name=row.variable_name,
                    documented_name=row.documented_name,
                    unit=row.unit,
                    data_type=row.data_type,
                    values_range=row.values_range,
                    adjustments=row.adjustments,
                    note=row.note,
                    read_interval=row.read_interval,
                    write_mode_protocol=row.write_mode_protocol,
                    user_write_enabled=s.user_write_enabled if s else False,
                    mask_enabled=s.mask_enabled if s else False,
                    screen_enabled=s.screen_enabled if s else False,
                    is_dirty=s.is_dirty if s else False,
                    paired_high_address=_safe_paired_address(row),
                    is_synthetic=_safe_flag(row, "is_synthetic"),
                    is_json_desc=_safe_flag(row, "is_json_desc"),
                    source_variable_name=_safe_str(row, "source_variable_name"),
                    pending_delete=_safe_flag(row, "pending_delete"),
                )
            )
        except Exception as exc:
            _log.warning(
                "Skipping register row id=%s variable=%s in get_protocol_registers: %s",
                getattr(row, "id", "?"),
                getattr(row, "variable_name", "?"),
                exc,
            )
    rows = view_rows

    return {
        "protocol_name": protocol_name,
        "registry_type": registry_type,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": rows,
    }


def get_protocols_for_device(db: Session, protocol_version: str, device_name: str | None = None) -> List[dict[str, str | int]]:
    """
    Given a protocol_version string (e.g. "eg4_18kpv"),
    returns the available registry_types (tabs) for that device,
    including W/M/S selection counts when device_name is provided.
    """
    rows: Sequence[Row[Tuple[str, str]]] = (
        db.execute(
            select(
                ProtocolRegister.protocol_name,
                ProtocolRegister.registry_type,
            )
            .where(ProtocolRegister.protocol_name.like(f"{protocol_version}%"))
            .distinct()
            .order_by(ProtocolRegister.registry_type)
        )
        .all()
    )

    tabs: List[dict[str, str | int]] = []
    protocol_name: str
    registry_type: str
    for r in rows:
        protocol_name, registry_type = r[0], r[1]

        write_count = mask_count = screen_count = 0

        if device_name:
            sels: List[DeviceProtocolSelection] = (
                db.query(DeviceProtocolSelection)
                .filter(
                    DeviceProtocolSelection.device_name == device_name,
                    DeviceProtocolSelection.protocol_name == protocol_name,
                    DeviceProtocolSelection.registry_type == registry_type,
                )
                .all()
            )
            write_count: int  = sum(1 for s in sels if s.user_write_enabled)
            mask_count: int   = sum(1 for s in sels if s.mask_enabled)
            screen_count: int = sum(1 for s in sels if s.screen_enabled)

        tabs.append({
            "protocol_name": protocol_name,
            "registry_type": registry_type,
            "write_count":   write_count,
            "mask_count":    mask_count,
            "screen_count":  screen_count,
        })
    return tabs


# The TimescaleDB wide-table writer builds one column per selected metric
# across a device's register maps. Past this many columns it refuses to
# form the table, so the UI warns the user before they hit that wall.
WIDE_TABLE_COLUMN_LIMIT: int = 160

# Every registry-type tab feeds the wide table except the synthetic "json"
# config tab — matches timescaledb._extract_metric_names, which (as of the
# CUSTOM_BUS fix) iterates every registry type actually present in a
# transport's registry_map rather than a fixed modbus-shaped allowlist.
# register_map_chosen is intentionally *not* narrowed to a type subset here
# for the same reason: a hardcoded allowlist is exactly what silently
# undercounted coil/discrete before, and would do the same to custom_bus
# (serial_pylon, canbus, ...) or any future registry type. existing_registry_types
# is already "json" excluded (see below), so nothing further to filter.


def get_device_metric_summary(
    db: Session,
    protocol_version: str,
    device_name: str,
    transport: transport_base | None = None,
) -> dict[str, int | bool | dict[str, dict[str, int]]]:
    """
    Device-wide metric-selection summary across every non-JSON registry-type
    tab (register map) for a protocol_version. Used to render the
    Available / Selected / <Registry Map> Available/Selected badges next to
    the protocol tab strip.

    Returns a dict with:

      - total_available: every metric available to the device — the sum of
        each register-map tab's row count, plus every distinct synthetic
        field.

      - chosen_count: metrics that will actually be forwarded to a bridge,
        summed across every non-JSON tab. Per tab, mask acts as a whitelist
        (only mask_enabled rows are kept) and screen acts as a blacklist
        (every row *except* the screen_enabled ones is kept). Mask and
        screen are mutually exclusive per register (enforced in
        toggle_register_field), so a given tab is never counted both ways.
        A tab with no selections of its own contributes its full total only
        if NO tab anywhere in the protocol has any selections (matching the
        "No chosen metrics: all metrics will write to bridges" notice shown
        elsewhere) — as soon as any other tab has a mask or screen
        selection, an empty tab contributes nothing rather than being
        assumed fully forwarded. Tabs that do have their own selections
        always apply their own whitelist/blacklist rule independently of
        what any other tab is doing. Synthetic fields have no toggle and
        are always forwarded, so they're added in once at the end
        regardless of any tab's selection state, the same as in
        total_available.

      - register_map_chosen: chosen_count restricted to registry_type in
        {"holding", "input"} — the register maps that feed the TimescaleDB
        wide table — even when a protocol also exposes other registry types
        that don't participate in that table.

      - over_limit: True when register_map_chosen exceeds
        WIDE_TABLE_COLUMN_LIMIT.

      - by_registry_type: {registry_type: {"total": int, "chosen": int}}
        for every non-JSON tab, so the UI can show an "<Active Tab>
        Available / Selected" pair that follows whichever tab the user has
        open. Unlike total_available/chosen_count (which dedupe a
        registry-agnostic synthetic field so it's only counted once
        overall), each tab's own total/chosen here includes every synthetic
        field that actually renders on that tab — a tab-specific field only
        on its tab, an agnostic field on every tab — since that's what the
        person actually sees when that tab is open, even though it means
        an agnostic field is reflected in more than one tab's numbers.

    `transport` should be the live gateway transport for this device (see
    build_synthetic_rows) so synthetic metrics are included in every count.
    Pass None to compute registers-only counts, e.g. for a device that
    isn't currently connected.
    """
    rows: Sequence[Row[Tuple[str, str, int]]] = (
        db.execute(
            select(
                ProtocolRegister.protocol_name,
                ProtocolRegister.registry_type,
                func.count(ProtocolRegister.id),
            )
            .where(ProtocolRegister.protocol_name.like(f"{protocol_version}%"))
            .group_by(ProtocolRegister.protocol_name, ProtocolRegister.registry_type)
        )
        .all()
    )

    total_available: int = 0
    chosen_count: int = 0
    register_map_chosen: int = 0
    existing_registry_types: set[str] = {
        registry_type for _, registry_type, _ in rows if registry_type != "json"
    }

    # Gather each register-map tab's own mask/screen counts first. Whether
    # an *empty* tab (no mask or screen selections of its own) counts as
    # "everything forwarded" or "nothing forwarded" depends on whether any
    # OTHER tab in the protocol has selections — so that has to be known
    # before any tab's contribution can be decided.
    tab_data: List[Tuple[str, int, int, int]] = []  # (registry_type, tab_total, mask_count, screen_count)
    for protocol_name, registry_type, tab_total in rows:
        if registry_type == "json":
            continue

        sels: List[DeviceProtocolSelection] = (
            db.query(DeviceProtocolSelection)
            .filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == protocol_name,
                DeviceProtocolSelection.registry_type == registry_type,
            )
            .all()
        )
        mask_count: int = sum(1 for s in sels if s.mask_enabled)
        screen_count: int = sum(1 for s in sels if s.screen_enabled)
        tab_data.append((registry_type, tab_total, mask_count, screen_count))
        total_available += tab_total

    # True as soon as ANY register-map tab has a mask or screen selection.
    # When that's the case, a tab with no selections of its own contributes
    # nothing — its metrics are left out entirely rather than assumed
    # forwarded. Only when nothing is selected anywhere does the "no
    # selections = everything forwarded" fallback apply, matching the
    # "No chosen metrics: all metrics will write to bridges" notice.
    any_selections: bool = any(mask_count or screen_count for _, _, mask_count, screen_count in tab_data)

    # Synthetic fields have no per-register toggle — they're always
    # forwarded — and build_synthetic_rows() attaches each one to every tab
    # it applies to (a tab-specific field to just that tab, an
    # registry-agnostic field to every tab). Track which tabs each distinct
    # field actually appears on, so total_available/chosen_count can count
    # it once overall while by_registry_type can still reflect it on every
    # tab it's visible on. Skip a field tagged to a registry_type this
    # protocol doesn't actually expose as a tab (matches the filtering
    # build_synthetic_rows() itself applies when rendering a specific tab).
    synthetic_names_by_type: dict[str, set[str]] = {rt: set() for rt in existing_registry_types}
    all_synthetic_names: set[str] = set()
    if transport is not None:
        metadata: list[SyntheticFieldMetadata] = getattr(transport, "synthetic_fields_metadata", [])
        for field in metadata:
            rest: tuple[str, ...] = field[4:]
            field_registry_type: str | None = str(rest[0]).lower() if rest and rest[0] else None
            if field_registry_type is not None and field_registry_type not in existing_registry_types:
                continue
            name: str = field[0]
            all_synthetic_names.add(name)
            if field_registry_type is None:
                for rt in existing_registry_types:
                    synthetic_names_by_type[rt].add(name)
            else:
                synthetic_names_by_type[field_registry_type].add(name)
    synthetic_total: int = len(all_synthetic_names)
    register_map_synthetic_names: set[str] = set()
    for rt in existing_registry_types:
        register_map_synthetic_names |= synthetic_names_by_type[rt]
    register_map_synthetic_total: int = len(register_map_synthetic_names)

    # JSON code-description entries ("<name>_desc") behave exactly like
    # synthetic fields for counting purposes: no per-register toggle (no
    # ProtocolRegister/DeviceProtocolSelection row exists for them), always
    # forwarded, so they're added in once here rather than through the
    # mask/screen tab_chosen logic below. build_json_desc_rows() reads
    # transport.registry_map directly and already tags each row with the
    # registry-type bucket it came from via row.registry_type.
    json_desc_names_by_type: dict[str, set[str]] = {rt: set() for rt in existing_registry_types}
    all_json_desc_names: set[str] = set()
    if transport is not None:
        for row in build_json_desc_rows(transport):
            if row.registry_type not in existing_registry_types:
                continue
            all_json_desc_names.add(row.variable_name)
            json_desc_names_by_type[row.registry_type].add(row.variable_name)
    json_desc_total: int = len(all_json_desc_names)
    # Every tab reaching by_registry_type below is already register-map
    # eligible (see register_map_chosen note above), so no further
    # restriction is needed here the way register_map_synthetic_names
    # needed one before that fix.
    register_map_json_desc_total: int = json_desc_total

    by_registry_type: dict[str, dict[str, int]] = {}
    for registry_type, tab_total, mask_count, screen_count in tab_data:
        if mask_count:
            tab_chosen: int = mask_count
        elif screen_count:
            tab_chosen = tab_total - screen_count
        elif not any_selections:
            tab_chosen = tab_total
        else:
            tab_chosen = 0

        chosen_count += tab_chosen
        # Every tab reaching this loop is already non-json (see tab_data's
        # own "json" skip above), so every tab is wide-table-eligible now.
        register_map_chosen += tab_chosen

        tab_synthetic: int = len(synthetic_names_by_type.get(registry_type, set()))
        tab_json_desc: int = len(json_desc_names_by_type.get(registry_type, set()))
        by_registry_type[registry_type] = {
            "total":  tab_total + tab_synthetic + tab_json_desc,
            "chosen": tab_chosen + tab_synthetic + tab_json_desc,
        }

    total_available += synthetic_total + json_desc_total
    chosen_count += synthetic_total + json_desc_total
    register_map_chosen += register_map_synthetic_total + register_map_json_desc_total

    return {
        "total_available":     total_available,
        "chosen_count":        chosen_count,
        "register_map_chosen": register_map_chosen,
        "over_limit":          register_map_chosen > WIDE_TABLE_COLUMN_LIMIT,
        "wide_table_limit":    WIDE_TABLE_COLUMN_LIMIT,
        "by_registry_type":    by_registry_type,
    }


def _find_protocol_register(
    db: Session, protocol_name: str, registry_type: str, variable_name: str
) -> ProtocolRegister | None:
    """Look up a ProtocolRegister by variable_name rather than register_address —
    used by the code<->desc auto-link cascade, which only ever has the
    counterpart's variable_name in hand, not its address."""
    return (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
            ProtocolRegister.variable_name == variable_name,
        )
        .first()
    )


def materialize_and_toggle_virtual_metric(
    db: Session,
    protocol_name: str,
    registry_type: str,
    device_name: str,
    kind: str,             # "synthetic" | "json_desc"
    variable_name: str,
    documented_name: str,
    unit: str | None,
    data_type: str | None,
    note: str | None,
    read_interval: str | None,
    field: str,             # "user_write_enabled" | "mask_enabled" | "screen_enabled"
    value: bool,
    source_variable_name: str | None = None,
) -> DeviceProtocolSelection | None:
    """
    First-selection path for a synthetic or JSON code-description metric —
    the equivalent of devices.create_and_activate() for a virtual Setting
    row, but for a virtual register row (DeviceRegisterView.id == -1).

    Neither kind of metric is a real CSV register row, so no ProtocolRegister
    exists for it yet the first time a device selects it. This creates that
    row on demand (is_synthetic / is_json_desc flagged, so config_writer
    must exclude it when regenerating the protocol's CSV — see the model
    docstring), keyed under a "~"-prefixed virtual address
    (_virtual_register_address) that can't collide with a real CSV address,
    then hands off to the exact same toggle_register_field() used by every
    other register from that point on — including its two-gate write rule
    (write_mode_protocol is always "R" here, so user_write_enabled can never
    be granted, same as any other read-only register).

    kind == "json_desc" additionally cascades mask_enabled/screen_enabled to
    the underlying CODE register (source_variable_name, always a real CSV
    row for a CSV-backed protocol, so no materialization needed there) —
    per the mask/screen semantics: selecting a description always selects
    its code too, and vice versa (see toggle_register_field's own cascade
    for the code -> desc direction when the desc already exists). Neither
    the description's own name nor a synthetic field's name is ever written
    to a mask/screen file (see config_writer._write_mask_screen_files) — the
    code register is what actually gets written and read back at scrape
    time, and the code<->desc link is what carries a description's
    selection into the mask/screen file via its paired code.

    Once this row exists, subsequent toggles for this device/metric go
    through the normal PATCH /api/protocols/{register_id}/toggle endpoint
    with the real id this returns — this function is only for the first one.

    Returns the updated DeviceProtocolSelection, or None if field/kind is
    invalid.
    """
    if kind not in ("synthetic", "json_desc"):
        return None
    allowed_fields: set[str] = {"user_write_enabled", "mask_enabled", "screen_enabled"}
    if field not in allowed_fields or not device_name:
        return None

    register_address: str = _virtual_register_address(kind, variable_name)

    row: ProtocolRegister | None = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
            ProtocolRegister.register_address == register_address,
        )
        .first()
    )
    if row is None:
        # protocol_group isn't derivable from anything passed to this
        # function — pull it from any existing row of this protocol rather
        # than defaulting to protocol_name, since get_protocol_json() and
        # the CSV-export routes key off the real value.
        existing_group_row: ProtocolRegister | None = (
            db.query(ProtocolRegister)
            .filter(ProtocolRegister.protocol_name == protocol_name)
            .first()
        )
        protocol_group: str = existing_group_row.protocol_group if existing_group_row else protocol_name

        row = ProtocolRegister(
            protocol_group=protocol_group,
            protocol_name=protocol_name,
            registry_type=registry_type,
            register_address=register_address,
            variable_name=variable_name,
            documented_name=documented_name or variable_name,
            unit=unit,
            data_type=data_type,
            values_range=None,
            adjustments=None,
            note=note,
            read_interval=read_interval,
            write_mode_protocol="R",
            is_synthetic=(kind == "synthetic"),
            is_json_desc=(kind == "json_desc"),
            source_variable_name=source_variable_name if kind == "json_desc" else None,
        )
        db.add(row)
        db.flush()

    return toggle_register_field(db, row.id, field, value, device_name)


def _toggle_selection_for_row(
    db: Session, row: ProtocolRegister, field: str, value: bool, device_name: str
) -> DeviceProtocolSelection | None:
    """
    Applies one field/value to one row's DeviceProtocolSelection, with no
    code<->desc cascade — the cascade lives in toggle_register_field, which
    calls this once for the row actually requested and, for mask/screen,
    once more (non-recursively) for its code<->desc counterpart if one
    exists. Kept separate so that second call can't trigger a further
    cascade of its own and loop.
    """
    target: DeviceProtocolSelection | None = (
        db.query(DeviceProtocolSelection)
        .filter(
            DeviceProtocolSelection.device_name == device_name,
            DeviceProtocolSelection.protocol_name == row.protocol_name,
            DeviceProtocolSelection.registry_type == row.registry_type,
            DeviceProtocolSelection.register_address == row.register_address,
        )
        .first()
    )
    if target is None:
        target = DeviceProtocolSelection(
            device_name=device_name,
            protocol_name=row.protocol_name,
            registry_type=row.registry_type,
            register_address=row.register_address,
            user_write_enabled=False,
            mask_enabled=False,
            screen_enabled=False,
            user_write_enabled_disk=False,
            mask_enabled_disk=False,
            screen_enabled_disk=False,
            is_dirty=False,
        )
        db.add(target)
        db.flush()

    if field == "user_write_enabled" and value and not row.is_writable_by_protocol:
        return None

    setattr(target, field, value)
    # Mask and screen are mutually exclusive for a register.
    if field == "mask_enabled" and value:
        setattr(target, "screen_enabled", False)
    elif field == "screen_enabled" and value:
        setattr(target, "mask_enabled", False)
    target.mark_dirty()
    db.flush()
    return target


def toggle_register_field(
    db: Session,
    register_id: int,
    field: str,   # "user_write_enabled" | "mask_enabled" | "screen_enabled"
    value: bool,
    device_name: str | None = None,
) -> DeviceProtocolSelection | None:
    """
    Toggle a single write/mask/screen field for one device's selection of a
    register. These are device-scoped choices (see DeviceProtocolSelection),
    not part of the shared ProtocolRegister definition, so a device_name is
    required — there's no protocol-wide toggle to fall back to.
    Enforces the two-gate rule: user_write_enabled can only be True
    if the protocol permits writing.

    mask_enabled / screen_enabled cascade to a register's JSON
    code-description counterpart, in whichever direction is DB-resolvable:
      - row is the "<name>_desc" entry (is_json_desc, source_variable_name
        set) -> cascades to the code register it decodes, which is always a
        real, already-materialized CSV row for a CSV-backed protocol.
      - row is a plain CSV register with an *already materialized* desc
        companion (found by source_variable_name == row.variable_name) ->
        cascades to it.
      - row is a plain CSV register whose desc companion has never been
        selected/materialized before -> nothing to cascade to here; that
        direction is resolved one layer up, in routers.protocols, which has
        transport access (via build_json_desc_rows) to discover and
        materialize the not-yet-existing desc row.
    This mirrors the requirement that selecting either a description or its
    code always selects both, and that only the code's own selection is
    ever written to a mask/screen file (see
    config_writer._write_mask_screen_files) — cascading here is what makes
    a description's selection actually reach the file, via its code.

    user_write_enabled never cascades — write-back is only ever meaningful
    for the code register itself (descriptions/synthetics are always "R").

    Returns the updated row, or None if not found / not allowed.
    """
    allowed_fields: set[str] = {"user_write_enabled", "mask_enabled", "screen_enabled"}
    if field not in allowed_fields or not device_name:
        return None

    row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
    if row is None:
        return None

    target: DeviceProtocolSelection | None = _toggle_selection_for_row(db, row, field, value, device_name)
    if target is None:
        return None

    if field in ("mask_enabled", "screen_enabled"):
        counterpart: ProtocolRegister | None = None
        if row.is_json_desc and row.source_variable_name:
            counterpart = _find_protocol_register(db, row.protocol_name, row.registry_type, row.source_variable_name)
        elif not row.is_synthetic and not row.is_json_desc:
            counterpart = (
                db.query(ProtocolRegister)
                .filter(
                    ProtocolRegister.protocol_name == row.protocol_name,
                    ProtocolRegister.registry_type == row.registry_type,
                    ProtocolRegister.is_json_desc == True,   # noqa: E712
                    ProtocolRegister.source_variable_name == row.variable_name,
                )
                .first()
            )
        if counterpart is not None and counterpart.id != row.id:
            _toggle_selection_for_row(db, counterpart, field, value, device_name)

    refresh_app_state(db)
    _log.debug("toggle_register_field: register=%d field=%s value=%s device=%s", register_id, field, value, device_name)
    return target


def toggle_register_pending_delete(db: Session, register_id: int, value: bool) -> ProtocolRegister | None:
    """
    Stage or unstage a protocol editor row for deletion. Protocol-editor-only
    (no device_name) — this is shared protocol *definition* state, same
    scope as update_protocol_register_field, not a per-device selection.

    Deliberately refuses synthetic/json_desc rows: those aren't real CSV
    rows to begin with (see ProtocolRegister.is_synthetic docstring) — they
    already don't get written back, and materialize on demand the next time
    a device selects that metric, so "deleting" one here would be
    meaningless (and, worse, misleading: the checkbox would appear to do
    something permanent to a row that isn't really there).
    """
    row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
    if row is None or row.is_synthetic or row.is_json_desc:
        return None

    row.pending_delete = value
    db.flush()
    refresh_app_state(db)
    return row


def update_protocol_register_field(db: Session, register_id: int, field: str, value: str) -> ProtocolRegister | None:
    allowed_fields: set[str] = {
        "variable_name",
        "documented_name",
        "unit",
        "data_type",
        "values_range",
        "adjustments",
        "note",
        "read_interval",
        "write_mode_protocol",
    }
    if field not in allowed_fields:
        return None

    row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
    if row is None:
        return None

    setattr(row, field, value)
    row.is_dirty = True
    db.flush()
    refresh_app_state(db)
    return row


# A generic JSON value — used for get_protocol_json()'s return type below.
# json.loads() itself is typed Any at the stdlib level (JSON content is
# inherently dynamic there), but every protocol .json config file this
# reads is a top-level JSON object, so the function's own declared return
# type can be this specific instead of inheriting json.loads()'s Any.
JSONValue = dict[str, "JSONValue"] | list["JSONValue"] | str | int | float | bool | None


def get_protocol_json(
    protocols_dir: Path,
    protocol_group: str,
    protocol_name: str,
    config_dir: Path | None = None,
)  -> Tuple[dict[str, JSONValue], Literal[True]] | Tuple[None, Literal[False]] | Tuple[dict[str, JSONValue], Literal[False]]:
    """
    Load the JSON config file for a protocol.
    Checks config_dir first (user override), then falls back to protocols_dir.
    Returns (data, is_override) where is_override=True means a modified copy exists.
    """
    # Check config override first
    if config_dir is not None:
        override_path: Path = config_dir / f"{protocol_name}.json"
        if override_path.exists():
            try:
                _log.debug("get_protocol_json: loading override from %s", override_path)
                return json.loads(override_path.read_text(encoding="utf-8")), True
            except Exception:
                _log.warning("Failed to load protocol json override file %s", override_path)
                return None, False

    json_path: Path = protocols_dir / protocol_group / f"{protocol_name}.json"
    if json_path.exists():
        try:
            _log.debug("get_protocol_json: loading default from %s", json_path)
            return json.loads(json_path.read_text(encoding="utf-8")), False
        except Exception:
            _log.warning("Failed to load protocol json file %s", json_path)
            return None, False
    _log.debug("get_protocol_json: no json file found for %s/%s", protocol_group, protocol_name)
    return None, False


def register_row_sort_key(row: DeviceRegisterView) -> tuple[int, str]:
    """Sort key for the webUI register table.

    Groups rows, in order:
      0. Synthetic metrics (row.is_synthetic)
      1. JSON code-description metrics (row.is_json_desc) — auto-generated
         "<name>_desc" companions, see build_json_desc_rows()
      2. Any row with at least one W/M/S checkbox selected
         (user_write_enabled, mask_enabled, or screen_enabled)
      3. Everything else

    ...and alphabetizes by variable_name (case-insensitive) within each
    group. Used to set the *initial* order of the table when it's rendered
    — this doesn't stop a client-side table widget from letting the user
    re-sort by clicking a column header afterward.
    """
    if row.is_synthetic:
        group = 0
    elif row.is_json_desc:
        group = 1
    elif row.user_write_enabled or row.mask_enabled or row.screen_enabled:
        group = 2
    else:
        group = 3
    return (group, (row.variable_name or "").lower())


def build_synthetic_rows(
    transport: transport_base, registry_type: str | None = None, exclude_names: set[str] | None = None
) -> list[DeviceRegisterView]:
    """Build display-only DeviceRegisterView rows for a transport's synthetic fields.

    Reads ``transport.synthetic_fields_metadata`` — a list of
    ``(variable_name, data_type, unit_mod, note)`` tuples, or
    ``(variable_name, data_type, unit_mod, note, registry_type)`` tuples
    where the 5th element is a lowercase registry-type string like
    "holding"/"input" (see ``transport_base.synthetic_fields_metadata`` and
    ``eg4_metadata.eg4_synthetic_fields_metadata``) — and constructs one
    ``DeviceRegisterView`` per field with ``is_synthetic=True``.

    ``registry_type``, when given, filters to only the synthetic fields
    tagged with that registry (case-insensitive). Fields with no 5th
    element (older ``synthetic_fields_metadata`` implementations, e.g.
    ``modbus_eg4_ll_s_tcp``/``modbus_eg4_ll_s_rtu``, which don't yet tag a
    registry) are treated as registry-agnostic and always included,
    regardless of the requested ``registry_type`` — that's the same
    every-tab behavior this function had before registry tagging existed,
    so those transports' tables are unaffected. Pass ``registry_type=None``
    (the default) to get every synthetic field regardless of registry, e.g.
    for a combined/JSON view.

    ``exclude_names``, when given, skips any field whose variable_name is
    in the set — used by the /table endpoint to drop fields that have
    already been materialized into a real ProtocolRegister row (once a
    device has selected one, it comes back from the DB query with a real
    id and belongs there, not in this live-only list — see
    materialize_and_toggle_virtual_metric()).

    Rows default to display-only (``id = -1``, no toggle endpoint
    reachable) — that's the pre-selection state for every synthetic field.
    Once selected for a device, the materialized DB row (with a real id)
    takes over via get_protocol_registers(), and this function's copy of
    that same field is excluded via exclude_names as above.
    - ``register_address = "synthetic"`` to distinguish from CSV rows
    - ``write_mode_protocol = "R"`` so the W checkbox is always disabled

    Returns an empty list when the transport has no synthetic fields or
    ``synthetic_fields_metadata`` is not defined.
    """
    metadata: list[SyntheticFieldMetadata] = getattr(
        transport, "synthetic_fields_metadata", []
    )
    if not metadata:
        return []

    exclude_names = exclude_names or set()
    rows: list[DeviceRegisterView] = []
    for field in metadata:
        variable_name: str = field[0]
        if variable_name in exclude_names:
            continue
        data_type: str | None = field[1]
        unit_mod: float = field[2]
        note: str | None = field[3]
        rest: tuple[str, ...] = field[4:]
        field_registry_type: str | None = str(rest[0]).lower() if rest and rest[0] else None

        if (
            registry_type is not None
            and field_registry_type is not None
            and field_registry_type != registry_type.lower()
        ):
            continue

        rows.append(
            DeviceRegisterView(
                id=-1,
                protocol_name=getattr(transport, "protocol_version", ""),
                registry_type="synthetic",
                register_address="synthetic",
                variable_name=variable_name,
                documented_name=variable_name,
                unit=str(unit_mod),
                data_type=data_type,
                values_range="",
                adjustments=None,
                note=note,
                read_interval=None,
                write_mode_protocol="R",
                user_write_enabled=False,
                mask_enabled=False,
                screen_enabled=False,
                is_dirty=False,
                paired_high_address=None,
                is_synthetic=True,
            )
        )
    return rows


def build_json_desc_rows(
    transport: transport_base,
    registry_type: str | None = None,
    address_by_variable: dict[str, str] | None = None,
    exclude_names: set[str] | None = None,
) -> list[DeviceRegisterView]:
    """Build display-only DeviceRegisterView rows for JSON-code-derived
    "<name>_desc" entries (see protocol_settings._add_code_description_entries).

    These are NOT literal CSV rows — ProtocolRegister only ever mirrors
    real CSV register rows (see its docstring) — they're synthesized at
    registry-map load time for any register that carries a code/enum
    dict, so like synthetic rows they only ever exist on the live
    transport's registry_map, never in the DB. Identified by
    registry_map_entry.description_source being truthy (it holds the
    source register's variable_name, not just a bool — see
    _add_code_description_entries).

    registry_type, when given, filters to entries whose registry bucket
    matches (case-insensitive) — mirrors build_synthetic_rows' filtering so
    the /table endpoint can request just the tab currently open. Reads the
    registry_map's own keys directly (rather than trusting each entry's own
    .registry_type) so this stays correct regardless of which Registry_Type
    members exist — same reasoning as timescaledb._extract_metric_names and
    bridge_service._active_metric_names_for_protocol.

    address_by_variable, when given, maps a source register's variable_name
    to its already-known register_address (e.g. built from the DB rows
    already fetched for this tab) so a _desc row can display the address of
    the register it decodes rather than a placeholder. Falls back to the
    source variable_name itself when no lookup is supplied or the source
    isn't found in it (e.g. the source register lives on a tab that hasn't
    been fetched yet).

    exclude_names, when given, skips any entry whose variable_name is in
    the set — same reasoning as build_synthetic_rows' exclude_names: drops
    entries already materialized into a real ProtocolRegister row for this
    device (see materialize_and_toggle_virtual_metric()).

    Display-only, same as synthetic rows:
    - id = -1, no toggle PATCH endpoint reachable
    - write_mode_protocol = "R" (desc entries are always WriteMode.READDISABLED)

    Returns an empty list when the transport has no registry_map, or no
    entry in it carries description_source.
    """
    registry_map: dict[Registry_Type, list[registry_map_entry]] = getattr(transport, "registry_map", None) or {}
    if not registry_map:
        return []

    address_by_variable = address_by_variable or {}
    exclude_names = exclude_names or set()
    rows: list[DeviceRegisterView] = []

    for reg_type, entries in registry_map.items():
        type_name: str = getattr(reg_type, "name", str(reg_type)).lower()
        if registry_type is not None and type_name != registry_type.lower():
            continue

        for entry in entries or []:
            source_name: str | None = getattr(entry, "description_source", None)
            if not source_name:
                continue
            if entry.variable_name in exclude_names:
                continue

            rows.append(
                DeviceRegisterView(
                    id=-1,
                    protocol_name=getattr(transport, "protocol_version", ""),
                    registry_type=type_name,
                    register_address=address_by_variable.get(source_name, source_name),
                    variable_name=entry.variable_name,
                    documented_name=getattr(entry, "documented_name", entry.variable_name),
                    unit=getattr(entry, "unit", "") or "",
                    data_type=str(getattr(entry, "data_type", "")),
                    values_range="",
                    adjustments=None,
                    note=getattr(entry, "note", None),
                    read_interval=getattr(entry, "read_interval", None),
                    write_mode_protocol="R",
                    user_write_enabled=False,
                    mask_enabled=False,
                    screen_enabled=False,
                    is_dirty=False,
                    paired_high_address=None,
                    is_json_desc=True,
                    source_variable_name=source_name,
                )
            )
    return rows


def export_protocol_registers(
    db: Session,
    protocol_name: str,
    registry_type: str,
    device_name: str | None = None,
) -> list[dict[str, str | bool]]:
    """
    Return ALL registers for a protocol/registry_type as a flat list of dicts
    suitable for CSV or JSON export.  Unlike get_protocol_registers this is
    unpaginated and always returns every row.

    When device_name is supplied the W/M/S selections for that device are
    merged in, matching the device view in the table.  Paired-register rows
    include both the logical stem address and the paired high address so the
    exported file documents the full physical address span.
    """
    protocol_rows: list[ProtocolRegister] = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
        )
        .order_by(ProtocolRegister.register_address)
        .all()
    )

    selections: dict[tuple[str, str, str], DeviceProtocolSelection] = {}
    if device_name:
        selections = {
            (r.protocol_name, r.registry_type, r.register_address): r
            for r in db.query(DeviceProtocolSelection).filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == protocol_name,
                DeviceProtocolSelection.registry_type == registry_type,
            ).all()
        }

    result: list[dict[str, str | bool]] = []
    for row in protocol_rows:
        paired_high: str | None = _safe_paired_address(row)
        # Address column: show range "40-41" for paired rows, plain address otherwise
        address_display: str = (
            f"{row.register_address}-{paired_high}" if paired_high
            else str(row.register_address)
        )

        entry: dict[str, str | bool] = {
            "register_address":   address_display,
            "variable_name":      row.variable_name,
            "documented_name":    row.documented_name,
            "unit":               row.unit or "",
            "data_type":          row.data_type or "",
            "values_range":       row.values_range or "",
            "write_mode_protocol": row.write_mode_protocol,
            "adjustments":        row.adjustments or "",
            "note":               row.note or "",
            "read_interval":      row.read_interval or "",
            "is_paired_register": bool(paired_high),
        }

        if device_name:
            s: DeviceProtocolSelection | None = selections.get(
                (row.protocol_name, row.registry_type, row.register_address)
            )
            entry["write_enabled"] = s.user_write_enabled if s else False
            entry["mask_enabled"]  = s.mask_enabled if s else False
            entry["screen_enabled"] = s.screen_enabled if s else False

        result.append(entry)

    return result


def get_protocol_groups(protocols_dir: Path) -> list[dict[str, str | list[str]]]:
    """
    Scan protocols_dir and return the cascading menu structure:
    [ { group: "eg4", protocols: ["eg4_18kpv_holding", "eg4_18kpv_input", ...] } ]
    """
    groups: list[dict[str, str | list[str]]] = []
    if not protocols_dir.exists():
        return groups

    _log.debug("get_protocol_groups: scanning %s", protocols_dir)
    for group_dir in sorted(protocols_dir.iterdir()):
        if not group_dir.is_dir():
            continue
        protocols: List[str] = sorted(
            f.stem for f in group_dir.iterdir()
            if f.suffix.lower() in (".csv", ".json")
            and not f.name.endswith(".override.csv")
        )
        if protocols:
            groups.append({"group": group_dir.name, "protocols": protocols})

    return groups
