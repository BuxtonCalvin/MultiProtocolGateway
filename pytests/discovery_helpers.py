"""Shared fixtures for the dynamic-discovery tests: a fake SunSpec device and chain builders.

``FakeReader`` is a dict of register values that implements ``DiscoveryReader`` and answers only the addresses it
holds, so discovery can be exercised without a Modbus stack.  Imported by ``test_dynamic_discovery``,
``test_dynamic_discovery_transport`` and ``test_sunspec_dynamic_maps``.
"""

from __future__ import annotations

import logging

from classes.discovery import DiscoveryResult, run_discovery
from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

BASE: int = 40000
SUNS: tuple[int, int] = (0x5375, 0x6E53)
DEFAULT_CHAIN: list[tuple[int, int]] = [(1, 65), (103, 50), (120, 26), (121, 30)]
"""The SolarEdge layout the JSON defaults describe: (model_id, length) in chain order."""
VERSION_ADDRESS: int = 40044
"""Wire address of the SunSpec common-model version string (the CSV ``version`` row)."""


class FakeReader:
    """A register image that implements ``DiscoveryReader``; unknown addresses read as a failure."""

    def __init__(self, words: dict[int, int] | None = None) -> None:
        """Start with ``words`` (address -> 16-bit value)."""
        self.words: dict[int, int] = dict(words or {})
        self.reads: list[tuple[int, int, Registry_Type]] = []

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        """Return ``count`` words from ``start`` or ``None`` if any is missing."""
        self.reads.append((start, count, registry_type))
        if any(start + offset not in self.words for offset in range(count)):
            return None
        return [self.words[start + offset] for offset in range(count)]


def chain_image(models: list[tuple[int, int]], base: int = BASE, end_marker: bool = True) -> dict[int, int]:
    """Build the register image of a SunSpec chain: marker, one header per model, then the end header."""
    words: dict[int, int] = {base: SUNS[0], base + 1: SUNS[1]}
    address: int = base + 2
    for model_id, length in models:
        words[address] = model_id
        words[address + 1] = length
        address += 2 + length
    if end_marker:
        words[address] = 0xFFFF
        words[address + 1] = 0
    return words


def ascii_words(text: str, count: int = 8) -> dict[int, int]:
    """Encode ``text`` as ``count`` ASCII registers starting at the version address (NUL padded)."""
    raw: bytes = text.encode("ascii").ljust(count * 2, b"\x00")
    return {VERSION_ADDRESS + index: (raw[2 * index] << 8) | raw[2 * index + 1] for index in range(count)}


def load(name: str) -> protocol_settings:
    """Load a protocol with logging silenced."""
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def holding(proto: protocol_settings) -> dict[str, registry_map_entry]:
    """Return the HOLDING map keyed by variable name."""
    return {entry.variable_name: entry for entry in proto.registry_map[Registry_Type.HOLDING]}


def discover_and_apply(proto: protocol_settings, reader: FakeReader) -> bool:
    """Run discovery against ``reader`` and apply the result the way the transport does."""
    result: DiscoveryResult | None = run_discovery(reader, proto, logging.getLogger("test"))
    assert result is not None
    return proto.apply_anchors(result.anchors, result.owned_patterns)


def tiled_chain(base: int, headers: list[tuple[int, int]], last_length: int = 20) -> dict[int, int]:
    """Build a chain image whose model headers sit at fixed addresses.

    ``headers`` is ``(model_id, header_address)`` in address order.  Each model's length is whatever fills the gap to
    the next header (so a gap holding models the map does not cover simply becomes part of the previous model), and
    the last model gets ``last_length``.  Used to describe the layout a protocol's JSON defaults stand for.
    """
    words: dict[int, int] = {base: SUNS[0], base + 1: SUNS[1]}
    for index, (model_id, address) in enumerate(headers):
        length: int = headers[index + 1][1] - address - 2 if index + 1 < len(headers) else last_length
        words[address], words[address + 1] = model_id, length
    last_model, last_address = headers[-1] # type: ignore
    words[last_address + 2 + last_length], words[last_address + 3 + last_length] = 0xFFFF, 0
    return words


def register_map(proto: protocol_settings) -> dict[str, int]:
    """Return ``variable_name -> register`` for the HOLDING map, for before/after comparisons."""
    return {name: entry.register for name, entry in holding(proto).items()}
