"""EG4-specific device/serial-number/battery metadata reconstruction.

This module exists because EG4's registry maps (holding/input CSVs under
protocols/eg4/*) don't follow the generic conventions the rest of this
codebase's transport-agnostic read paths (e.g. modbus_base.read_serial_number)
rely on:

- The serial number is split across five registers as ten single-character
  'SN_0_...'..'SN_9_...' fields rather than one 'Serial_Number'/'Serial No N'
  field, and even where a combined 'Serial_Number' field IS declared (some
  EG4 input maps), protocol_settings' ASCII decoder is single-register-only,
  so that field silently truncates to 2 characters.
- Device type, model, and firmware version live at fixed register addresses
  (19, 0-1, 7-10) that aren't meaningfully named in the CSVs at all.
- The same "eg4" protocol family also covers EG4 lithium batteries (e.g.
  LL-S), which use a holding-register map that has nothing to do with the
  inverter one — register 2 is half a serial number on an 18kPV, but
  cell_01_voltage on an LL-S. Callers must detect which kind of hardware
  they're talking to before touching any of the fixed-address registers
  above.

Everything here is deliberately kept out of modbus_base.py, which is a
generic Modbus transport base with no protocol-specific knowledge otherwise;
this module is called into from there, not the other way around.

Usage from a transport (e.g. modbus_base):

    from ..eg4_metadata import is_eg4_protocol, read_eg4_serial_number, read_eg4_device_metadata

    if is_eg4_protocol(self._protocol.protocol):
        sn = read_eg4_serial_number(self)
        metadata = read_eg4_device_metadata(self)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, Protocol

from .protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

_log: logging.Logger = logging.getLogger(__name__)

class EG4MetadataTransport(Protocol):
    """Structural type for the subset of modbus_base this module needs.

    Defined here (rather than importing modbus_base directly) to avoid a
    circular import — modbus_base is the one that imports this module — and
    so this module doesn't need to hard-code modbus_base's package path.
    Any object satisfying this shape (in practice, a modbus_base instance)
    can be passed to the functions below.
    """
    transport_name: str
    modbus_delay: float
    send_holding_register: bool
    send_input_register: bool
    device_serial_number: str
    eg4_hardware_kind_cache: str | None

    @property
    def proto(self) -> protocol_settings: ...

    def read_variable(self, variable_name: str, registry_type: Registry_Type) -> int | float | str | None: ...

    def read_modbus_registers(
        self,
        ranges: list[tuple[int, int]] | None = None,
        start: int = 0,
        end: int | None = None,
        batch_size: int | None = None,
        registry_type: Registry_Type = Registry_Type.INPUT,
        entries: list[registry_map_entry] | None = None,
    ) -> dict[int, int]: ...


# ----------------------------------------------------------------------------
# Device type codes
# ----------------------------------------------------------------------------
# Read directly from EG4 holding register 19 wherever this module needs the
# device type code (see _read_inverter_metadata()) — a plain, unsigned
# ushort (0-65535; observed values are all comfortably in range, e.g. 2092
# for the 18kPV/12kPV family — see EG4_DEVICE_TYPE_CODE_* below). This
# addressing convention (and the codes themselves) is consistent across
# EG4/LuxPower firmware families. It's read via transport.read_modbus_registers()
# with an explicit start/end rather than through a named registry_map entry,
# which — deliberately — bypasses variable_mask/variable_screen filtering
# entirely: those only constrain the registry_map-driven per-cycle read
# (calculate_registry_ranges), not an explicit start/end range read like this
# one, so this value is available regardless of what the transport's mask
# includes.
#
# This value is used internally (see _read_inverter_metadata()) to compute
# model/is_gridboss, but is deliberately never exposed as its own synthetic
# field: on variants whose CSV names register 19 (e.g. as 'device_type_code'
# or 'Device_Type_Code'), it already decodes under that name through the
# normal registry_map path, and re-declaring it here would just produce a
# duplicate "real" register-19 row plus a "synthetic" one with the same
# name/value in the webUI. On variants that leave register 19 unlabeled, the
# code is still read (above) and still used to derive model/is_gridboss —
# it's just not separately surfaced as a field of its own either way.
EG4_DEVICE_TYPE_CODE_GRIDBOSS: int = 50
EG4_DEVICE_TYPE_CODE_OFFGRID: int = 54       # 12000XP, 6000XP
EG4_DEVICE_TYPE_CODE_OFFGRID_ALT: int = 38   # 6000XP variant (field-reported)
EG4_DEVICE_TYPE_CODE_HYBRID: int = 2092      # 18kPV, 12kPV
EG4_DEVICE_TYPE_CODE_FLEXBOSS: int = 10284   # FlexBOSS21, FlexBOSS18
EG4_DEVICE_TYPE_CODE_LXP_EU: int = 12        # LXP-EU 12K
EG4_DEVICE_TYPE_CODE_LXP_BR: int = 44        # LXP-LB-BR 10K

# Coarse (model_name, is_gridboss) per device type code, used as a fallback
# when the finer-grained EG4ModelInfo.get_model_name() below doesn't
# recognize the code.
EG4_DEVICE_TYPE_MODEL_MAP: dict[int, tuple[str, bool]] = {
    # device_type_code: (model_name, is_gridboss)
    EG4_DEVICE_TYPE_CODE_GRIDBOSS: ("GridBOSS", True),
    EG4_DEVICE_TYPE_CODE_OFFGRID: ("12000XP", False),
    EG4_DEVICE_TYPE_CODE_OFFGRID_ALT: ("6000XP", False),
    EG4_DEVICE_TYPE_CODE_HYBRID: ("18kPV", False),
    EG4_DEVICE_TYPE_CODE_FLEXBOSS: ("FlexBOSS21", False),
    EG4_DEVICE_TYPE_CODE_LXP_EU: ("LXP-EU 12K", False),
    EG4_DEVICE_TYPE_CODE_LXP_BR: ("LXP-LB-BR 10K", False),
}

# ----------------------------------------------------------------------------
# Text byte order (serial number / firmware prefix)
# ----------------------------------------------------------------------------
# EG4 registers pack two ASCII characters per 16-bit register. Two reference
# implementations were compared while building this and they DISAGREE on
# which byte comes first:
#   - one decodes high byte -> first character, low byte -> second
#     (standard Modbus/big-endian text convention; also what the EG4 18kPV
#     holding-register datasheet note works out to: SN[0]=0x41('A') is the
#     documented *first* character of the example serial "AB12345678", and
#     that only reproduces correctly if the high byte is read first)
#   - the other decodes low byte -> first character, high byte -> second
# Absent a live device to test against, this module originally followed the
# first (high-byte-first) convention, since it's the one independently
# corroborated by the datasheet's own worked example -- but live hardware
# testing showed serial numbers coming out with each register's two
# characters swapped (e.g. 'batteryserialnumber_1' decoded as
# 'aBttre_yDI0_1', which un-swaps cleanly to 'Battery_ID_01'), so this is
# now set to the low-byte-first convention instead. If serial numbers or
# firmware strings come out reversed/garbled again on different hardware,
# flip this constant back.
EG4_TEXT_HIGH_BYTE_FIRST: bool = False

# ----------------------------------------------------------------------------
# Serial number register addresses
# ----------------------------------------------------------------------------
# Fixed addresses, read the same way as the device type code register (19)
# above: via an explicit start/end transport.read_modbus_registers() call
# rather than through the registry_map-driven per-cycle path. This
# deliberately bypasses variable_mask/variable_screen filtering entirely.
#
# That bypass matters here specifically because the ten 'SN_0_...'..
# 'SN_9_...' fields these registers pack are exactly as maskable/screenable
# as any other named field — protocol_settings.load__registry deletes
# masked-out/screened-out rows from the loaded registry_map in place (see
# its "Apply variable mask"/"Apply variable screen" blocks), so a transport
# whose mask doesn't happen to include the SN_* fields (or whose screen
# excludes them) ends up with a registry_map that no longer has those
# entries at all. Deriving the registers to read by scanning that map (the
# previous approach here) would then silently find nothing, and this
# function would return "" even though the underlying registers are
# perfectly readable — matching the previously-reported "serial number does
# not decode at all" symptom. Reading these fixed addresses directly avoids
# that dependency altogether, same as register 19.
#
# 18kPV holding map: 5 registers starting at 2 (SN_0_Year..SN_9_batch_number,
# two characters per register). 18kPV/GridBOSS input map: 5 registers
# starting at 115, same packing.
EG4_SERIAL_NUMBER_HOLDING_REGISTERS: tuple[int, int] = (2, 6)
EG4_SERIAL_NUMBER_INPUT_REGISTERS: tuple[int, int] = (115, 119)


def _decode_register_chars(value: int) -> tuple[int, int]:
    """Split a 16-bit register into (first_byte, second_byte) for EG4 2-char
    text packing, honoring EG4_TEXT_HIGH_BYTE_FIRST."""
    high_byte: int = (value >> 8) & 0xFF
    low_byte: int = value & 0xFF
    return (high_byte, low_byte) if EG4_TEXT_HIGH_BYTE_FIRST else (low_byte, high_byte)


@dataclass
class EG4ModelInfo:
    """Decodes the HOLD_MODEL bitfield (holding registers 0-1) into a
    power-rating code, which combined with the device type code (register 19)
    gives the specific model variant within a family (e.g. FlexBOSS21 vs.
    FlexBOSS18, 12kPV vs. 18kPV) rather than just the family name.

    Bit layout ported from a reference implementation that reports it as
    verified against 13 devices across all EG4/LuxPower families (18kPV,
    12kPV, FlexBOSS21, FlexBOSS18, SNA 12K-US, LXP-EU, LXP-US, GridBOSS) — not
    independently re-verified here.
    """
    raw_value: int = 0
    power_rating: int = 0

    @classmethod
    def from_registers(cls, reg0: int, reg1: int) -> "EG4ModelInfo":
        """reg0: holding register 0 (HOLD_MODEL low word). reg1: holding register 1 (HOLD_MODEL high word)."""
        raw_value: int = ((reg1 & 0xFFFF) << 16) | (reg0 & 0xFFFF)
        # Base rating from bits 5-7 of the low byte of reg0.
        power_rating: int = ((reg0 & 0xFF) >> 5) & 0x7
        # Bit 8 of reg1 adds 8 (FlexBOSS family offset).
        if reg1 & 0x100:
            power_rating += 8
        return cls(raw_value=raw_value, power_rating=power_rating)

    def get_power_rating_kw(self, device_type_code: int) -> int:
        if device_type_code == EG4_DEVICE_TYPE_CODE_HYBRID:
            return {2: 12, 6: 18}.get(self.power_rating, 0)
        if device_type_code == EG4_DEVICE_TYPE_CODE_FLEXBOSS:
            return {8: 21, 9: 18}.get(self.power_rating, 0)
        if device_type_code == EG4_DEVICE_TYPE_CODE_LXP_BR:
            return {4: 10}.get(self.power_rating, 0)
        return 0

    def get_model_name(self, device_type_code: int) -> str | None:
        """Returns None (rather than an "Unknown-<code>" placeholder) when the
        device type code isn't recognized, so callers can fall back to
        EG4_DEVICE_TYPE_MODEL_MAP's coarser family-level name instead."""
        kw: int = self.get_power_rating_kw(device_type_code)

        if device_type_code == EG4_DEVICE_TYPE_CODE_HYBRID:
            return {2: "12kPV", 6: "18kPV"}.get(self.power_rating, f"PV-{kw}K" if kw else None)

        if device_type_code == EG4_DEVICE_TYPE_CODE_FLEXBOSS:
            return {8: "FlexBOSS21", 9: "FlexBOSS18"}.get(self.power_rating, f"FlexBOSS{kw}" if kw else None)

        if device_type_code == EG4_DEVICE_TYPE_CODE_LXP_BR:
            return f"LXP-LB-{kw}K" if kw else None

        return None

