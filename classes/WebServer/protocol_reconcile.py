# Description: Keeps the web UI's staging database in step with the protocol CSV files: plans and applies the removal of stale register rows.
# File: protocol_reconcile.py
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

"""Reconcile the ``protocol_registers`` table with the protocol CSV files on disk.

Why this exists
---------------
``protocol_registers`` is a *cache* of the protocol CSV files, keyed by
``(protocol_name, registry_type, register_address)``.  The scanner adds and updates rows
from the CSVs but, on its own, never removes one.  So whenever a CSV changes in a way that
removes or moves an address (corrected addresses, ``[anchor+offset]`` template rows, a
renamed block) the old rows stay behind next to the new ones: the editor shows every
variable twice, and the next commit regenerates the CSV from *all* the rows, writing the
stale ones back into the file.

The most common way this happens in practice is a ``docker pull``: the container's database
lives in a persistent volume, while ``protocols/`` is replaced by the new image.  The first
scan after the new container starts therefore sees CSV files that no longer match the
database, and this module brings the database back in line without anyone having to delete it.

How it works
------------
* A SHA-256 of each CSV is remembered per protocol (``protocol_file_states``).  A protocol is
  reconciled only when its file's hash differs from the remembered one, so an unchanged
  library costs nothing.  The first scan after this feature arrives has no remembered
  hashes, so every protocol is reconciled once, which also cleans up rows that went stale
  before the feature existed.
* Reconciling means removing the rows for addresses that are no longer in the CSV.  Rows that
  are not CSV rows (synthetic metrics, JSON code descriptions, ``~`` virtual addresses) are
  never touched.  Nothing is removed when the file could not be parsed, or yielded no
  registers, because a partial parse would otherwise delete valid rows.
* Work the user has not committed is carried over where it can be matched safely, by
  ``variable_name``, which is also what the mask, screen and writable files use:

  - a pending edit on a stale row is copied to the row that now holds that variable, exactly
    as the scanner already lets a pending edit win over the CSV at an unchanged address;
  - staged mask / screen / write choices (``DeviceProtocolSelection``, which has no foreign
    key to the register table) follow the variable to its new address.

  When there is no unambiguous match the stale pending edit is dropped and reported.

The decision logic (``plan_reconciliation``) and its application (``apply_plan``) work on plain
data and duck-typed row objects, so they are unit-tested without a database.  The scanner
supplies the queries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

CARRIED_FIELDS: tuple[str, ...] = (
    "documented_name",
    "unit",
    "data_type",
    "values_range",
    "adjustments",
    "note",
    "read_interval",
    "write_mode_protocol",
    "ha_device_class",
    "ha_state_class",
    "ha_entity_category",
)
"""Register fields a pending editor edit can change.

The same set the scanner leaves alone on a dirty row (see ``_upsert_protocol_register``), so a
carried edit behaves exactly like a pending edit at an unchanged address.
"""

VIRTUAL_ADDRESS_PREFIX: str = "~"
"""Addresses starting with this are materialized virtual metrics, never real CSV addresses."""


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of the file at ``path`` (read in chunks)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredRegister:
    """What the planner needs to know about one stored ``protocol_registers`` row."""

    id: int
    address: str
    variable_name: str
    is_dirty: bool


@dataclass(frozen=True)
class StoredSelection:
    """What the planner needs to know about one stored ``device_protocol_selections`` row."""

    id: int
    device_name: str
    address: str
    is_dirty: bool


@dataclass(frozen=True)
class ReconcilePlan:
    """The changes that bring the stored rows in line with the CSV, as ids and addresses.

    Everything here refers to rows by their database id, so the plan can be inspected, logged
    and tested without touching a database.
    """

    delete_register_ids: tuple[int, ...] = ()
    """Stale register rows to remove."""
    carry_edits: tuple[tuple[int, int], ...] = ()
    """``(stale_register_id, target_register_id)``: copy the stale row's pending edit onto the target."""
    dropped_edits: tuple[int, ...] = ()
    """Stale register rows with a pending edit that could not be matched to a current row."""
    move_selections: tuple[tuple[int, str], ...] = ()
    """``(selection_id, new_address)``: re-point a selection at the variable's new address."""
    merge_selections: tuple[tuple[int, int], ...] = ()
    """``(stale_selection_id, target_selection_id)``: copy staged choices onto the existing target selection, then delete the stale one."""
    delete_selection_ids: tuple[int, ...] = ()
    """Selections that point at an address that no longer exists and have no counterpart."""

    @property
    def is_empty(self) -> bool:
        """True when nothing needs to change."""
        return not (self.delete_register_ids or self.delete_selection_ids or self.move_selections or self.merge_selections)


@dataclass
class ReconcileOutcome:
    """Counts of what a reconciliation did, for logging and the scan statistics."""

    ran: bool = False
    """False when the protocol was skipped (unchanged file, or a parse that cannot be trusted)."""
    skipped_reason: str = ""
    pruned_registers: int = 0
    carried_edits: int = 0
    dropped_edits: int = 0
    moved_selections: int = 0
    merged_selections: int = 0
    deleted_selections: int = 0
    notes: list[str] = field(default_factory=lambda: [])
    """Human-readable lines (one per carried or dropped edit) for the scanner to log."""


