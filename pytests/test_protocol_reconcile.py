"""Keeping the staging database in step with the protocol CSVs: the planner and the apply step.

Both work on plain data and duck-typed rows, so these tests need no database.  The scanner's own queries and
its use of the remembered file hashes are covered in ``test_scanner_reconcile``.

The scenario behind most of them is a ``docker pull``: the database volume survives, the ``protocols/``
directory is replaced by the new image, and the CSVs no longer match the rows the database holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from classes.WebServer.protocol_reconcile import (
    CARRIED_FIELDS,
    ReconcileOutcome,
    ReconcilePlan,
    StoredRegister,
    StoredSelection,
    apply_plan,
    file_sha256,
    plan_reconciliation,
)


def reg(row_id: int, address: str, name: str, dirty: bool = False) -> StoredRegister:
    """A stored register as the planner sees it."""
    return StoredRegister(id=row_id, address=address, variable_name=name, is_dirty=dirty)


def sel(row_id: int, device: str, address: str, dirty: bool = False) -> StoredSelection:
    """A stored device selection as the planner sees it."""
    return StoredSelection(id=row_id, device_name=device, address=address, is_dirty=dirty)


# --------------------------------------------------------------------------------------------------
# plan_reconciliation: which rows are stale
# --------------------------------------------------------------------------------------------------


def test_nothing_changes_when_every_stored_address_is_still_in_the_csv() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000", "40002"}, [reg(1, "40000", "a"), reg(2, "40002", "b")], [sel(1, "d", "40000")])
    assert plan.is_empty and plan == ReconcilePlan()


def test_rows_for_addresses_the_csv_no_longer_has_are_removed() -> None:
    """The -1 address shift: the new rows exist, the old ones are what is left over."""
    stored = [reg(1, "40001", "sunspec_id"), reg(2, "40000", "sunspec_id"), reg(3, "40005", "manufacturer"), reg(4, "40004", "manufacturer")]
    plan: ReconcilePlan = plan_reconciliation({"40000", "40004"}, stored, [])
    assert plan.delete_register_ids == (1, 3)


def test_a_converted_block_leaves_only_the_template_rows() -> None:
    """Fixed addresses became [anchor+offset] templates: every numeric row of that block goes."""
    stored = [reg(1, "40083", "ac_power"), reg(2, "[inverter_1_base+14]", "ac_power"), reg(3, "40002", "sunspec_did")]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]", "40002"}, stored, [])
    assert plan.delete_register_ids == (1,)


# --------------------------------------------------------------------------------------------------
# plan_reconciliation: pending edits
# --------------------------------------------------------------------------------------------------


def test_a_pending_edit_follows_its_variable_to_the_new_address() -> None:
    stored = [reg(1, "40083", "ac_power", dirty=True), reg(2, "[inverter_1_base+14]", "ac_power")]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, [])
    assert plan.carry_edits == ((1, 2),) and plan.dropped_edits == ()


def test_a_pending_edit_is_dropped_when_the_variable_has_no_current_row() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000"}, [reg(1, "40050", "gone", dirty=True), reg(2, "40000", "other")], [])
    assert plan.carry_edits == () and plan.dropped_edits == (1,) and plan.delete_register_ids == (1,)


def test_a_pending_edit_is_dropped_when_the_name_is_ambiguous() -> None:
    stored = [reg(1, "40050", "dup", dirty=True), reg(2, "40000", "dup"), reg(3, "40002", "dup")]
    plan: ReconcilePlan = plan_reconciliation({"40000", "40002"}, stored, [])
    assert plan.carry_edits == () and plan.dropped_edits == (1,)


def test_the_newer_edit_at_the_new_address_wins_over_a_stale_one() -> None:
    stored = [reg(1, "40083", "ac_power", dirty=True), reg(2, "[inverter_1_base+14]", "ac_power", dirty=True)]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, [])
    assert plan.carry_edits == () and plan.dropped_edits == (1,)


def test_two_stale_rows_cannot_both_claim_one_current_row() -> None:
    stored = [reg(1, "40001", "x", dirty=True), reg(2, "40002", "x", dirty=True), reg(3, "40000", "x")]
    plan: ReconcilePlan = plan_reconciliation({"40000"}, stored, [])
    assert plan.carry_edits == ((1, 3),) and plan.dropped_edits == (2,)


def test_a_stale_row_with_no_pending_edit_is_just_removed() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000"}, [reg(1, "40001", "x"), reg(2, "40000", "x")], [])
    assert plan.carry_edits == () and plan.dropped_edits == () and plan.delete_register_ids == (1,)


# --------------------------------------------------------------------------------------------------
# plan_reconciliation: device selections
# --------------------------------------------------------------------------------------------------


def test_a_selection_follows_the_variable_to_its_new_address() -> None:
    stored = [reg(1, "40083", "ac_power"), reg(2, "[inverter_1_base+14]", "ac_power")]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, [sel(1, "inverter_a", "40083", dirty=True)])
    assert plan.move_selections == ((1, "[inverter_1_base+14]"),)


def test_each_device_keeps_its_own_selection() -> None:
    stored = [reg(1, "40083", "ac_power"), reg(2, "[inverter_1_base+14]", "ac_power")]
    selections = [sel(1, "a", "40083", dirty=True), sel(2, "b", "40083", dirty=True)]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, selections)
    assert plan.move_selections == ((1, "[inverter_1_base+14]"), (2, "[inverter_1_base+14]"))


def test_staged_choices_are_merged_into_an_existing_clean_selection() -> None:
    stored = [reg(1, "40083", "ac_power"), reg(2, "[inverter_1_base+14]", "ac_power")]
    selections = [sel(1, "a", "40083", dirty=True), sel(2, "a", "[inverter_1_base+14]")]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, selections)
    assert plan.merge_selections == ((1, 2),) and plan.move_selections == ()


def test_a_stale_selection_that_is_not_dirty_is_dropped_because_the_files_recreate_it() -> None:
    stored = [reg(1, "40083", "ac_power"), reg(2, "[inverter_1_base+14]", "ac_power")]
    selections = [sel(1, "a", "40083"), sel(2, "a", "[inverter_1_base+14]")]
    plan: ReconcilePlan = plan_reconciliation({"[inverter_1_base+14]"}, stored, selections)
    assert plan.delete_selection_ids == (1,) and plan.merge_selections == ()


def test_a_selection_with_no_counterpart_is_removed() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000"}, [reg(1, "40050", "gone"), reg(2, "40000", "x")], [sel(1, "a", "40050", dirty=True)])
    assert plan.delete_selection_ids == (1,)


def test_a_selection_whose_register_row_is_already_gone_is_removed() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000"}, [reg(1, "40000", "x")], [sel(1, "a", "40999")])
    assert plan.delete_selection_ids == (1,)


def test_selections_at_current_addresses_are_left_alone() -> None:
    plan: ReconcilePlan = plan_reconciliation({"40000"}, [reg(1, "40000", "x")], [sel(1, "a", "40000", dirty=True)])
    assert plan.is_empty


# --------------------------------------------------------------------------------------------------
# apply_plan
# --------------------------------------------------------------------------------------------------


@dataclass
class FakeRegister:
    """A ``ProtocolRegister`` stand-in carrying every field ``apply_plan`` touches."""

    id: int
    register_address: str
    variable_name: str
    is_dirty: bool = False
    paired_high_address: str | None = None
    documented_name: str = ""
    unit: str = ""
    data_type: str = ""
    values_range: str = ""
    adjustments: str = ""
    note: str = ""
    read_interval: str = ""
    write_mode_protocol: str = "R"
    ha_device_class: str = ""
    ha_state_class: str = ""
    ha_entity_category: str = ""


@dataclass
class FakeSelection:
    """A ``DeviceProtocolSelection`` stand-in; ``mark_dirty`` is recorded rather than computed."""

    id: int
    register_address: str
    paired_high_address: str | None = None
    user_write_enabled: bool = False
    mask_enabled: bool = False
    screen_enabled: bool = False
    marked: bool = False

    def mark_dirty(self) -> None:
        """Record that the dirty flag was recomputed."""
        self.marked = True


def test_the_fields_the_scanner_protects_on_a_dirty_row_are_the_fields_that_are_carried() -> None:
    assert set(CARRIED_FIELDS) == {
        "documented_name", "unit", "data_type", "values_range", "adjustments", "note",
        "read_interval", "write_mode_protocol", "ha_device_class", "ha_state_class", "ha_entity_category",
    }  # fmt: skip


def test_a_carried_edit_copies_only_what_differs_and_leaves_the_target_pending() -> None:
    stale = FakeRegister(1, "40083", "ac_power", is_dirty=True, documented_name="My power", unit="kW", note="same")
    target = FakeRegister(2, "[inverter_1_base+14]", "ac_power", documented_name="AC Power", unit="W", note="same")
    deleted: list[object] = []

    outcome: ReconcileOutcome = apply_plan(ReconcilePlan(delete_register_ids=(1,), carry_edits=((1, 2),)), {1: stale, 2: target}, {}, deleted.append)

    assert (target.documented_name, target.unit, target.note) == ("My power", "kW", "same")
    assert target.is_dirty is True
    assert deleted == [stale] and outcome.carried_edits == 1 and outcome.pruned_registers == 1
    assert "documented_name, unit" in outcome.notes[0]


def test_a_dropped_edit_is_reported() -> None:
    stale = FakeRegister(1, "40050", "gone", is_dirty=True)
    outcome: ReconcileOutcome = apply_plan(ReconcilePlan(delete_register_ids=(1,), dropped_edits=(1,)), {1: stale}, {}, lambda row: None)
    assert outcome.dropped_edits == 1 and "'gone'" in outcome.notes[0] and "40050" in outcome.notes[0]


def test_a_moved_selection_takes_the_new_address_and_its_paired_high_address() -> None:
    target = FakeRegister(2, "40_41", "energy", paired_high_address="41")
    selection = FakeSelection(7, "40083", mask_enabled=True)
    outcome: ReconcileOutcome = apply_plan(ReconcilePlan(move_selections=((7, "40_41"),)), {2: target}, {7: selection}, lambda row: None)
    assert (selection.register_address, selection.paired_high_address, selection.mask_enabled) == ("40_41", "41", True)
    assert outcome.moved_selections == 1


def test_a_merged_selection_hands_its_staged_choices_to_the_target_and_disappears() -> None:
    source = FakeSelection(1, "40083", user_write_enabled=True, mask_enabled=True, screen_enabled=False)
    destination = FakeSelection(2, "[inverter_1_base+14]")
    deleted: list[object] = []
    outcome: ReconcileOutcome = apply_plan(ReconcilePlan(merge_selections=((1, 2),)), {}, {1: source, 2: destination}, deleted.append)
    assert (destination.user_write_enabled, destination.mask_enabled, destination.screen_enabled) == (True, True, False)
    assert destination.marked is True and deleted == [source] and outcome.merged_selections == 1


def test_selections_are_removed_before_the_registers_they_point_at() -> None:
    stale = FakeRegister(1, "40083", "ac_power")
    selection = FakeSelection(5, "40083")
    deleted: list[object] = []
    apply_plan(ReconcilePlan(delete_register_ids=(1,), delete_selection_ids=(5,)), {1: stale}, {5: selection}, deleted.append)
    assert deleted == [selection, stale]


# --------------------------------------------------------------------------------------------------
# file_sha256
# --------------------------------------------------------------------------------------------------


def test_the_file_hash_changes_with_the_content_and_only_then(tmp_path: Path) -> None:
    first: Path = tmp_path / "a.csv"
    second: Path = tmp_path / "b.csv"
    first.write_bytes(b"register,variable_name\n40000,x\n")
    second.write_bytes(b"register,variable_name\n40000,x\n")
    assert file_sha256(first) == file_sha256(second)
    second.write_bytes(b"register,variable_name\n40001,x\n")
    assert file_sha256(first) != file_sha256(second)
    assert len(file_sha256(first)) == 64