# ----------------------------------------------------------------------------
# Hardware kind detection
# ----------------------------------------------------------------------------
# EG4 lithium batteries (e.g. LL-S) and EG4 inverters both speak Modbus under
# the "eg4" protocol family, but use entirely different holding-register maps
# addressed the same way, so device type / model / firmware register
# addresses collide with unrelated battery data. Detect which kind of
# hardware is actually connected before reading protocol-specific metadata,
# using cell_01_voltage (present only on battery maps) as the signal: a real
# lithium cell reads roughly 2.0-4.0V.
EG4_CELL_VOLTAGE_MIN: float = 2.0
EG4_CELL_VOLTAGE_MAX: float = 4.0


@dataclass
class EG4DeviceMetadata:
    """Metadata discovered from an EG4 *inverter's* registers, beyond just the
    serial number. Mirrors the shape of the reference discovery module's
    DiscoveredDevice, but sourced through the calling transport's own
    register reads rather than a separate cloud/API client. See
    EG4BatteryMetadata for the battery-hardware equivalent.
    """
    hardware_kind: str  # "inverter"
    serial: str
    model: str
    device_type_code: int | None
    is_gridboss: bool
    firmware_version: str
    parallel_number: int = 0
    parallel_master_slave: int = 0
    parallel_phase: int = 0