def plan_reconciliation(
    fresh_addresses: Collection[str],
    registers: Sequence[StoredRegister],
    selections: Sequence[StoredSelection],
) -> ReconcilePlan:
    """Work out what to remove or carry over for one protocol file and registry type.

    ``fresh_addresses`` are the addresses the CSV defines now.  ``registers`` and ``selections`` are the
    stored rows for the same ``(protocol_name, registry_type)``; the caller leaves out virtual rows.

    A stale register (address no longer in the CSV) is matched to a current register by ``variable_name``
    only when exactly one current register has that name and no other stale register already claimed it.
    """
    fresh: set[str] = set(fresh_addresses)

    live_by_name: dict[str, list[StoredRegister]] = {}
    for register in registers:
        if register.address in fresh:
            live_by_name.setdefault(register.variable_name, []).append(register)

    stale: list[StoredRegister] = sorted((r for r in registers if r.address not in fresh), key=lambda r: r.id)

    target_of: dict[int, StoredRegister] = {}
    claimed: set[int] = set()
    for register in stale:
        candidates: list[StoredRegister] = live_by_name.get(register.variable_name, [])
        if len(candidates) == 1 and candidates[0].id not in claimed:
            target_of[register.id] = candidates[0]
            claimed.add(candidates[0].id)

    carry: list[tuple[int, int]] = []
    dropped: list[int] = []
    for register in stale:
        if not register.is_dirty:
            continue
        target: StoredRegister | None = target_of.get(register.id)
        if target is not None and not target.is_dirty:
            carry.append((register.id, target.id))
        else:
            dropped.append(register.id)

    stale_by_address: dict[str, StoredRegister] = {r.address: r for r in stale}
    slots: dict[tuple[str, str], StoredSelection] = {(s.device_name, s.address): s for s in selections}
    move: list[tuple[int, str]] = []
    merge: list[tuple[int, int]] = []
    delete_selections: list[int] = []
    for selection in sorted(selections, key=lambda s: s.id):
        if selection.address in fresh:
            continue
        stale_register: StoredRegister | None = stale_by_address.get(selection.address)
        target = target_of.get(stale_register.id) if stale_register is not None else None
        if target is None:
            delete_selections.append(selection.id)
            continue
        occupant: StoredSelection | None = slots.get((selection.device_name, target.address))
        if occupant is None:
            move.append((selection.id, target.address))
            slots[(selection.device_name, target.address)] = selection
        elif selection.is_dirty and not occupant.is_dirty:
            merge.append((selection.id, occupant.id))
        else:
            delete_selections.append(selection.id)

    return ReconcilePlan(
        delete_register_ids=tuple(r.id for r in stale),
        carry_edits=tuple(carry),
        dropped_edits=tuple(dropped),
        move_selections=tuple(move),
        merge_selections=tuple(merge),
        delete_selection_ids=tuple(delete_selections),
    )


class RegisterRow(Protocol):
    """The attributes of a ``ProtocolRegister`` that ``apply_plan`` reads and writes."""

    id: int
    register_address: str
    variable_name: str
    is_dirty: bool
    paired_high_address: str | None


class SelectionRow(Protocol):
    """The attributes of a ``DeviceProtocolSelection`` that ``apply_plan`` reads and writes."""

    id: int
    register_address: str
    paired_high_address: str | None
    user_write_enabled: bool
    mask_enabled: bool
    screen_enabled: bool

    def mark_dirty(self) -> None:
        """Recompute ``is_dirty`` from the staged and on-disk flags."""
        ...


def apply_plan(
    plan: ReconcilePlan,
    registers: Mapping[int, RegisterRow],
    selections: Mapping[int, SelectionRow],
    delete_row: Callable[[object], None],
) -> ReconcileOutcome:
    """Carry out ``plan`` on the given row objects (ids as in the plan) and report what happened.

    ``delete_row`` removes a row (the scanner passes ``Session.delete``).  Row objects are changed in place.
    Ordering matters only for the caller's unit of work: selections are re-pointed or removed first, then the
    stale registers go.
    """
    outcome = ReconcileOutcome(ran=True)

    for stale_id, target_id in plan.carry_edits:
        stale_row: RegisterRow = registers[stale_id]
        target_row: RegisterRow = registers[target_id]
        changed: list[str] = []
        for name in CARRIED_FIELDS:
            old: object = getattr(stale_row, name)
            if getattr(target_row, name) != old:
                setattr(target_row, name, old)
                changed.append(name)
        target_row.is_dirty = True
        outcome.carried_edits += 1
        outcome.notes.append(
            f"carried the pending edit on '{stale_row.variable_name}' from {stale_row.register_address} "
            f"to {target_row.register_address} ({', '.join(changed) or 'no field differs'})"
        )

    for stale_id in plan.dropped_edits:
        stale_row = registers[stale_id]
        outcome.dropped_edits += 1
        outcome.notes.append(
            f"dropped the pending edit on '{stale_row.variable_name}' at {stale_row.register_address}: the CSV no "
            f"longer has that address and no single, unedited current register carries that variable name"
        )

    for selection_id, new_address in plan.move_selections:
        selection: SelectionRow = selections[selection_id]
        selection.register_address = new_address
        target_register: RegisterRow | None = next((r for r in registers.values() if r.register_address == new_address), None)
        if target_register is not None:
            selection.paired_high_address = target_register.paired_high_address
        outcome.moved_selections += 1

    for stale_selection_id, target_selection_id in plan.merge_selections:
        source: SelectionRow = selections[stale_selection_id]
        destination: SelectionRow = selections[target_selection_id]
        destination.user_write_enabled = source.user_write_enabled
        destination.mask_enabled = source.mask_enabled
        destination.screen_enabled = source.screen_enabled
        destination.mark_dirty()
        delete_row(source)
        outcome.merged_selections += 1

    for selection_id in plan.delete_selection_ids:
        delete_row(selections[selection_id])
        outcome.deleted_selections += 1

    for register_id in plan.delete_register_ids:
        delete_row(registers[register_id])
        outcome.pruned_registers += 1

    return outcome
