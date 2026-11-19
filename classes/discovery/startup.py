# Description: Startup read: capture the registers that rarely change once per connection instead of polling them.
# File: startup.py
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

"""Startup read of the registers that rarely or never change.

Meters carry a lot of configuration (transformer ratios, communication settings, pulse constants) and logs
(history by month and day, event records) that change at most a few times a day.  Polling them every cycle wastes
bus time, and on a slow serial line it steals time from the measurements that matter.

A row opts in by putting ``startup`` in its ``read interval`` cell.  The row is otherwise an ordinary row: it is
listed in the register map, can be selected, masked or screened in the web UI, keeps its data type, unit, code
table and adjustments, and is skipped when it is marked ``RD`` (read disabled) or ``WO`` (write only).  Once per
connection (and again after a reconnect) ``read_startup_values`` reads those rows, decodes them through the normal
register machinery and returns the values.  The transport caches them and adds them to every poll cycle's data,
so bridges and the web UI see them like any other value.  Context formulas can use them too (``pt_ratio`` and the
like are typical), see ``protocol_settings.static_values``.

Record blocks
-------------
Some meters return history and event logs as numbered *records*: reading ``words`` registers at a record's address
returns the whole record, and every record has the same layout.  A protocol declares the block under
``record_blocks`` in its JSON and describes the layout once, in template rows (see ``RecordBlock``).  Each record
is read with one request of exactly ``words`` registers at exactly the record's address, never merged with a
neighboring record, because the meter only answers a whole-record read.  A record is read only if at least one of
its rows is enabled, so a block with every row read-disabled costs nothing.

Reserved registers
------------------
Registers the manufacturer documents as reserved are listed under ``unreadable_registers`` in the JSON.  A startup
request is never built across one, because a meter may refuse a request that touches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..protocol_settings import VIRTUAL_REGISTER_BASE, Registry_Type, WriteMode

if TYPE_CHECKING:
    from ..protocol_settings import RecordBlock, protocol_settings, registry_map_entry
    from .base import DiscoveryReader

_LOG: logging.Logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE: int = 40
""" Same default as the polling code. """

_MAX_CONSECUTIVE_FAILURES: int = 3
""" Give up on a block, or on the whole startup read, after this many requests in a row go unanswered.  A meter that
does not have a feature (history on a basic model, an optional module) answers none of its requests, and each
unanswered request costs a timeout. """


@dataclass
class StartupResult:
    """What one startup read captured."""

    values: dict[str, int | float | str] = field(default_factory=lambda: {})
    requests: int = 0
    failed_requests: int = 0
    records_read: int = 0
    gave_up: bool = False
    """True when the read stopped early because the device stopped answering."""

    @property
    def complete(self) -> bool:
        """True when every request was answered."""
        return self.failed_requests == 0 and not self.gave_up

    def summary(self) -> str:
        """One line for the log."""
        return (
            f"{len(self.values)} values from {self.requests} requests ({self.records_read} records), "
            f"{self.failed_requests} unanswered" + (", stopped early" if self.gave_up else "")
        )


def startup_entries(protocol: protocol_settings, registry_type: Registry_Type) -> list[registry_map_entry]:
    """Rows of ``registry_type`` the startup read should fetch: ``startup`` rows that are readable."""
    return [
        entry
        for entry in protocol.registry_map.get(registry_type, [])
        if entry.read_at_startup and entry.write_mode in (WriteMode.READ, WriteMode.WRITE) and not entry.description_source
    ]


def _batch_size(protocol: protocol_settings) -> int:
    try:
        return max(1, int(protocol.settings.get("batch_size", _DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_SIZE


def plain_ranges(protocol: protocol_settings, registry_type: Registry_Type, entries: list[registry_map_entry]) -> list[tuple[int, int]]:
    """Group ordinary startup rows into ``(start, count)`` requests.

    Neighboring rows share a request while the request stays within the protocol's batch size and no
    unreadable register lies between them.  A row that itself sits on an unreadable register is left out.
    """
    spans: list[tuple[int, int]] = []
    for entry in entries:
        if entry.register >= VIRTUAL_REGISTER_BASE:
            continue
        step: int = protocol.register_address_step
        last: int = max(entry.register_list) if entry.register_list else entry.register + (protocol.entry_word_count(entry) - 1) * step
        if protocol.is_unreadable(registry_type, entry.register, last):
            _LOG.warning(f"[{protocol.protocol}] not reading '{entry.variable_name}' at {entry.register}: the manufacturer documents it as unreadable")
            continue
        spans.append((entry.register, last))

    batch: int = _batch_size(protocol)
    step = protocol.register_address_step
    ranges: list[tuple[int, int]] = []
    group_first: int | None = None
    group_last: int = 0
    for first, last in sorted(spans):
        if group_first is None:
            group_first, group_last = first, last
            continue
        merged_last: int = max(group_last, last)
        crosses_unreadable: bool = first > group_last + 1 and protocol.is_unreadable(registry_type, group_last + 1, first - 1)
        if merged_last - group_first + 1 <= batch and not crosses_unreadable:
            group_last = merged_last
        else:
            ranges.append((group_first, (group_last - group_first) // step + 1))
            group_first, group_last = first, last
    if group_first is not None:
        ranges.append((group_first, (group_last - group_first) // step + 1))
    return ranges


class _Fetcher:
    """Makes the requests of one startup read and counts the ones the device does not answer."""

    def __init__(self, reader: DiscoveryReader, result: StartupResult, log: logging.Logger) -> None:
        self._reader: DiscoveryReader = reader
        self.result: StartupResult = result
        self._log: logging.Logger = log
        self.consecutive_failures: int = 0

    @property
    def should_stop(self) -> bool:
        """True once the device has stopped answering."""
        return self.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES

    def fetch(self, start: int, count: int, registry_type: Registry_Type, *, device_check: bool = True) -> list[int] | None:
        """Read ``count`` words at ``start``; ``None`` when the device does not answer or answers short.

        ``device_check`` False is for record reads: a record that is not there yet (a meter that has not been
        running for 48 months has no 48th month) is normal, so it is counted as unanswered but does not count
        toward concluding that the device itself has stopped answering.
        """
        self.result.requests += 1
        words: list[int] | None = self._reader.read_discovery_registers(start, count, registry_type)
        if words is None or len(words) < count:
            self.result.failed_requests += 1
            if device_check:
                self.consecutive_failures += 1
            self._log.debug(f"startup read: no answer for {registry_type.name} {start}-{start + count - 1}")
            return None
        if device_check:
            self.consecutive_failures = 0
        return words


def read_startup_values(reader: DiscoveryReader, protocol: protocol_settings, log: logging.Logger | None = None) -> StartupResult:
    """Read every startup row (and record block field) once and return the decoded values.

    Never raises: a request the device does not answer is counted and its rows are left out.  After
    ``_MAX_CONSECUTIVE_FAILURES`` unanswered requests in a row the read stops, so a device that is not there does
    not hold up connecting.
    """
    result: StartupResult = StartupResult()
    fetcher: _Fetcher = _Fetcher(reader, result, log or _LOG)
    plain_requests: int = 0
    plain_answers: int = 0

    step: int = protocol.register_address_step      # devices that address by byte offset space their words this far apart
    for registry_type in Registry_Type:
        entries: list[registry_map_entry] = startup_entries(protocol, registry_type)
        if not entries:
            continue
        registry: dict[int, int] = {}

        for start, count in plain_ranges(protocol, registry_type, entries):
            if fetcher.should_stop:
                break
            words: list[int] | None = fetcher.fetch(start, count, registry_type)
            plain_requests += 1
            if words is not None:
                plain_answers += 1
                registry.update({start + offset * step: word for offset, word in enumerate(words)})

        # Records are only worth requesting from a device that answered its ordinary rows: when none of those
        # was answered the device is not there, and each record request would cost a timeout.
        device_silent: bool = fetcher.should_stop or (plain_requests > 0 and plain_answers == 0)
        for block in protocol.record_blocks.values():
            if block.registry_type is registry_type and not device_silent:
                _read_block(block, entries, registry, fetcher, log or _LOG, step)

        if not registry:
            continue
        wanted: set[str] = {entry.variable_name for entry in entries}
        decode_entries: list[registry_map_entry] = [
            entry
            for entry in protocol.registry_map[registry_type]
            if entry.variable_name in wanted or entry.description_source in wanted
        ]
        result.values.update(protocol.process_registery(registry, decode_entries))

    result.gave_up = fetcher.should_stop or (plain_requests > 0 and plain_answers == 0)
    return result


def _read_block(
    block: RecordBlock,
    entries: list[registry_map_entry],
    registry: dict[int, int],
    fetcher: _Fetcher,
    log: logging.Logger,
    step: int = 1,
) -> None:
    """Read the records of ``block`` that have at least one startup row, placing their words at the virtual addresses."""
    wanted_records: set[int] = set()
    for entry in entries:
        record: int | None = block.record_of(entry.register) if entry.register >= VIRTUAL_REGISTER_BASE else None
        if record is not None:
            wanted_records.add(record)

    block_failures: int = 0
    for record in sorted(wanted_records):
        if fetcher.should_stop:
            return
        if block_failures >= _MAX_CONSECUTIVE_FAILURES:
            msg: str = f"startup read: block '{block.name}' record {record} is not answering, skipping its remaining records"
            log.info(msg)
            return
        words: list[int] | None = fetcher.fetch(block.handle(record), block.words, block.registry_type, device_check=False)
        if words is None:
            block_failures += 1
            continue
        block_failures = 0
        fetcher.result.records_read += 1
        base: int = block.virtual_base(record)
        registry.update({base + offset * step: word for offset, word in enumerate(words)})


class StartupCache:
    """Holds the values of the last startup read for a transport and decides when to try again.

    The transport calls ``refresh`` after it connects, ``retry_due`` at the start of each poll cycle, and
    ``merge_into`` on each cycle's data.  A read that was not complete (the meter was slow to answer after
    connecting, say) is retried, a limited number of times, a minute apart, so one bad connect does not leave
    the cached values, and every Context formula that depends on them, missing until the next reconnect.
    """

    RETRY_SECONDS: float = 60.0
    MAX_ATTEMPTS: int = 4

    def __init__(self, protocol: protocol_settings, log: logging.Logger) -> None:
        self._protocol: protocol_settings = protocol
        self._log: logging.Logger = log
        self._attempts: int = 0
        self._last_attempt: float = 0.0
        self._complete: bool = False

    @property
    def enabled(self) -> bool:
        """True when the protocol has anything to read at startup."""
        return self._protocol.has_startup_registers

    def refresh(self, reader: DiscoveryReader, now: float) -> StartupResult | None:
        """Run the startup read now (``now`` is the current time in seconds) and cache what it captured."""
        if not self.enabled:
            return None
        self._attempts += 1
        self._last_attempt = now
        result: StartupResult = read_startup_values(reader, self._protocol, self._log)
        if result.values or result.complete:
            # keep what an earlier attempt captured when this one returned nothing new
            merged: dict[str, int | float | str] = dict(self._protocol.static_values)
            merged.update(result.values)
            self._protocol.static_values = merged
        self._complete = result.complete
        level: int = logging.INFO if result.complete else logging.WARNING
        self._log.log(level, f"[{self._protocol.protocol}] startup read: {result.summary()}")
        return result

    def reset(self) -> None:
        """Forget the attempt count after a new connection, so the next ``refresh`` starts a fresh series."""
        self._attempts = 0
        self._complete = False

    def retry_due(self, now: float) -> bool:
        """True when the last read was incomplete and another attempt is allowed."""
        # cheap checks first: this runs at the start of every poll cycle
        return (
            not self._complete
            and 0 < self._attempts < self.MAX_ATTEMPTS
            and now - self._last_attempt >= self.RETRY_SECONDS
            and self.enabled
        )

    def merge_into(self, info: dict[str, int | float | str]) -> None:
        """Add the cached values to one cycle's data (values the cycle decoded itself win)."""
        for name, value in self._protocol.static_values.items():
            info.setdefault(name, value)