@dataclass
class EG4BatteryMetadata:
    """Metadata discovered from an EG4 lithium battery's (e.g. LL-S) holding
    registers. The battery registry map does declare 'model', 'fw_version',
    and 'serial_no' fields (holding registers 105-116, 117-119, 120-127
    respectively, each spanning several registers — 24, 6, and 16 bytes), but
    per hardware documentation those are only populated when queried over the
    battery's CANbus interface — over Modbus they are not readable, so this
    module does not attempt them and always leaves serial/model/firmware_version
    as None here (see `metadata_note`).

    These fields are kept (rather than dropped from the dataclass) because
    they're a real part of the battery's declared register map and, being
    multi-register ASCII spans, would need the same kind of proper multi-
    register decoding this module already does elsewhere (see
    read_eg4_serial_number(), _resolve_eg4_firmware_version(),
    _resolve_eg4_battery_serial()) if EG4 ever exposes them over Modbus
    directly — at which point populating them is a small, contained change
    rather than a new dataclass. For CANbus-sourced battery data available
    *today*, see the batteryserialnumber_<N> correction on the inverter's own
    input registers instead (ensure_eg4_synthetic_entries()) — the inverter's
    CANbus link to the battery bank already surfaces some of this.
    """
    hardware_kind: str  # "battery"
    serial: str | None
    model: str | None
    firmware_version: str | None
    cell_count: int
    pack_voltage: float | None
    soc: float | None
    soh: float | None
    metadata_note: str = (
        "serial/model/firmware are only readable over this battery's CANbus "
        "interface, not Modbus — not attempted here."
    )


def is_eg4_protocol(protocol_name: str | None) -> bool:
    """Whether a protocol name (e.g. transport.proto.protocol) belongs to
    the EG4 family this module handles."""
    return bool(protocol_name) and protocol_name.lower().startswith("eg4")


def detect_eg4_hardware_kind(transport: EG4MetadataTransport) -> str:
    """Determine whether an "eg4"-protocol transport is talking to an
    inverter/GridBOSS or a lithium battery (e.g. LL-S). Both live under the
    "eg4" protocol family and both are addressed via holding registers
    starting at 0, but the two register maps have nothing to do with each
    other (e.g. holding register 2 is half of the serial number on an 18kPV,
    but is cell_01_voltage on an LL-S battery), so callers must know which
    one they're dealing with before touching any of the fixed-address
    metadata registers (19, 0-1, 7-10, 113).

    Detection reads 'cell_01_voltage', which only exists on battery registry
    maps and — critically — is checked against a plausible real-world range
    (a lithium cell reads ~2.0-4.0V) rather than just checked for presence,
    so a garbage/zero read (disconnected battery, wrong register map,
    transient bus error) doesn't get misclassified as "this is a battery".

    Returns "battery", "inverter", or "unknown" (holding registers not
    readable / cell_01_voltage absent and nothing else to go on — treated as
    "not a battery" by callers, i.e. falls through to inverter logic).
    Cached on transport.eg4_hardware_kind_cache after the first call.
    """
    if transport.eg4_hardware_kind_cache is not None:
        return transport.eg4_hardware_kind_cache

    if not transport.send_holding_register:
        return "unknown"

    try:
        cell_1_voltage: int | float | str | None = transport.read_variable("cell_01_voltage", Registry_Type.HOLDING)
    except Exception:
        cell_1_voltage = None
        _log.debug("Could not read cell_01_voltage in {transport.transport_name} while detecting EG4 hardware kind", exc_info=True)

    if cell_1_voltage is not None and EG4_CELL_VOLTAGE_MIN <= float(cell_1_voltage) <= EG4_CELL_VOLTAGE_MAX:
        transport.eg4_hardware_kind_cache = "battery"
    else:
        # Either cell_01_voltage isn't defined for this protocol's map
        # (inverters) or it read outside a plausible cell-voltage range (not
        # actually a battery, or a bad read) — default to inverter, since
        # that's the far more common case and inverter metadata reads are
        # all individually try/except-guarded anyway.
        transport.eg4_hardware_kind_cache = "inverter"
    msg: str = (
        f"Transport {transport.transport_name} detected EG4 hardware kind: "
        f"{transport.eg4_hardware_kind_cache} (cell_01_voltage={cell_1_voltage})")
    _log.info(msg)
    return transport.eg4_hardware_kind_cache


