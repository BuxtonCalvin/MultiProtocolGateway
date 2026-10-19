# Description: Synthetic fields (EG4 model/firmware_version/...) follow the
#   same mask/screen rule as every other metric: with a mask they are counted
#   and forwarded only if M-selected; otherwise they are counted and forwarded
#   unless S-screened.
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

from classes.protocol_settings import (
    SYNTHETIC_FILTER_PREFIX,
    Registry_Type,
    protocol_settings,
)
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
    row: ProtocolRegister = ProtocolRegister(
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


def test_no_selections_anywhere_counts_synthetic_like_every_other_metric(db: Session) -> None:
    _seed(db)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["chosen_count"] == 8 + 4  # everything is forwarded, synthetic included
    assert s["by_registry_type"]["holding"]["chosen"] == 3 + 4


def test_screen_mode_counts_unscreened_synthetic_as_selected(db: Session) -> None:
    """The LL-S report: screening real metrics must not drop the synthetic ones."""
    _seed(db)
    rows: list[ProtocolRegister] = db.query(ProtocolRegister).filter_by(registry_type="holding").all()
    _select(db, rows[0], screen_enabled=True)
    db.commit()
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    # holding: 3 - 1 screened = 2 real + 4 synthetic; input tab has no entries of its own -> 0
    assert s["by_registry_type"]["holding"]["chosen"] == 2 + 4
    assert s["chosen_count"] == 2 + 4


def test_screened_synthetic_is_not_counted(db: Session) -> None:
    _seed(db)
    _add_synthetic(db, "model", screen_enabled=True)
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["by_registry_type"]["holding"]["chosen"] == 3 + 3
    assert s["chosen_count"] == 8 + 3
    assert s["total_available"] == 8 + 4


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


def test_json_desc_counts_only_when_its_code_register_is_chosen(db: Session) -> None:
    inputs: list[ProtocolRegister] = _seed(db, mask_inputs=0)
    _select(db, inputs[0], mask_enabled=True)  # i0 chosen via mask; i1 is not
    db.commit()
    t: Any = _transport(descs=[("i0_desc", "i0", "input"), ("i1_desc", "i1", "input")])
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=t)
    assert s["by_registry_type"]["input"]["chosen"] == 1 + 1  # i0 + i0_desc, not i1_desc
    assert s["chosen_count"] == 2
    assert s["total_available"] == 3 + 5 + 4 + 2


# ---------------------------------------------------------------- forwarding


def _member(mask: list[str], registry_names: list[str], screen: set[str] | None = None) -> Any:
    ps: ProtocolSettingsStub = ProtocolSettingsStub(mask, screen or set())
    return SimpleNamespace(
        protocolSettings=ps,
        synthetic_field_names=frozenset(f[0] for f in SYNTHETIC_FIELDS),
        registry_map={"input": [SimpleNamespace(variable_name=n) for n in registry_names]},
    )


class ProtocolSettingsStub:
    """Just the bits of protocol_settings the filter touches, using the real rule."""

    def __init__(self, mask: list[str], screen: set[str]) -> None:
        self.variable_mask = mask
        self.synthetic_screen = screen

    synthetic_forwarded = protocol_settings.synthetic_forwarded


DATA: dict[str, Any] = {"i0": 1, "i1": 2, "model": "18kPV", "firmware_version": "FAAB-2727",
                        "hardware_kind": "inverter", "is_gridboss": 0}


def _filter(member: Any, data: dict[str, Any]) -> dict[str, Any]:
    return Protocol_Gateway._filter_for_member(None, data, member)  # type: ignore[arg-type]


def test_mask_without_synthetic_withholds_them() -> None:
    assert _filter(_member(["i0"], ["i0"]), DATA) == {"i0": 1}


def test_mask_naming_a_synthetic_forwards_only_that_one() -> None:
    assert _filter(_member(["i0", "model"], ["i0"]), DATA) == {"i0": 1, "model": "18kPV"}


def test_screen_mode_forwards_unscreened_synthetic() -> None:
    """No mask: synthetic fields flow like every other metric not in the screen file."""
    assert _filter(_member([], ["i0", "i1"]), DATA) == DATA


def test_screen_mode_withholds_a_screened_synthetic() -> None:
    out: dict[str, Any] = _filter(_member([], ["i0", "i1"], screen={"model"}), DATA)
    assert "model" not in out
    assert {"firmware_version", "hardware_kind", "is_gridboss", "i0", "i1"} <= out.keys()


def test_no_mask_no_registry_still_honours_synthetic_screen() -> None:
    out: dict[str, Any] = _filter(_member([], [], screen={"model"}), DATA)
    assert "model" not in out and "firmware_version" in out


