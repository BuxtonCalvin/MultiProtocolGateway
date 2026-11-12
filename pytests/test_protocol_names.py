"""Mapping the editor's CSV-stem protocol names to the protocol's JSON file name.

The protocol editor keys its rows by CSV stem (``accuenergy_acuvim.holding_registry_map``), but the
JSON file is ``accuenergy_acuvim.json``.  Looking for ``<stem>.json`` finds nothing, which is why the
adjustments popup once reported a context variable defined in the JSON file as unknown.
"""

from __future__ import annotations

import pytest

from classes.WebServer.services.protocol_service import (
    json_name_candidates,
    protocol_base_name,
)


@pytest.mark.parametrize(
    ("stem", "base"),
    [
        ("accuenergy_acuvim.holding_registry_map", "accuenergy_acuvim"),
        ("accuenergy_acuvim.coil_registry_map", "accuenergy_acuvim"),
        ("accuenergy_acuvim.discrete_registry_map", "accuenergy_acuvim"),
        ("acrel_dtsd1352_c.input_registry_map", "acrel_dtsd1352_c"),
        ("some_can_protocol.registry_map", "some_can_protocol"),
        ("accuenergy_acuvim", "accuenergy_acuvim"),
        ("name.with.dots.holding_registry_map", "name.with.dots"),
        ("holding_registry_map", "holding_registry_map"),          # no dot: part of the name, not a suffix
        ("x.holding_registry_map.json", "x.holding_registry_map.json"),
    ],
)
def test_protocol_base_name(stem: str, base: str) -> None:
    assert protocol_base_name(stem) == base


def test_json_candidates_try_the_base_name_first_then_the_name_as_given() -> None:
    assert json_name_candidates("accuenergy_acuvim.holding_registry_map") == [
        "accuenergy_acuvim", "accuenergy_acuvim.holding_registry_map",
    ]


def test_a_plain_protocol_name_has_a_single_candidate() -> None:
    assert json_name_candidates("accuenergy_acuvim") == ["accuenergy_acuvim"]