def read_eg4_serial_number(transport: EG4MetadataTransport) -> str:
    """EG4-specific serial number reconstruction.

    EG4 registry maps encode the 10-character serial number as ten
    single-character fields named 'SN_0_...' through 'SN_9_...', packed two
    characters (one register) at a time across five consecutive registers —
    holding registers 2-6, or input registers 115-119 (see
    EG4_SERIAL_NUMBER_HOLDING_REGISTERS / EG4_SERIAL_NUMBER_INPUT_REGISTERS).

    These fixed addresses are read directly via an explicit start/end
    transport.read_modbus_registers() call, the same fixed-address pattern
    used for the device type code (holding register 19) — this both
    sidesteps the single-register limit of protocol_settings' generic ASCII
    decoder (see this module's docstring) and, deliberately, bypasses
    variable_mask/variable_screen filtering entirely, since those filters
    remove the SN_* entries from the loaded registry_map outright rather
    than just hiding their values (see the register constants' comments for
    why that made the previous registry_map-scanning approach here return ""
    whenever a transport's mask/screen didn't happen to include the SN_*
    fields).

    Character order within each register follows EG4_TEXT_HIGH_BYTE_FIRST.

    For battery hardware (see detect_eg4_hardware_kind()), the serial number
    is only readable over CANbus, not Modbus, so this returns "" immediately
    without attempting any register reads.
    """
    if detect_eg4_hardware_kind(transport) == "battery":
        msg: str = (
            f"Transport {transport.transport_name} is an EG4 battery — "
            f"serial number requires a CANbus connection and isn't readable "
            f"over Modbus.")
        _log.info(msg)
        return ""

    registers_by_type: list[tuple[Registry_Type, tuple[int, int]]] = [
        (Registry_Type.HOLDING, EG4_SERIAL_NUMBER_HOLDING_REGISTERS),
        (Registry_Type.INPUT, EG4_SERIAL_NUMBER_INPUT_REGISTERS),
    ]

    for r_type, (start_reg, end_reg) in registers_by_type:
        if r_type == Registry_Type.HOLDING and not transport.send_holding_register:
            continue
        if r_type == Registry_Type.INPUT and not transport.send_input_register:
            continue

        msg: str = (
            f"Reconstructing EG4 serial number from {r_type.name} registers "
            f"{start_reg}-{end_reg} (fixed address, bypassing mask/screen).")
        _log.info(msg)

        sn_chars: list[str] = []
        read_failed = False
        for reg in range(start_reg, end_reg + 1):
            data: Dict[int, int] = transport.read_modbus_registers(start=reg, end=reg, registry_type=r_type)
            if not data or reg not in data:
                msg = (
                    f"Failed reading EG4 SN register {reg} ({r_type.name}) — "
                    f"treating partial read as total failure for SN integrity.")
                _log.warning(msg)
                read_failed = True
                break

            val: int = data[reg] & 0xFFFF
            for b in _decode_register_chars(val):
                if 0x20 <= b <= 0x7E:
                    sn_chars.append(chr(b))
                # else: null terminator / padding / non-printable — drop, matching
                # protocol_settings._decode_text_bytes' handling of ASCII fields.

            time.sleep(transport.modbus_delay * 2)

        if read_failed:
            continue

        sn_decoded: str = "".join(sn_chars).strip()
        if sn_decoded and not re.search(r"[^a-zA-Z0-9_]", sn_decoded):
            msg = f"Read EG4 SN from {r_type.name}: {sn_decoded}"
            _log.info(msg)
            return sn_decoded

    return ""


def read_eg4_device_metadata(transport: EG4MetadataTransport) -> EG4DeviceMetadata | EG4BatteryMetadata | None:
    """Assemble EG4 device metadata beyond just the serial number. Dispatches
    to _read_inverter_metadata() or _read_battery_metadata() based on
    detect_eg4_hardware_kind(), since inverters and batteries expose
    completely different information at the same register addresses.

    Returns None if this isn't an EG4 protocol.
    """
    protocol_name: str = getattr(transport.proto, "protocol", "") or ""
    if not is_eg4_protocol(protocol_name):
        return None

    if detect_eg4_hardware_kind(transport) == "battery":
        return _read_battery_metadata(transport)
    return _read_inverter_metadata(transport)