def test_screened_synthetic_follows_the_helper_on_the_transport() -> None:
    from classes.transports.transport_base import transport_base
    ps: ProtocolSettingsStub = ProtocolSettingsStub([], {"model"})
    stub: SimpleNamespace = SimpleNamespace(protocolSettings=ps, synthetic_fields_metadata=SYNTHETIC_FIELDS)
    got: list[str] = [f[0] for f in transport_base.selected_synthetic_fields_metadata.fget(stub)]  # type: ignore[attr-defined]
    assert got == ["firmware_version", "hardware_kind", "is_gridboss"]


# ---------------------------------------------------------------- files


def test_mask_gets_plain_synthetic_names_and_screen_gets_prefixed_ones(
    db: Session, tmp_path: Path
) -> None:
    inputs: list[ProtocolRegister] = _seed(db)
    db.add(Setting(section=f"transport.{DEVICE}", key="protocol_version", value_disk=PROTOCOL, value_staged=PROTOCOL))
    _select(db, inputs[0], mask_enabled=True)
    _select(db, inputs[1], screen_enabled=True)
    _add_synthetic(db, "model", mask_enabled=True)
    _add_synthetic(db, "hardware_kind", screen_enabled=True)
    _add_synthetic(db, "is_gridboss")                          # materialized, then unchecked
    db.commit()

    _write_mask_screen_files(db, tmp_path)

    mask: list[str] = (tmp_path / "config" / f"variable_mask_{DEVICE}.txt").read_text().split()
    screen: list[str] = (tmp_path / "config" / f"variable_screen_{DEVICE}.txt").read_text().split()
    assert mask == ["i0", "model"]
    assert screen == ["i1", f"{SYNTHETIC_FILTER_PREFIX}hardware_kind"]


def test_screen_file_with_only_synthetic_entries_does_not_exclude_real_registers(tmp_path: Path) -> None:
    """The loader must not mistake "synthetic:<name>" for a real name (would clear every registry type)."""
    (tmp_path / "variable_screen_dev.txt").write_text("synthetic:model\n")
    ps: protocol_settings = protocol_settings.__new__(protocol_settings)
    ps.protocol = PROTOCOL
    ps._log = SimpleNamespace(debug=lambda *a, **k: None, error=lambda *a, **k: None)  # type: ignore[attr-defined]
    ps.find_protocol_file = lambda name, _d: str(tmp_path / name)  # type: ignore[method-assign]
    raw: list[str] = ps._load_filter_file("variable_screen_dev.txt")
    assert raw == ["synthetic:model"]
    # Same split protocol_settings.__init__ performs:
    assert [e for e in raw if not e.startswith(SYNTHETIC_FILTER_PREFIX)] == []


# ------------------------------------------------- bridge schema / stale columns


def _stub_transport(mask: list[str] | None, screen: set[str] | None = None) -> Any:
    return SimpleNamespace(
        protocolSettings=None if mask is None else ProtocolSettingsStub(mask, screen or set()),
        synthetic_fields_metadata=SYNTHETIC_FIELDS,
        registry_map={"input": [SimpleNamespace(variable_name="i0")]},
    )


def _selected(stub: Any) -> list[Any]:
    from classes.transports.transport_base import transport_base
    return transport_base.selected_synthetic_fields_metadata.fget(stub)  # type: ignore[attr-defined]


def test_selected_synthetic_metadata_follows_the_mask_or_screen() -> None:
    assert [f[0] for f in _selected(_stub_transport(["i0", "model"]))] == ["model"]   # mask: only M-selected
    assert _selected(_stub_transport(["i0"])) == []                                   # mask without synthetic
    assert len(_selected(_stub_transport([]))) == 4                                   # nothing selected: all
    assert len(_selected(_stub_transport(None))) == 4                                 # no settings: all
    assert [f[0] for f in _selected(_stub_transport([], {"model"}))] == [
        "firmware_version", "hardware_kind", "is_gridboss"]                           # screen: all but S-screened


def test_unchosen_synthetic_columns_are_not_expected_so_they_flag_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from classes.WebServer.services import bridge_service

    class _T(SimpleNamespace):
        @property
        def selected_synthetic_fields_metadata(self) -> list[Any]:
            return _selected(self)

    def expected(mask: list[str]) -> set[str] | None:
        t: _T = _T(**vars(_stub_transport(mask)))

        def _fake_live_transport(*_args: object, **_kwargs: object) -> _T:
            return t

        monkeypatch.setattr(bridge_service, "_get_live_transport", _fake_live_transport)
        return bridge_service._active_metric_names_for_protocol(None, PROTOCOL)

    # Mask present without synthetic: their columns are NOT expected => flag stale.
    assert expected(["i0"]) == {"i0"}
    # Mask naming one: that one is expected again => not flagged.
    assert expected(["i0", "model"]) == {"i0", "model"}
    # No mask (screen mode / nothing selected): every synthetic field is still forwarded => expected.
    assert expected([]) == {"i0", "model", "firmware_version", "hardware_kind", "is_gridboss"}


