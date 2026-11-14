# Description: Decode tests for the SRNE v1.7 time registers (current time, power-on time, last equalizing charge).
# File: test_srne_v17_time_registers.py
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

"""
SRNE energy-storage inverter protocol V1.92 documents a time stamp as three 16-bit registers:

    first  register: high byte = year (0-99, years since 2000), low byte = month
    second register: high byte = day,                            low byte = hour
    third  register: high byte = minute,                         low byte = second

0x020C-0x020E is the current time; F040-F042 (power-on time) and F043-F045 (last equalizing charge
completion time) are 3 registers each (the old map treated them as 2-register "UINT48" values) and use the
same format.
"""

from __future__ import annotations

from classes.protocol_settings import Registry_Type, protocol_settings


def _decode(registers: dict[int, int]) -> dict[str, int | float | str]:
    settings: protocol_settings = protocol_settings("srne_v1.7")
    return settings.process_registery(registers, settings.get_registry_map(Registry_Type.HOLDING))


def _stamp(year: int, month: int, day: int, hour: int, minute: int, second: int) -> tuple[int, int, int]:
    return (year << 8) | month, (day << 8) | hour, (minute << 8) | second


def test_current_time_decodes_all_six_fields() -> None:
    first, second, third = _stamp(24, 11, 12, 13, 45, 30)

    info = _decode({0x20C: first, 0x20D: second, 0x20E: third})

    assert [info[f"current_time_{name}"] for name in ("year", "month", "day", "hour", "minute", "second")] == [24, 11, 12, 13, 45, 30]


def test_current_time_does_not_overwrite_the_registers_it_used_to_overlap() -> None:
    """Day..second used to be mapped onto 0x21D / 0x21E, which are the inverter DC component and mains charge current."""
    first, second, third = _stamp(24, 11, 12, 13, 45, 30)

    info = _decode({0x20C: first, 0x20D: second, 0x20E: third, 0x21D: 1234, 0x21E: 567})

    assert info["inverter_dc_component"] == 1234
    assert info["mains_charge_current"] == 56.7


def test_power_on_time_is_three_registers() -> None:
    first, second, third = _stamp(23, 2, 28, 9, 5, 59)

    info = _decode({0xF040: first, 0xF041: second, 0xF042: third})

    assert [info[f"power_on_time_{name}"] for name in ("year", "month", "day", "hour", "minute", "second")] == [23, 2, 28, 9, 5, 59]


def test_last_equalizing_charge_time_starts_at_f043_and_does_not_touch_f046() -> None:
    first, second, third = _stamp(24, 10, 3, 17, 21, 8)

    info = _decode({0xF043: first, 0xF044: second, 0xF045: third, 0xF046: 7})

    prefix = "last_equalizing_charge_completion_time_"
    assert [info[prefix + name] for name in ("year", "month", "day", "hour", "minute", "second")] == [24, 10, 3, 17, 21, 8]