def _read_inverter_metadata(transport: EG4MetadataTransport) -> EG4DeviceMetadata:
    """Assemble metadata for an EG4 inverter/GridBOSS: model, device type
    code, GridBOSS/MID detection, firmware version, and parallel-group role.
    Modeled on the reference discovery module's DiscoveredDevice, but sourced
    entirely from the transport's own register reads.

    Individual fields fall back to safe defaults (rather than aborting the
    whole read) if their underlying registers aren't available for this
    particular EG4 variant — e.g. a GridBOSS has no PV/battery runtime
    registers, and some EG4 maps may not expose the parallel-config fields at
    all.
    """
    serial: str = transport.device_serial_number or read_eg4_serial_number(transport)

    # Device type code — holding register 19. Fixed address, not a named CSV
    # field (see EG4_DEVICE_TYPE_CODE_* constants above).
    device_type_code: int | None = None
    if transport.send_holding_register:
        try:
            data: Dict[int, int] = transport.read_modbus_registers(start=19, end=19, registry_type=Registry_Type.HOLDING)
            if data and 19 in data:
                device_type_code = data[19] & 0xFFFF
        except Exception:
            _log.debug("Could not read EG4 device type code (holding register 19)", exc_info=True)

    # Model — try the fine-grained power-rating decode (holding registers
    # 0-1) first, since it distinguishes variants within a family (e.g.
    # FlexBOSS21 vs. FlexBOSS18) that the coarse device-type-code map can't.
    # Fall back to the coarse map if registers 0-1 aren't
    # available/recognized, or if device_type_code itself is unknown.
    model: str = "Unknown"
    is_gridboss: bool = False
    if device_type_code is not None:
        _, is_gridboss = EG4_DEVICE_TYPE_MODEL_MAP.get(device_type_code, ("Unknown", False))

        fine_model: str | None = None
        if transport.send_holding_register and not is_gridboss:
            try:
                data = transport.read_modbus_registers(start=0, end=1, registry_type=Registry_Type.HOLDING)
                if data and 0 in data and 1 in data:
                    model_info: EG4ModelInfo = EG4ModelInfo.from_registers(data[0], data[1])
                    fine_model = model_info.get_model_name(device_type_code)
            except Exception:
                _log.debug("Could not read EG4 HOLD_MODEL registers (0-1)", exc_info=True)

        if fine_model:
            model = fine_model
        else:
            model, _ = EG4_DEVICE_TYPE_MODEL_MAP.get(device_type_code, ("Unknown", False))

    # Firmware version. Two conventions were found for this and they disagree
    # on structure as well as byte order — this implementation follows the
    # more specific one, which lines up with how this EG4 map's own
    # registers 9-10 are actually labeled (Com_Ver at the high byte of reg 9,
    # Cntl_Ver at the low byte of reg 10):
    #   prefix  = registers 7-8 decoded as 4 ASCII characters
    #   version = f"{high byte of reg 9:02X}{low byte of reg 10:02X}"
    #   result  = "<prefix>-<version>", e.g. "FAAB-2525"
    # The alternative reference implementation instead treats all of
    # registers 7-10 as 8 raw ASCII characters, which can't be right for this
    # map specifically — registers 9-10 are documented 8-bit version fields
    # (Com_Ver/Slave_Ver/Cntl_Ver/FWVeR), not text.
    # Byte order within the prefix follows EG4_TEXT_HIGH_BYTE_FIRST, same as
    # the serial number — flip that one constant if hardware testing shows
    # it's backwards, and both will correct together.
    firmware_version: str = ""
    if transport.send_holding_register:
        try:
            fw_regs: Dict[int, int] = transport.read_modbus_registers(start=7, end=10, registry_type=Registry_Type.HOLDING)
            if fw_regs and all(r in fw_regs for r in (7, 8, 9, 10)):
                prefix_chars: list[str] = []
                for reg in (7, 8):
                    for b in _decode_register_chars(fw_regs[reg] & 0xFFFF):
                        if 0x20 <= b <= 0x7E:
                            prefix_chars.append(chr(b))
                prefix: str = "".join(prefix_chars)

                com_ver: int = (fw_regs[9] >> 8) & 0xFF
                cntl_ver: int = fw_regs[10] & 0xFF
                firmware_version = f"{prefix}-{com_ver:02X}{cntl_ver:02X}" if prefix else f"{com_ver:02X}{cntl_ver:02X}"
        except Exception:
            _log.debug("Could not read EG4 firmware version registers (7-10)", exc_info=True)

    # Parallel group configuration — EG4 input register 113. Already decoded
    # into named bitfields by the registry map CSV, unlike the device type /
    # model registers above.
    parallel_number: int = 0
    parallel_master_slave: int = 0
    parallel_phase: int = 0
    if transport.send_input_register:
        try:
            role: int | float | str | None = transport.read_variable("MasterOrSlave", Registry_Type.INPUT)
            phase: int | float | str | None = transport.read_variable("SingleOrThreePhase", Registry_Type.INPUT)
            group: int | float | str | None = transport.read_variable("ParallelNum", Registry_Type.INPUT)
            if role is not None:
                parallel_master_slave = int(role)
            if phase is not None:
                parallel_phase = int(phase)
            if group is not None:
                parallel_number = int(group)
        except Exception:
            _log.debug("Could not read EG4 parallel group configuration (input register 113)", exc_info=True)

    return EG4DeviceMetadata(
        hardware_kind="inverter",
        serial=serial,
        model=model,
        device_type_code=device_type_code,
        is_gridboss=is_gridboss,
        firmware_version=firmware_version or "Unknown",
        parallel_number=parallel_number,
        parallel_master_slave=parallel_master_slave,
        parallel_phase=parallel_phase,
    )


