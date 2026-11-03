# Description: Base types for dynamic register discovery: the reader protocol, the result record and the provider interface.
# File: base.py
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

"""Base types shared by every register-discovery provider.

A *discovery provider* answers one question for a manufacturer whose register layout is not fixed:
"where did this particular device put each of its blocks?"  The answer is a flat dict of **anchors**
(name -> integer, usually a register address or an instance count).  The manufacturer's registry-map CSV
then describes each register as a *template row* relative to those anchors, for example
``[model_203_{i}_base+3]``, and ``protocol_settings`` turns the template rows into ordinary
``registry_map_entry`` objects.  Providers therefore never build registry entries themselves, which
keeps them small and lets the same CSV machinery serve every manufacturer.

See ``documentation/architecture/dynamic_register_discovery.md`` for the full design.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from ..protocol_settings import Registry_Type, protocol_settings


class DiscoveryReader(Protocol):
    """The one capability a provider needs from the transport: read a few raw words from the device."""

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        """Read ``count`` 16-bit words starting at ``start`` and return them in order.

        Returns ``None`` when the device does not answer or reports an error.  Must make a single
        attempt and must not raise, so a provider can probe addresses that may not exist.
        """
        ...


@dataclass(frozen=True)
class DiscoveryResult:
    """What a discovery pass learned about one device.

    ``anchors`` are laid over the defaults from the protocol JSON ``"discovery_defaults"`` object.
    ``owned_patterns`` lists the anchor names the provider is authoritative for: any *default* anchor
    matching one of them is discarded before the overlay, so a block that the device no longer reports
    drops out of the map instead of lingering from the defaults.  Anchors matching no pattern (for
    example a probe's) are only ever overridden, never removed.
    """

    anchors: dict[str, int] = field(default_factory=lambda: {})
    owned_patterns: tuple[re.Pattern[str], ...] = ()
    summary: str = ""


class DiscoveryProvider(ABC):
    """Interface implemented by each register-discovery provider.

    A provider is selected by the protocol JSON ``"discovery"`` key, which must equal ``name``.  Register
    a new provider by adding it to ``classes/discovery/__init__.py``.
    """

    name: ClassVar[str]
    """Value of the protocol JSON ``"discovery"`` key that selects this provider."""

    @abstractmethod
    def discover(self, reader: DiscoveryReader, protocol: protocol_settings) -> DiscoveryResult | None:
        """Inspect the connected device and return its anchors.

        ``protocol`` is the device's loaded protocol; providers read their own configuration from its
        ``settings`` (string values from the protocol JSON).  Return ``None`` when the device cannot be
        read or does not implement the specification, in which case the caller keeps the layout the maps
        were already built with.  Must not raise for ordinary read failures.
        """
        ...