# ------------------------------------------------- mask always wins over screen


def test_mask_in_use_reflects_any_mask_selection(db: Session) -> None:
    from classes.WebServer.services.protocol_service import device_mask_in_use
    inputs: list[ProtocolRegister] = _seed(db)
    assert device_mask_in_use(db, DEVICE) is False
    _select(db, inputs[0], screen_enabled=True)
    assert device_mask_in_use(db, DEVICE) is False
    _add_synthetic(db, "model", mask_enabled=True)   # a synthetic M counts as a mask too
    assert device_mask_in_use(db, DEVICE) is True
    assert device_mask_in_use(db, "OtherDevice") is False


def test_summary_ignores_screens_while_a_mask_exists(db: Session) -> None:
    """Holding has S:1, Input has M:2: the mask wins, so Holding contributes nothing."""
    _seed(db, mask_inputs=2)
    holding: ProtocolRegister | None = db.query(ProtocolRegister).filter_by(registry_type="holding").first()
    assert holding is not None
    _select(db, holding, screen_enabled=True)
    db.commit()
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["mask_in_use"] is True
    assert s["by_registry_type"]["holding"]["chosen"] == 0   # not 3 - 1 + 4
    assert s["by_registry_type"]["input"]["chosen"] == 2
    assert s["chosen_count"] == 2


def test_summary_reports_no_mask_in_use_for_screen_only(db: Session) -> None:
    _seed(db)
    holding: ProtocolRegister | None = db.query(ProtocolRegister).filter_by(registry_type="holding").first()
    assert holding is not None
    _select(db, holding, screen_enabled=True)
    db.commit()
    s: Any = get_device_metric_summary(db, PROTOCOL, DEVICE, transport=_transport())
    assert s["mask_in_use"] is False
    assert s["by_registry_type"]["holding"]["chosen"] == 2 + 4


def test_screen_toggle_is_refused_while_a_mask_exists_but_unticking_is_allowed(db: Session) -> None:
    from classes.WebServer.services.protocol_service import toggle_register_field
    inputs: list[ProtocolRegister] = _seed(db, mask_inputs=1)
    other: ProtocolRegister = inputs[3]
    assert toggle_register_field(db, other.id, "screen_enabled", True, DEVICE) is None  # noqa: FBT003
    # Mask toggles themselves are never blocked, and S may be cleared.
    assert toggle_register_field(db, other.id, "mask_enabled", True, DEVICE) is not None  # noqa: FBT003
    assert toggle_register_field(db, other.id, "screen_enabled", False, DEVICE) is not None  # noqa: FBT003
    # Once the masks are gone, screening works again.
    for r in inputs:
        toggle_register_field(db, r.id, "mask_enabled", False, DEVICE)  # noqa: FBT003
    assert toggle_register_field(db, other.id, "screen_enabled", True, DEVICE) is not None  # noqa: FBT003


def _ll_s_holding(mask: list[str], screen: list[str]) -> list[str]:
    """Run the real loader over the real EG4 LL-S holding map with the given filters."""
    ps: protocol_settings = protocol_settings("eg4_ll_s")
    first: str
    second: str
    first, second = (e.variable_name for e in ps.get_registry_map(Registry_Type.HOLDING)[:2])
    ps.variable_mask = [n.replace("{0}", first).replace("{1}", second) for n in mask]
    ps.variable_screen = [n.replace("{0}", first).replace("{1}", second) for n in screen]
    path: str | None = ps.find_protocol_file("eg4_ll_s.holding_registry_map.csv", "")
    assert path
    return [e.variable_name for e in ps.load__registry(path, Registry_Type.HOLDING)]


def test_loader_mask_wins_when_a_metric_is_both_masked_and_screened() -> None:
    assert _ll_s_holding(mask=["{0}"], screen=["{0}"]) == [protocol_settings("eg4_ll_s").get_registry_map(Registry_Type.HOLDING)[0].variable_name]


def test_loader_ignores_screen_entirely_when_a_mask_exists() -> None:
    kept: list[str] = _ll_s_holding(mask=["{0}"], screen=["{1}"])
    assert len(kept) == 1


def test_loader_screen_only_drops_just_the_screened_metric() -> None:
    full: int = len(protocol_settings("eg4_ll_s").get_registry_map(Registry_Type.HOLDING))
    assert len(_ll_s_holding(mask=[], screen=["{0}"])) == full - 1