def _read_battery_metadata(transport: EG4MetadataTransport) -> EG4BatteryMetadata:
    """Assemble metadata for an EG4 lithium battery (e.g. LL-S). Serial
    number, model, and firmware version are deliberately NOT read here — the
    battery's registry map declares 'serial_no' (holding 120-127), 'model'
    (holding 105-116), and 'fw_version' (holding 117-119), but per hardware
    documentation those only populate over the battery's CANbus interface;
    querying them over Modbus is expected to fail or time out, so this
    reports them as unavailable instead of attempting the read.
    pack_voltage/soc/soh and cell_count (from however many cell_NN_voltage
    fields the map defines) come from Modbus normally.
    """
    cell_count: int = 0
    pack_voltage: float | None = None
    soc: float | None = None
    soh: float | None = None

    if transport.send_holding_register:
        try:
            registry_map: list[registry_map_entry] = transport.proto.get_registry_map(Registry_Type.HOLDING)
            cell_count = sum(
                1 for entry in registry_map
                if entry.variable_name and re.match(r"^cell_\d+_voltage$", entry.variable_name)
            )
        except Exception:
            _log.debug("Could not enumerate EG4 battery cell_NN_voltage fields", exc_info=True)

        for var_name in ("pack_voltage", "soc", "soh"):
            try:
                value: int | float | str | None = transport.read_variable(var_name, Registry_Type.HOLDING)
                if value is not None:
                    if var_name == "pack_voltage":
                        pack_voltage = float(value)
                    elif var_name == "soc":
                        soc = float(value)
                    elif var_name == "soh":
                        soh = float(value)
            except Exception:
                msg: str = f"Could not read EG4 battery '{var_name}'"
                _log.debug(msg, exc_info=True)

    return EG4BatteryMetadata(
        hardware_kind="battery",
        serial=None,
        model=None,
        firmware_version=None,
        cell_count=cell_count,
        pack_voltage=pack_voltage,
        soc=soc,
        soh=soh,
    )


# ----------------------------------------------------------------------------
# Per-cycle synthetic metrics — the post_process_data() injection path
# ----------------------------------------------------------------------------
# Everything above this point is a plain function library used at connect
# time (read_eg4_serial_number, read_eg4_device_metadata) or on demand.
# Neither is wired into the per-cycle metrics dict that reaches
# callers/exports (modbus_base.read_data() etc.) on its own.
#
# This codebase's established mechanism for adding derived/synthetic metrics
# to that dict is the post_process_data(info) hook, called once per cycle by
# transport_base.finish_cycle_tracking() regardless of read mode — see
# modbus_eg4_ll_s_tcp.py's post_process_data() / _compute_cell_stats() /
# _compute_balancing_state() for the reference implementation this follows.
# Injected keys are plain dict entries added via info.update(...), not backed
# by any registry_map_entry — this distinction matters, because it's what the
# webUI and TimescaleDB's wide-table schema registration
# (synthetic_fields_metadata) use to recognize "synthetic, un-maskable"
# metrics and treat them differently from ordinary register-backed ones.
#
#
# compute_eg4_post_process_fields() is meant to be called from a transport's
# post_process_data(info) override:
#
#     def post_process_data(self, info):
#         info.update(eg4_metadata.compute_eg4_post_process_fields(self, info))
#         return info
#
# and eg4_synthetic_fields_metadata() from its synthetic_fields_metadata
# property, so TimescaleDB's wide-table schema registration knows about
# these columns ahead of time (same reason modbus_eg4_ll_s_tcp.py declares
# its own cell_voltage_max_v etc. there). modbus_base.py provides default
# implementations of both hooks that call these two functions directly for
# any EG4 protocol that doesn't need to override them itself.


_BATTERY_SERIAL_FIELD_REGEX: re.Pattern[str] = re.compile(r"^batteryserialnumber_\d+$")


def _find_named_registry(transport: EG4MetadataTransport, variable_name: str) -> Registry_Type | None:
    """Which registry (HOLDING or INPUT), if either, already declares
    ``variable_name`` as an ordinary registry_map entry — i.e. it decodes
    through the normal process_registery() path and lands in ``info`` on its
    own, mask/screen permitting.

    Used to decide whether a corrected value should just overwrite that
    entry's (possibly wrong/truncated) value in place, or — when no CSV
    variant names the field at all — needs its own synthetic declaration so
    it's visible anywhere. Checked per-transport since this varies by EG4
    variant/CSV revision (e.g. not every EG4 map has a single consolidated
    'serial_number' field; some only split it across ten SN_<n>_... fields).
    """
    for registry_type in (Registry_Type.HOLDING, Registry_Type.INPUT):
        try:
            if any(e.variable_name == variable_name for e in transport.proto.get_registry_map(registry_type)):
                return registry_type
        except Exception as e:
            _log.debug(f"registry_type unknown {e}")
            continue
    return None


