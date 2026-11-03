# Description: SunSpec discovery provider: walks the model chain and reports where each model starts.
# File: sunspec.py
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

"""SunSpec discovery provider.

A SunSpec device exposes a *chain* of models.  The chain starts with the marker ``SunS`` (two registers,
``0x5375 0x6E53``), followed by a sequence of models, each introduced by a two-register header::

    register +0   model ID      (1 = common, 101/102/103 = inverter, 203 = meter, ...)
    register +1   model length  (number of data registers after the header)

The next header follows at ``this header + 2 + length``.  The chain ends with a header whose ID is
``0xFFFF``.  Which models are present, and therefore where each one sits, depends on the equipment
attached, which is exactly the situation the dynamic register maps exist for.

Anchors reported (all integers; addresses are wire/PDU addresses, matching the registry-map CSVs):

``sunspec_base``
    Address of the ``SunS`` marker.
``model_<id>_<n>_base`` / ``model_<id>_<n>_length`` / ``model_<id>_count``
    Address of the header of the n-th (1-based) instance of model ``<id>``, that instance's length, and
    how many instances the chain contains.  A template row offset is measured from the header, so the
    model ID register is offset 0 and the first data register is offset 2, as in the SunSpec documents.
``<alias>_<n>_base`` / ``<alias>_<n>_length`` / ``<alias>_<n>_model`` / ``<alias>_count``
    The same, grouped by *kind of equipment* regardless of the exact model number, so a map can say
    ``inverter_1_base`` instead of caring whether the device implements model 101, 102 or 103.  See
    ``SunSpecProvider.MODEL_ALIASES``.  ``<alias>_<n>_model`` is the actual model ID found.

Protocol JSON settings read by this provider (all optional):

``sunspec_base_address``
    Wire address of the ``SunS`` marker.  When absent the standard candidates 40000, 0 and 50000 are tried.
``sunspec_registry_type``
    ``holding`` (default) or ``input``.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from ..protocol_settings import Registry_Type, protocol_settings
from .base import DiscoveryProvider, DiscoveryReader, DiscoveryResult

_log: logging.Logger = logging.getLogger(__name__)


class SunSpecProvider(DiscoveryProvider):
    """Discover the SunSpec model chain of a device and report each model's address as an anchor."""

    name: ClassVar[str] = "sunspec"

    DEFAULT_BASE_ADDRESSES: ClassVar[tuple[int, ...]] = (40000, 0, 50000)
    """Marker addresses the SunSpec specification allows, tried in this order when none is configured."""

    MARKER_WORDS: ClassVar[tuple[int, int]] = (0x5375, 0x6E53)
    """The ASCII text ``SunS`` as two 16-bit registers."""

    END_MODEL_ID: ClassVar[int] = 0xFFFF
    """Model ID of the terminating header."""

    MAX_MODELS: ClassVar[int] = 64
    """Safety limit on chain length, so a corrupt chain cannot loop forever."""

    MODEL_ALIASES: ClassVar[dict[str, tuple[int, ...]]] = {
        "inverter": (101, 102, 103),
        "inverter_float": (111, 112, 113),
        "meter": (201, 202, 203, 204),
        "meter_float": (211, 212, 213, 214),
    }
    """Equipment kind -> model IDs that share one register layout (integer + scale-factor, or float)."""

    @classmethod
    def owned_pattern(cls) -> re.Pattern[str]:
        """Return the regex matching every anchor name this provider can emit (see ``DiscoveryResult.owned_patterns``)."""
        aliases: str = "|".join(re.escape(alias) for alias in sorted(cls.MODEL_ALIASES, key=len, reverse=True))
        return re.compile(
            r"^(?:sunspec_base"
            r"|model_\d+_count"
            r"|model_\d+_\d+_(?:base|length)"
            rf"|(?:{aliases})_count"
            rf"|(?:{aliases})_\d+_(?:base|length|model))$"
        )

    def discover(self, reader: DiscoveryReader, protocol: protocol_settings) -> DiscoveryResult | None:
        """Find the SunSpec marker, walk the model chain and return the resulting anchors.

        Returns ``None`` when no marker is found at any candidate address, i.e. the device is silent or is
        not a SunSpec device.  A marker with an empty or truncated chain still returns a result describing
        whatever was read.
        """
        registry_type: Registry_Type = self._registry_type(protocol)

        for base in self._candidate_bases(protocol):
            marker: list[int] | None = reader.read_discovery_registers(base, 2, registry_type)
            if marker is None or tuple(marker[:2]) != self.MARKER_WORDS:
                continue

            models: list[tuple[int, int, int]] = self._walk_chain(reader, registry_type, base)
            anchors: dict[str, int] = self.build_anchors(base, models)
            summary: str = f"SunSpec marker at {base}, {len(models)} model(s): " + ", ".join(str(model_id) for model_id, _, _ in models)
            _log.info(f"[{protocol.protocol}] {summary}")
            return DiscoveryResult(anchors=anchors, owned_patterns=(self.owned_pattern(),), summary=summary)

        _log.warning(f"[{protocol.protocol}] no SunSpec marker found at {list(self._candidate_bases(protocol))}")
        return None

    def build_anchors(self, base: int, models: list[tuple[int, int, int]]) -> dict[str, int]:
        """Turn a walked chain into the anchor dict described in the module docstring.

        ``models`` is a list of ``(model_id, header_address, length)`` in chain order.  Kept public and free
        of I/O so the numbering rules can be tested without a device.
        """
        anchors: dict[str, int] = {"sunspec_base": base}

        per_model: dict[int, int] = {}
        per_alias: dict[str, int] = {}
        for model_id, header, length in models:
            instance: int = per_model.get(model_id, 0) + 1
            per_model[model_id] = instance
            anchors[f"model_{model_id}_{instance}_base"] = header
            anchors[f"model_{model_id}_{instance}_length"] = length
            anchors[f"model_{model_id}_count"] = instance

            for alias, ids in self.MODEL_ALIASES.items():
                if model_id in ids:
                    alias_instance: int = per_alias.get(alias, 0) + 1
                    per_alias[alias] = alias_instance
                    anchors[f"{alias}_{alias_instance}_base"] = header
                    anchors[f"{alias}_{alias_instance}_length"] = length
                    anchors[f"{alias}_{alias_instance}_model"] = model_id
                    anchors[f"{alias}_count"] = alias_instance
        return anchors

    def _walk_chain(self, reader: DiscoveryReader, registry_type: Registry_Type, base: int) -> list[tuple[int, int, int]]:
        """Follow the model headers from the marker and return ``(model_id, header_address, length)`` for each model.

        Stops at the ``0xFFFF`` end header, at an all-zero header, after ``MAX_MODELS`` models, at the end of the
        16-bit address space, or when a header cannot be read (logged; the models found so far are kept).
        """
        models: list[tuple[int, int, int]] = []
        address: int = base + len(self.MARKER_WORDS)

        while len(models) < self.MAX_MODELS and address + 1 <= 0xFFFF:
            header: list[int] | None = reader.read_discovery_registers(address, 2, registry_type)
            if header is None or len(header) < 2:
                _log.warning(f"SunSpec chain: header at {address} not readable; keeping {len(models)} model(s) found so far")
                break
            model_id: int = header[0]
            length: int = header[1]
            if model_id == self.END_MODEL_ID or (model_id == 0 and length == 0):
                break
            models.append((model_id, address, length))
            address += 2 + length

        return models

    def _candidate_bases(self, protocol: protocol_settings) -> tuple[int, ...]:
        """Return the marker addresses to try: the configured one alone, else the specification's three."""
        configured: str = protocol.settings.get("sunspec_base_address", "").strip()
        if configured:
            try:
                return (int(configured, 0),)
            except ValueError:
                _log.warning(f"[{protocol.protocol}] sunspec_base_address '{configured}' is not a number; using the standard candidates")
        return self.DEFAULT_BASE_ADDRESSES

    def _registry_type(self, protocol: protocol_settings) -> Registry_Type:
        """Return the register space to read: ``holding`` unless the protocol JSON says ``input``."""
        name: str = protocol.settings.get("sunspec_registry_type", "holding").strip().upper()
        return Registry_Type.INPUT if name == "INPUT" else Registry_Type.HOLDING
