# Description: Dynamic register discovery: provider registry and the run_discovery entry point used by the transports.
# File: __init__.py
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

"""Dynamic register discovery.

Most protocol maps are static: every register is at a fixed address and nothing needs to be discovered.
Some specifications (SunSpec is the common one) let a device place its blocks wherever the attached
equipment requires.  Those protocols opt in by naming a provider in their protocol JSON::

    "discovery": "sunspec"

On every connect the transport calls ``run_discovery``, which runs the named provider (and any
``"discovery_probes"``), and feeds the resulting anchors to ``protocol_settings.apply_anchors``.  A protocol
without the key is never touched, so static maps pay nothing.

To add a provider, subclass ``DiscoveryProvider`` in a new module and register it in ``_PROVIDERS``.
"""

from __future__ import annotations

import logging
import re

from ..protocol_settings import protocol_settings
from .base import DiscoveryProvider, DiscoveryReader, DiscoveryResult
from .probes import ProbeSpec, decode_ascii, parse_probes, run_probes
from .sunspec import SunSpecProvider

__all__: list[str] = [
    "DiscoveryProvider",
    "DiscoveryReader",
    "DiscoveryResult",
    "ProbeSpec",
    "SunSpecProvider",
    "decode_ascii",
    "get_discovery_provider",
    "parse_probes",
    "run_discovery",
    "run_probes",
]

_PROVIDERS: dict[str, type[DiscoveryProvider]] = {
    SunSpecProvider.name: SunSpecProvider,
}
"""Registered providers, keyed by the value of the protocol JSON ``"discovery"`` key."""


def get_discovery_provider(name: str) -> DiscoveryProvider | None:
    """Return a new instance of the provider registered under ``name`` (case-insensitive), or ``None`` if unknown."""
    provider_class: type[DiscoveryProvider] | None = _PROVIDERS.get(name.strip().lower())
    return provider_class() if provider_class is not None else None


def run_discovery(reader: DiscoveryReader, protocol: protocol_settings, log: logging.Logger) -> DiscoveryResult | None:
    """Run the protocol's declared provider and probes against the connected device.

    Combines the provider's anchors with those set by ``"discovery_probes"`` (probes win on a clash).
    Returns ``None`` when nothing could be learned, for instance because the device did not answer; the caller
    then leaves the maps as they are.  Returns a result with no anchors only if a provider ran and found an
    empty structure.  Never raises for read failures.
    """
    anchors: dict[str, int] = {}
    owned: tuple[re.Pattern[str], ...] = ()
    summaries: list[str] = []
    answered: bool = False

    if protocol.discovery_provider:
        provider: DiscoveryProvider | None = get_discovery_provider(protocol.discovery_provider)
        if provider is None:
            msg: str = f"[{protocol.protocol}] unknown discovery provider '{protocol.discovery_provider}'; known: {sorted(_PROVIDERS)}"
            log.warning(msg)
        else:
            result: DiscoveryResult | None = provider.discover(reader, protocol)
            if result is not None:
                anchors.update(result.anchors)
                owned = result.owned_patterns
                if result.summary:
                    summaries.append(result.summary)
                answered = True

    probes: list[ProbeSpec] = parse_probes(protocol.codes.get("discovery_probes"), log)
    if probes:
        probe_anchors: dict[str, int] = run_probes(reader, probes, log)
        if probe_anchors:
            anchors.update(probe_anchors)
            answered = True

    if not answered:
        return None
    return DiscoveryResult(anchors=anchors, owned_patterns=owned, summary="; ".join(summaries))