def compute_eg4_post_process_fields(
    transport: EG4MetadataTransport,
    info: dict[str, int | float | str],
) -> dict[str, int | float | str]:
    """Compute EG4 derived/synthetic fields for injection via ``info.update(...)``
    from a transport's ``post_process_data(info)`` override.

    Returns ``{}`` immediately (no-op) if this isn't an EG4 protocol.
    Otherwise returns model/firmware_version/hardware_kind/is_gridboss for
    inverters (from read_eg4_device_metadata(), which does a handful of
    small register reads every call — see that function's docstring), or
    just hardware_kind for batteries, plus corrected batteryserialnumber_<N>
    values wherever the inverter's own input registers declare them (see
    _read_battery_serial_fields()).

    ``device_type_code`` (holding register 19) is deliberately never part of
    the result, even though ``read_eg4_device_metadata()`` reads it and uses
    it internally to compute model/is_gridboss — that register already
    decodes under its own name through the normal registry_map path on every
    EG4 variant this module has seen, so re-exposing it here would produce a
    duplicate "real" register-19 entry plus a "synthetic" one with the same
    name and value.

    ``info`` (the already-decoded values for this cycle) is otherwise
    accepted for signature symmetry with the post_process_data(info) hook
    and so future resolvers can build on already-decoded values without a
    redundant register read, but isn't read from further than the above —
    every other field here still needs its own live register read
    regardless of what's already in ``info``.

    Every field is independently best-effort: if one piece fails (e.g. a
    register read times out), the rest of this cycle's ``info`` is
    unaffected — this function logs failures but never raises them out to
    the caller.
    """
    protocol_name: str = getattr(transport.proto, "protocol", "") or ""
    if not is_eg4_protocol(protocol_name):
        return {}

    derived: dict[str, int | float | str] = {}

    metadata: EG4DeviceMetadata | EG4BatteryMetadata | None = None
    try:
        metadata = read_eg4_device_metadata(transport)
    except Exception:
        _log.debug(
            f"compute_eg4_post_process_fields: read_eg4_device_metadata failed for "
            f"transport '{transport.transport_name}' — skipping this cycle's metadata fields.",
            exc_info=True,
        )

    if isinstance(metadata, EG4DeviceMetadata):
        derived["hardware_kind"] = metadata.hardware_kind
        derived["model"] = metadata.model
        derived["firmware_version"] = metadata.firmware_version
        derived["is_gridboss"] = 1 if metadata.is_gridboss else 0
        # device_type_code (holding register 19) is deliberately NOT injected
        # here — it already decodes under its own name through the normal
        # registry_map path (its CSV documented_name resolves to variable_name
        # 'device_type_code'), so adding it here would produce a duplicate
        # "real" register-19 entry plus a "synthetic" one with the same value
        # and name in the webUI. metadata.device_type_code is still read and
        # used above/below to compute model/is_gridboss; it's just not
        # separately exposed as its own synthetic field.
        if metadata.serial:
            # Unlike device_type_code, this DOES need injecting even when a
            # CSV entry already names it ('serial_number' on the INPUT map,
            # for variants that have one): protocol_settings' generic ASCII
            # decoder is single-register-only (see read_eg4_serial_number()'s
            # docstring), so that entry's own per-cycle decode truncates the
            # 10-character serial to its first 2 characters. This overwrites
            # that truncated value with the correctly-reassembled one from
            # read_eg4_device_metadata() (which read_eg4_serial_number() feeds
            # — see _read_inverter_metadata()), the same truncation-correction
            # pattern _read_battery_serial_fields() uses for battery serials.
            derived["serial_number"] = metadata.serial
    elif isinstance(metadata, EG4BatteryMetadata):
        derived["hardware_kind"] = metadata.hardware_kind
        # serial/model/firmware intentionally omitted here too — CANbus-only,
        # see EG4BatteryMetadata's docstring.

    try:
        derived.update(_read_battery_serial_fields(transport))
    except Exception:
        _log.debug(
            f"compute_eg4_post_process_fields: _read_battery_serial_fields failed for "
            f"transport '{transport.transport_name}' — skipping corrected battery serials "
            f"this cycle.",
            exc_info=True,
        )

    return derived


def _read_battery_serial_fields(transport: EG4MetadataTransport) -> dict[str, str]:
    """Live-read and correctly decode every batteryserialnumber_<N> field
    declared on this protocol's INPUT registry map.

    protocol_settings' ASCII decoder is single-register-only (see
    read_eg4_serial_number()'s docstring for the full explanation), so the
    normal per-cycle decode of these fields truncates them to their first 2
    characters. This reads all 8 registers each field's CSV entry documents
    ("(8 registers)") directly and decodes the full string, keyed under the
    same variable_name the truncated value would have used — the caller's
    ``info.update(...)`` then overwrites that truncated value rather than
    adding a new key.
    """
    fields: dict[str, str] = {}
    if not transport.send_input_register:
        return fields

    registry_map: list[registry_map_entry] = []
    try:
        registry_map = transport.proto.get_registry_map(Registry_Type.INPUT)
    except Exception:
        _log.debug("_read_battery_serial_fields: could not read INPUT registry map", exc_info=True)
        return fields

    battery_serial_entries: list[registry_map_entry] = [
        entry for entry in registry_map
        if entry.variable_name and _BATTERY_SERIAL_FIELD_REGEX.match(entry.variable_name)
    ]
    if not battery_serial_entries:
        return fields

    for entry in battery_serial_entries:
        start: int = entry.register
        data: dict[int, int] = {}
        try:
            data = transport.read_modbus_registers(start=start, end=start + 7, registry_type=Registry_Type.INPUT)
        except Exception:
            _log.debug(
                f"_read_battery_serial_fields: register read failed for "
                f"'{entry.variable_name}' (registers {start}-{start + 7})",
                exc_info=True,
            )
            continue

        chars: list[str] = []
        missing: list[int] = [start + i for i in range(8) if data.get(start + i) is None]
        if missing:
            _log.debug(
                f"_read_battery_serial_fields: registers {missing} missing from the read "
                f"for '{entry.variable_name}' — leaving this field's value untouched this cycle."
            )
            continue

        for i in range(8):
            raw: int = data[start + i]
            for b in _decode_register_chars(raw & 0xFFFF):
                if 0x20 <= b <= 0x7E:
                    chars.append(chr(b))

        decoded: str = "".join(chars).strip()
        if decoded:
            fields[entry.variable_name] = decoded
            _log.debug(f"_read_battery_serial_fields: '{entry.variable_name}' = '{decoded}'")

    return fields


