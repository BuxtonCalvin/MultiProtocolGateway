# Description: Synthetic fields (EG4 model/firmware_version/...) are opt-in:
#   not counted as "Selected", not forwarded to bridges, and only written to
#   the mask file, unless the user actively chose them with (M)ask.
# File: test_synthetic_opt_in.py
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

# pyright: reportPrivateUsage=false

"""Regression tests for the "Holding Selected: 4 with nothing chosen" bug."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from classes.protocol_settings import Registry_Type
from classes.WebServer.config_writer import _write_mask_screen_files
from classes.WebServer.models import (
    Base,
    DeviceProtocolSelection,
    ProtocolRegister,
    Setting,
)
from classes.WebServer.services.protocol_service import get_device_metric_summary
from protocol_gateway import Protocol_Gateway

PROTOCOL = "eg4_18kpv"
DEVICE = "Inverter1"
SYNTHETIC_FIELDS: list[tuple[Any, ...]] = [
    ("firmware_version", "ASCII", 1.0, "Synthetic", "holding"),
    ("hardware_kind", "ASCII", 1.0, "Synthetic", "holding"),
    ("is_gridboss", "BOOLEAN", 1.0, "Synthetic", "holding"),
    ("model", "ASCII", 1.0, "Synthetic", "holding"),
]


@pytest.fixture()
def db() -> Iterator[Session]:
    engine: Engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_register(db: Session, registry_type: str, address: str, name: str, **kw: Any) -> ProtocolRegister:
    row = ProtocolRegister(
        protocol_group="eg4", protocol_name=PROTOCOL, registry_type=registry_type,
        register_address=address, variable_name=name, documented_name=name, **kw,
    )
    db.add(row)
    db.flush()
    return row


def _select(db: Session, row: ProtocolRegister, **flags: bool) -> None:
    db.add(DeviceProtocolSelection(
        device_name=DEVICE, protocol_name=row.protocol_name, registry_type=row.registry_type,
        register_address=row.register_address, **flags,
    ))
    db.flush()


def _add_synthetic(db: Session, name: str, **flags: bool) -> None:
    row: ProtocolRegister = _add_register(db, "holding", f"~synthetic:{name}", name, is_synthetic=True)
    _select(db, row, **flags)


def _seed(db: Session, mask_inputs: int = 0) -> list[ProtocolRegister]:
    """3 real holding rows + 5 real input rows (the first `mask_inputs` input rows mask-selected)."""
    for i in range(3):
        _add_register(db, "holding", str(i), f"h{i}")
    inputs: list[ProtocolRegister] = [_add_register(db, "input", str(i), f"i{i}") for i in range(5)]
    for r in inputs[:mask_inputs]:
        _select(db, r, mask_enabled=True)
    db.commit()
    return inputs


def _transport(descs: list[tuple[str, str, str]] | None = None) -> Any:
    registry_map: dict[Any, list[Any]] = {}
    if descs:
        entries: list[SimpleNamespace] = [
            SimpleNamespace(variable_name=name, documented_name=name, unit="", data_type="ASCII",
                            note=None, read_interval=None, description_source=src)
            for name, src, _rt in descs
        ]
        registry_map = {Registry_Type[descs[0][2].upper()]: entries}
    return SimpleNamespace(
        synthetic_fields_metadata=SYNTHETIC_FIELDS, protocol_version=PROTOCOL, registry_map=registry_map,
    )


# ---------------------------------------------------------------- counts


def test_unselected_synthetic_fields_do_not_count_as_selected(db: Session) -> None:
    """The reported scenario: Holding M:0, Input M:2 -> Holding Selected must be 0, not 4."""
    _seed(db, mask_inputs=2)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["by_registry_type"]["holding"]["chosen"] == 0
    assert s["by_registry_type"]["input"]["chosen"] == 2
    assert s["chosen_count"] == 2
    assert s["register_map_chosen"] == 2
    # ...but they are still *available*.
    assert s["total_available"] == 3 + 5 + 4
    assert s["by_registry_type"]["holding"]["total"] == 3 + 4


def test_no_selections_anywhere_does_not_count_synthetic(db: Session) -> None:
    _seed(db)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["chosen_count"] == 8  # real registers only (all forwarded), no synthetic
    assert s["by_registry_type"]["holding"]["chosen"] == 3


def test_mask_selected_synthetic_counts_once_without_double_counting(db: Session) -> None:
    _seed(db, mask_inputs=2)
    _add_synthetic(db, "model", mask_enabled=True)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["by_registry_type"]["holding"]["chosen"] == 1
    assert s["chosen_count"] == 3
    # A materialized synthetic row must not inflate the totals either.
    assert s["total_available"] == 3 + 5 + 4
    assert s["by_registry_type"]["holding"]["total"] == 3 + 4


def test_only_a_synthetic_selection_switches_to_whitelist_mode(db: Session) -> None:
    """A lone chosen synthetic is written to the mask file, which makes the mask non-empty."""
    _seed(db)
    _add_synthetic(db, "model", mask_enabled=True)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["chosen_count"] == 1
    assert s["by_registry_type"]["input"]["chosen"] == 0


def test_screen_on_synthetic_is_inert_for_counts(db: Session) -> None:
    _seed(db)
    _add_synthetic(db, "model", screen_enabled=True)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["chosen_count"] == 8  # still "no selections": nothing was written anywhere


def test_json_desc_counts_only_when_its_code_register_is_chosen(db: Session) -> None:
    inputs: list[ProtocolRegister] = _seed(db, mask_inputs=0)
    _select(db, inputs[0], mask_enabled=True)  # i0 chosen via mask; i1 is not
    db.commit()
    t = _transport(descs=[("i0_desc", "i0", "input"), ("i1_desc", "i1", "input")])
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=t)
    assert s["by_registry_type"]["input"]["chosen"] == 1 + 1  # i0 + i0_desc, not i1_desc
    assert s["chosen_count"] == 2
    assert s["total_available"] == 3 + 5 + 4 + 2


# ---------------------------------------------------------------- forwarding


def _member(mask: list[str], registry_names: list[str]) -> Any:
    return SimpleNamespace(
        protocolSettings=SimpleNamespace(variable_mask=mask),
        synthetic_field_names=frozenset(f[0] for f in SYNTHETIC_FIELDS),
        registry_map={"input": [SimpleNamespace(variable_name=n) for n in registry_names]},
    )


DATA: dict[str, Any] = {"i0": 1, "i1": 2, "model": "18kPV", "firmware_version": "FAAB-2727",
                        "hardware_kind": "inverter", "is_gridboss": 0}


def _filter(member: Any, data: dict[str, Any]) -> dict[str, Any]:
    return Protocol_Gateway._filter_for_member(None, data, member)  # type: ignore[arg-type]


def test_mask_without_synthetic_withholds_them() -> None:
    assert _filter(_member(["i0"], ["i0"]), DATA) == {"i0": 1}


def test_mask_naming_a_synthetic_forwards_only_that_one() -> None:
    assert _filter(_member(["i0", "model"], ["i0"]), DATA) == {"i0": 1, "model": "18kPV"}


def test_no_mask_registry_fallback_withholds_synthetic() -> None:
    assert _filter(_member([], ["i0", "i1"]), DATA) == {"i0": 1, "i1": 2}


def test_no_mask_no_registry_withholds_synthetic_but_forwards_the_rest() -> None:
    assert _filter(_member([], []), DATA) == {"i0": 1, "i1": 2}


# ---------------------------------------------------------------- files


def test_mask_file_gets_chosen_synthetic_and_screen_file_never_does(
    db: Session, tmp_path: Path
) -> None:
    inputs: list[ProtocolRegister] = _seed(db)
    db.add(Setting(section=f"transport.{DEVICE}", key="protocol_version", value_disk=PROTOCOL, value_staged=PROTOCOL))
    _select(db, inputs[0], mask_enabled=True)
    _select(db, inputs[1], screen_enabled=True)
    _add_synthetic(db, "model", mask_enabled=True)
    _add_synthetic(db, "hardware_kind", screen_enabled=True)   # inert
    _add_synthetic(db, "is_gridboss")                          # materialized, then unchecked
    db.commit()

    _write_mask_screen_files(db, tmp_path)

    mask: list[str] = (tmp_path / "config" / f"variable_mask_{DEVICE}.txt").read_text().split()
    screen: list[str] = (tmp_path / "config" / f"variable_screen_{DEVICE}.txt").read_text().split()
    assert mask == ["i0", "model"]
    assert screen == ["i1"]


# ------------------------------------------------- bridge schema / stale columns


def _stub_transport(mask: list[str] | None) -> Any:
    return SimpleNamespace(
        protocolSettings=None if mask is None else SimpleNamespace(variable_mask=mask),
        synthetic_fields_metadata=SYNTHETIC_FIELDS,
        registry_map={"input": [SimpleNamespace(variable_name="i0")]},
    )


def _selected(stub: Any) -> list[Any]:
    from classes.transports.transport_base import transport_base
    return transport_base.selected_synthetic_fields_metadata.fget(stub)  # type: ignore[attr-defined]


def test_selected_synthetic_metadata_follows_the_mask() -> None:
    assert [f[0] for f in _selected(_stub_transport(["i0", "Model"]))] == ["model"]
    assert _selected(_stub_transport(["i0"])) == []
    assert _selected(_stub_transport([])) == []
    assert _selected(_stub_transport(None)) == []


def test_unchosen_synthetic_columns_are_not_expected_so_they_flag_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from classes.WebServer.services import bridge_service

    class _T(SimpleNamespace):
        @property
        def selected_synthetic_fields_metadata(self) -> list[Any]:
            return _selected(self)

    def expected(mask: list[str]) -> set[str] | None:
        t = _T(**vars(_stub_transport(mask)))

        def _fake_live_transport(*_args: object, **_kwargs: object) -> _T:
            return t

        monkeypatch.setattr(bridge_service, "_get_live_transport", _fake_live_transport)
        return bridge_service._active_metric_names_for_protocol(None, PROTOCOL)

    # No synthetic chosen: model/firmware_version/... are NOT expected => their columns flag stale.
    assert expected(["i0"]) == {"i0"}
    # Chosen one is expected again => not flagged.
    assert expected(["i0", "model"]) == {"i0", "model"}
