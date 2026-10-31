"""Unit tests for MigrationGate, the flag that pauses the flush worker during a schema migration (no database needed).

Several code paths set the flag around their schema work and clear it in a finally block: ensure_rollups, add_wide_rollup,
BridgeAdminManager.delete_fields (which calls add_wide_rollup while holding it itself) and edit_metric_values. With a plain
threading.Event the first one to finish cleared the flag while another was still altering tables.
"""

# pyright: strict

from __future__ import annotations

import threading

from classes.transports.timescaledb import MigrationGate


def test_it_behaves_like_an_event_for_a_single_migration() -> None:
    gate: MigrationGate = MigrationGate()
    assert not gate.is_set()
    gate.set()
    assert gate.is_set()
    gate.clear()
    assert not gate.is_set()


def test_a_nested_migration_does_not_release_the_outer_one() -> None:
    """delete_fields sets the flag, then add_wide_rollup sets and clears it again before delete_fields is done."""
    gate: MigrationGate = MigrationGate()
    gate.set()  # delete_fields
    gate.set()  # add_wide_rollup
    gate.clear()  # add_wide_rollup finishes
    assert gate.is_set()  # delete_fields still holds the flush worker back
    gate.clear()
    assert not gate.is_set()


def test_overlapping_migrations_on_different_threads_release_only_when_the_last_finishes() -> None:
    gate: MigrationGate = MigrationGate()
    second_started: threading.Event = threading.Event()
    first_may_finish: threading.Event = threading.Event()
    second_may_finish: threading.Event = threading.Event()

    def migration(started: threading.Event | None, may_finish: threading.Event) -> None:
        gate.set()
        if started is not None:
            started.set()
        may_finish.wait(5.0)
        gate.clear()

    first: threading.Thread = threading.Thread(target=migration, args=(None, first_may_finish))
    second: threading.Thread = threading.Thread(target=migration, args=(second_started, second_may_finish))
    first.start()
    second.start()
    assert second_started.wait(5.0)

    first_may_finish.set()
    first.join(5.0)
    assert gate.is_set()  # the second migration is still running

    second_may_finish.set()
    second.join(5.0)
    assert not gate.is_set()


def test_an_unmatched_clear_is_ignored() -> None:
    gate: MigrationGate = MigrationGate()
    gate.clear()
    gate.clear()
    assert not gate.is_set()
    gate.set()
    assert gate.is_set()  # a stray clear must not have left a negative count that swallows the next set
    gate.clear()
    assert not gate.is_set()