def eg4_synthetic_fields_metadata(transport: EG4MetadataTransport) -> list[tuple[str, str, float, str, str]]:
    """Field declarations for compute_eg4_post_process_fields()'s output, in
    an ``(variable_name, data_type, unit_mod, note, registry_type)`` format —
    the same ``(variable_name, data_type, unit_mod, note)`` shape
    ``modbus_eg4_ll_s_tcp.synthetic_fields_metadata`` documents (used by
    TimescaleDB's wide-table schema registration so these columns are created
    ahead of time with the correct type, instead of being reported as
    unexpected/missing extra_keys), plus a trailing ``registry_type`` string
    ("holding" / "input", lowercase — matching ``DeviceRegisterView.registry_type``,
    the ``ProtocolRegister.registry_type`` DB column, and the
    ``/api/protocols/{protocol}/{registry_type}/table`` URL segment, rather
    than the ``Registry_Type`` enum those consumers don't otherwise deal in)
    so consumers that render per-registry views (e.g. the webUI's
    Holding/Input tabs) can show each synthetic field only under the
    registry it's actually extracted from, instead of duplicating every
    synthetic field onto every tab. This 5th element is additive — existing
    4-tuple-returning implementations (e.g. modbus_eg4_ll_s_tcp.py,
    modbus_eg4_ll_s_rtu.py) remain valid; consumers should treat a missing
    5th element as "registry-agnostic, show/register everywhere" rather
    than assuming every synthetic_fields_metadata implementation supplies
    one. ``registry_type`` reflects where the *source* registers for that
    field live: HOLDING for model/firmware_version/is_gridboss/hardware_kind
    (holding registers 0-1, 7-10, 19), INPUT for the corrected
    batteryserialnumber_<N> fields (they correct an existing INPUT-map entry
    in place).

    Returns ``[]`` if this isn't an EG4 protocol. For battery hardware, only
    'hardware_kind' is declared (the only field compute_eg4_post_process_fields
    actually produces for batteries); inverters additionally get model/
    firmware_version/is_gridboss and one entry per batteryserialnumber_<N>
    field this protocol's INPUT map declares. device_type_code (holding
    register 19) is intentionally never declared here — see
    compute_eg4_post_process_fields()'s docstring for why.
    """
    protocol_name: str = getattr(transport.proto, "protocol", "") or ""
    if not is_eg4_protocol(protocol_name):
        return []

    fields: list[tuple[str, str, float, str, str]] = [
        ("hardware_kind", "ASCII", 1.0, "Synthetic: 'battery' or 'inverter'.", Registry_Type.HOLDING.name.lower()),
    ]

    is_battery: bool = False
    try:
        is_battery = detect_eg4_hardware_kind(transport) == "battery"
    except Exception:
        _log.debug("eg4_synthetic_fields_metadata: detect_eg4_hardware_kind failed", exc_info=True)

    if not is_battery:
        fields.extend([
            ("model", "ASCII", 1.0, "Synthetic: model name derived from device type code and the HOLD_MODEL bitfield.", Registry_Type.HOLDING.name.lower()),
            ("firmware_version", "ASCII", 1.0, "Synthetic: assembled from holding registers 7-10.", Registry_Type.HOLDING.name.lower()),
            (
                "is_gridboss", "BOOLEAN", 1.0,
                "Synthetic: True if device type code indicates a GridBOSS/MID device, else False.",
                Registry_Type.HOLDING.name.lower(),
            ),
        ])

        # serial_number: only declared here for EG4 variants whose CSV has
        # no consolidated 'serial_number' field at all (some only split it
        # across ten SN_<n>_... fields) — where one does exist, the value is
        # corrected in place instead (see compute_eg4_post_process_fields()),
        # and declaring it here too would duplicate that row, the same
        # problem device_type_code had. Left untagged (registry-agnostic,
        # shows on every tab) rather than guessing HOLDING or INPUT: without
        # a named CSV entry to anchor it, read_eg4_serial_number() may end up
        # sourcing it from either depending on which registers this variant
        # and this cycle actually have available.
        named_registry: Registry_Type | None = _find_named_registry(transport, "serial_number")
        if named_registry is None:
            fields.append((
                "serial_number", "ASCII", 1.0,
                "Synthetic: reassembled from the SN_<n>_... registers (see read_eg4_serial_number()).",
                "",  # registry-agnostic — see comment above
            ))

        try:
            registry_map: list[registry_map_entry] = transport.proto.get_registry_map(Registry_Type.INPUT)
            for entry in registry_map:
                if entry.variable_name and _BATTERY_SERIAL_FIELD_REGEX.match(entry.variable_name):
                    fields.append((
                        entry.variable_name,
                        "ASCII",
                        1.0,
                        "Corrected full serial — protocol_settings' ASCII decoder truncates this field to 2 characters.",
                        Registry_Type.INPUT.name.lower(),
                    ))
        except Exception:
            _log.debug(
                "eg4_synthetic_fields_metadata: could not enumerate batteryserialnumber_<N> entries",
                exc_info=True,
            )

    return fields
