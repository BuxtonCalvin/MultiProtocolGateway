# Description: Declarative string probes that turn a device's identification text into discovery anchors.
# File: probes.py
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

"""Probes: read an identification string and set an anchor depending on what it starts with.

Some manufacturers ship two register layouts under one protocol and the only way to tell them apart is
a firmware or model string (SolarEdge's early StorEdge hardware keeps its battery data in a proprietary
block, for instance).  A probe lets the protocol JSON express that without any code::

    "discovery_probes": [
      {"anchor": "battery_legacy_count", "registry": "holding", "address": 40044, "count": 8,
       "prefix": "0003", "value": 3}
    ]

Reads ``count`` registers at ``address``, decodes them as ASCII (two characters per register, high byte
first) and, if the text starts with ``prefix``, sets ``anchor`` to ``value``.  A readable string that does
not match sets the anchor to ``else_value`` (default 0).  An unreadable string sets nothing, so the
default from ``"discovery_defaults"`` stays in force.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..protocol_settings import Registry_Type
from .base import DiscoveryReader


@dataclass(frozen=True)
class ProbeSpec:
    """One parsed entry of the protocol JSON ``"discovery_probes"`` list."""

    anchor: str
    """Anchor that receives ``value`` (or ``else_value``)."""
    registry_type: Registry_Type
    """Register space to read from."""
    address: int
    """First register of the identification string, as the wire (PDU) address used in the CSV."""
    count: int
    """Number of registers to read."""
    prefix: str
    """Text the identification string must start with for the probe to match."""
    value: int = 1
    """Anchor value when the string matches."""
    else_value: int = 0
    """Anchor value when the string was read but does not match."""


def _to_int(raw: object, default: int | None = None) -> int | None:
    """Convert a JSON number, or a decimal / ``0x`` hex string, to an int; return ``default`` when it is neither."""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip(), 0)
        except ValueError:
            return default
    return default


def parse_probes(raw: object, log: logging.Logger) -> list[ProbeSpec]:
    """Parse the protocol JSON ``"discovery_probes"`` value into ``ProbeSpec`` objects.

    Malformed entries are logged and skipped so one bad probe cannot stop the others.  ``None`` or a
    non-list value yields an empty list.
    """
    if not isinstance(raw, list):
        return []

    probes: list[ProbeSpec] = []
    for index, item in enumerate(raw):  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        if not isinstance(item, dict):
            msg: str = f"discovery_probes[{index}] ignored: expected an object"
            log.warning(msg)
            continue
        fields: dict[str, object] = {str(key): value for key, value in item.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

        anchor: object = fields.get("anchor")
        address: int | None = _to_int(fields.get("address"))
        count: int | None = _to_int(fields.get("count"), 8)
        prefix: object = fields.get("prefix")
        registry_name: str = str(fields.get("registry", "holding")).strip().upper()

        if not isinstance(anchor, str) or not anchor or address is None or count is None or count < 1 or not isinstance(prefix, str) or not prefix:
            msg: str = f"discovery_probes[{index}] ignored: needs 'anchor', 'address' and 'prefix'"
            log.warning(msg)
            continue
        if registry_name not in Registry_Type.__members__:
            msg: str = f"discovery_probes[{index}] ignored: unknown registry '{registry_name}'"
            log.warning(msg)
            continue

        probes.append(
            ProbeSpec(
                anchor=anchor,
                registry_type=Registry_Type[registry_name],
                address=address,
                count=count,
                prefix=prefix,
                value=_to_int(fields.get("value"), 1) or 0,
                else_value=_to_int(fields.get("else_value"), 0) or 0,
            )
        )
    return probes


def decode_ascii(words: list[int]) -> str:
    """Decode 16-bit words as ASCII, high byte first, dropping NUL padding and surrounding whitespace."""
    raw: bytes = bytes(byte for word in words for byte in ((word >> 8) & 0xFF, word & 0xFF))
    return raw.decode("ascii", errors="ignore").replace("\x00", "").strip()


def run_probes(reader: DiscoveryReader, probes: list[ProbeSpec], log: logging.Logger) -> dict[str, int]:
    """Run every probe and return the anchors they set.

    A probe whose string cannot be read contributes nothing (the JSON default applies).  Never raises.
    """
    anchors: dict[str, int] = {}
    for probe in probes:
        words: list[int] | None = reader.read_discovery_registers(probe.address, probe.count, probe.registry_type)
        if words is None or len(words) < probe.count:
            msg: str = f"probe '{probe.anchor}': registers {probe.address}-{probe.address + probe.count - 1} not readable"
            log.debug(msg)
            continue
        text: str = decode_ascii(words)
        matched: bool = text.startswith(probe.prefix)
        anchors[probe.anchor] = probe.value if matched else probe.else_value
        msg: str = f"probe '{probe.anchor}': '{text}' {'starts' if matched else 'does not start'} with '{probe.prefix}' -> {anchors[probe.anchor]}"
        log.info(msg)
    return anchors
