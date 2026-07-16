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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Dict, Protocol

from .protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    protocol_settings,
    register_synthetic_resolver,
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
# Read from EG4 holding register 19. This addressing convention (and the
# codes themselves) is consistent across EG4/LuxPower firmware families and
# isn't expressed anywhere in the per-model registry map CSVs (register 19
# shows up there only as an unlabeled "Unknown" ushort), so it's captured
# here instead of relying on a CSV field name.
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
# Absent a live device to test against, this module follows the first
# (high-byte-first) convention, since it's the one independently corroborated
# by the datasheet's own worked example. If serial numbers or firmware
# strings come out reversed/garbled on real hardware, flip this constant.
EG4_TEXT_HIGH_BYTE_FIRST: bool = True


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
    characters (one register) at a time across five consecutive registers.
    The exact suffix after 'SN_<n>_' and the exact registers used vary
    between EG4 variants (18kPV, GridBOSS, LL-S, v58, etc.), so rather than
    hard-coding register numbers this scans the transport's loaded registry
    map for any variable matching that naming convention, derives the
    distinct registers involved from those entries, and reads them directly —
    sidestepping the single-register limit of protocol_settings' generic
    ASCII decoder entirely (see this module's docstring).

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

    sn_field_regex: re.Pattern[str] = re.compile(r"^sn_\d+_")

    for r_type in (Registry_Type.HOLDING, Registry_Type.INPUT):
        if r_type == Registry_Type.HOLDING and not transport.send_holding_register:
            continue
        if r_type == Registry_Type.INPUT and not transport.send_input_register:
            continue

        registry_map: list[registry_map_entry] = transport.proto.get_registry_map(r_type)
        sn_registers: list[int] = sorted({
            entry.register for entry in registry_map
            if entry.variable_name and sn_field_regex.match(entry.variable_name)
        })

        if not sn_registers:
            continue
        msg: str = (
            f"Reconstructing EG4 serial number from {r_type.name} registers {sn_registers}")
        _log.info(msg)

        sn_chars: list[str] = []
        read_failed = False
        for reg in sn_registers:
            data: Dict[int, int] = transport.read_modbus_registers(start=reg, end=reg, registry_type=r_type)
            if not data or reg not in data:
                msg: str = (
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
            msg: str = f"Read EG4 SN from {r_type.name}: {sn_decoded}"
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
# Synthetic registry entries — the injection path into process_registery()
# ----------------------------------------------------------------------------
# Everything above this point is the connect-time path: read_eg4_serial_number()
# and read_eg4_device_metadata() run once at connect and hand back a string or a
# dataclass, which is fine for logging/one-off use but never reaches the normal
# per-cycle metrics dict that flows to callers/exports (modbus_base.read_data()
# etc.), since that dict is built entirely from registry_map_entry decoding.
#
# The functions below plug EG4-derived values into that dict instead, using
# the same general-purpose synthetic-entry mechanism protocol_settings.py
# already uses for '<name>_desc' entries (registry_map_entry.description_source
# + a resolution pass in process_registery()), generalized via
# registry_map_entry.synthetic_resolver + register_synthetic_resolver() so it
# can carry a value more complex than a single code-dict lookup.
#
# Two distinct cases, per how the calling code should treat the result:
#   - Genuinely new metrics (model, firmware_version, device_type_code,
#     hardware_kind, is_gridboss) get brand new synthetic registry_map_entry
#     objects appended to the map, so they show up as new keys.
#   - Fields the CSV already declares but that protocol_settings' single-
#     register ASCII decoder truncates (batteryserialnumber_1..N, spanning 8
#     registers each) get their *existing* entry's synthetic_resolver field
#     set in place, so the corrected value lands under the same key the
#     truncated one used to.
#
# All resolvers here are pure functions of (proto, registry, info, entry) with
# no closures over per-instance data, so they're safe to register once under
# fixed global names even when multiple EG4 devices (possibly the same model)
# are connected at once — see register_synthetic_resolver()'s docstring.


def _find_registry_entry(proto: protocol_settings, registry_type: Registry_Type, variable_name: str) -> registry_map_entry | None:
    try:
        for entry in proto.get_registry_map(registry_type):
            if entry.variable_name == variable_name:
                return entry
    except Exception:
        msg: str = f"Could not find registry entry for {variable_name} in {registry_type.name}"
        _log.debug(msg, exc_info=True)
        pass
    return None


def _as_int(raw: int | bytes | tuple[bytes, float] | None) -> int | None:
    """Narrow a process_registery()-style raw register value down to a plain
    int. EG4 registers are always simple ints in practice (never the bytes/
    tuple forms process_registery() also supports for other transports'
    byte-oriented reads), but the shared SyntheticResolver signature has to
    accommodate all of them, so every resolver below narrows through this
    rather than assuming int."""
    if isinstance(raw, int):
        return raw
    return None


def _resolve_eg4_hardware_kind(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> str | None:
    """Same signal as detect_eg4_hardware_kind() (cell_01_voltage in a
    plausible 2.0-4.0V range), but re-derived from this cycle's actual
    register snapshot rather than a cached connect-time read, and looked up
    by variable name rather than a hard-coded register address so it still
    works if a future EG4 variant puts cell_01_voltage somewhere else."""
    cell_entry: registry_map_entry | None = _find_registry_entry(proto, Registry_Type.HOLDING, "cell_01_voltage")
    if cell_entry is not None:
        raw: int | None = _as_int(registry.get(cell_entry.register))
        if raw is not None:
            voltage: float = (raw & 0xFFFF) * (cell_entry.unit_mod or 1.0)
            if EG4_CELL_VOLTAGE_MIN <= voltage <= EG4_CELL_VOLTAGE_MAX:
                return "battery"
    return "inverter"


def _resolve_eg4_device_type_code(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> int | None:
    """Holding register 19. Fixed address per the EG4/LuxPower firmware
    convention (see the EG4_DEVICE_TYPE_CODE_* constants) — not something a
    registry map ever names, so this is read directly rather than looked up
    by variable name."""
    raw: int | None = _as_int(registry.get(19))
    return (raw & 0xFFFF) if raw is not None else None


def _resolve_eg4_is_gridboss(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> int | None:
    device_type_code: int | float | str | None = info.get("device_type_code")
    if device_type_code is None:
        device_type_code = _resolve_eg4_device_type_code(proto, registry, info, entry)
    if device_type_code is None:
        return None
    _, is_gridboss = EG4_DEVICE_TYPE_MODEL_MAP.get(int(device_type_code), ("Unknown", False))
    return 1 if is_gridboss else 0


def _resolve_eg4_model(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> str | None:
    device_type_code_value: int | float | str | None = info.get("device_type_code")
    if device_type_code_value is None:
        device_type_code_value = _resolve_eg4_device_type_code(proto, registry, info, entry)
    if device_type_code_value is None:
        return None
    device_type_code: int = int(device_type_code_value)

    _, is_gridboss = EG4_DEVICE_TYPE_MODEL_MAP.get(device_type_code, ("Unknown", False))
    if not is_gridboss:
        reg0: int | None = _as_int(registry.get(0))
        reg1: int | None = _as_int(registry.get(1))
        if reg0 is not None and reg1 is not None:
            fine_model: str | None = EG4ModelInfo.from_registers(reg0 & 0xFFFF, reg1 & 0xFFFF).get_model_name(device_type_code)
            if fine_model:
                return fine_model

    model, _ = EG4_DEVICE_TYPE_MODEL_MAP.get(device_type_code, ("Unknown", False))
    return model


def _resolve_eg4_firmware_version(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> str | None:
    """See _read_inverter_metadata()'s docstring for why this specific
    structure (registers 7-8 as a 4-char prefix, hi-byte-of-9/lo-byte-of-10 as
    a hex version suffix) was chosen over the alternative reference
    implementation that treats 7-10 as 8 raw ASCII characters."""
    regs: dict[int, int | None] = {r: _as_int(registry.get(r)) for r in (7, 8, 9, 10)}
    if any(v is None for v in regs.values()):
        return None

    prefix_chars: list[str] = []
    for reg in (7, 8):
        reg_value: int = regs[reg]  # type: ignore[assignment]  # narrowed non-None by the `any(...)` check above
        for b in _decode_register_chars(reg_value & 0xFFFF):
            if 0x20 <= b <= 0x7E:
                prefix_chars.append(chr(b))
    prefix: str = "".join(prefix_chars)

    reg9: int = regs[9]  # type: ignore[assignment]  # narrowed non-None by the `any(...)` check above
    reg10: int = regs[10]  # type: ignore[assignment]  # narrowed non-None by the `any(...)` check above
    com_ver: int = (reg9 >> 8) & 0xFF
    cntl_ver: int = reg10 & 0xFF
    return f"{prefix}-{com_ver:02X}{cntl_ver:02X}" if prefix else f"{com_ver:02X}{cntl_ver:02X}"


def _resolve_eg4_battery_serial(
    proto: protocol_settings,
    registry: Mapping[int, int | bytes | tuple[bytes, float]],
    info: dict[str, int | float | str],
    entry: registry_map_entry,
) -> str | None:
    """Reconstructs a batteryserialnumber_<N> field from all 8 of its
    registers (per the CSV's own "(8 registers)" note), rather than the 1
    register protocol_settings' ASCII decoder is limited to — this is the fix
    for the truncated-to-2-characters serial number. Works for any
    batteryserialnumber_<N> entry generically since it reads its span
    starting at entry.register (already correct per-entry from the CSV
    parse: 5019 for _1, 5049 for _2, etc.) rather than a hard-coded address,
    so one resolver registration covers all of them."""
    start: int = entry.register
    chars: list[str] = []
    for i in range(8):
        raw: int | None = _as_int(registry.get(start + i))
        if raw is None:
            return None
        for b in _decode_register_chars(raw & 0xFFFF):
            if 0x20 <= b <= 0x7E:
                chars.append(chr(b))
    decoded: str = "".join(chars).strip()
    return decoded or None


def _make_synthetic_entry(name: str, resolver_name: str, note: str) -> registry_map_entry:

    return registry_map_entry(
        registry_type=Registry_Type.HOLDING,
        register=-1,
        register_bit=-1,
        register_bit_end=-1,
        register_byte=0,
        variable_name=name,
        documented_name=name,
        note=note,
        unit="",
        unit_mod=1.0,
        adjustments={},
        concatenate=False,
        concatenate_registers=[],
        values=[],
        value_regex="",
        value_min=0,
        value_max=0,
        data_type=Data_Type.STRING,
        data_type_size=-1,
        read_command=None,
        read_interval=0,
        write_mode=WriteMode.READDISABLED,
        has_enum_mapping=False,
        synthetic_resolver=resolver_name,
    )


_resolvers_registered: bool = False


def _register_eg4_resolvers() -> None:
    """Idempotent; safe to call from ensure_eg4_synthetic_entries() every time
    a new EG4 protocol_settings instance loads its registry map, since these
    are stateless module-level functions, not per-instance closures."""
    global _resolvers_registered
    if _resolvers_registered:
        return
    register_synthetic_resolver("eg4_hardware_kind", _resolve_eg4_hardware_kind)
    register_synthetic_resolver("eg4_device_type_code", _resolve_eg4_device_type_code)
    register_synthetic_resolver("eg4_is_gridboss", _resolve_eg4_is_gridboss)
    register_synthetic_resolver("eg4_model", _resolve_eg4_model)
    register_synthetic_resolver("eg4_firmware_version", _resolve_eg4_firmware_version)
    register_synthetic_resolver("eg4_battery_serial", _resolve_eg4_battery_serial)
    _resolvers_registered = True


_BATTERY_SERIAL_FIELD_REGEX: re.Pattern[str] = re.compile(r"^batteryserialnumber_\d+$")


def ensure_eg4_synthetic_entries(proto: protocol_settings, registry_map: list[registry_map_entry], registry_type: Registry_Type) -> None:
    """Called once per (protocol, registry_type) from protocol_settings.load__registry()
    for any "eg4"-family protocol, right after its existing '_desc' synthetic-entry
    pass. This is the injection point: after this runs, model/firmware_version/
    device_type_code/hardware_kind/is_gridboss (for inverters) and corrected
    batteryserialnumber_<N> values (for inverters reporting CANbus-connected
    battery data) flow through process_registery() into the same dict every
    other metric does.

    What gets added is decided from the entries already present in
    ``registry_map`` (structural/static — no live register read is possible at
    this stage, since this runs at CSV-parse time before any transport has
    connected). This is deliberately coarser than detect_eg4_hardware_kind()'s
    live, value-range-based check used at connect time: it only has to decide
    which synthetic *entries* are even worth adding for this particular CSV,
    not authoritatively classify hardware — that's still the runtime
    _resolve_eg4_hardware_kind() resolver's job, re-evaluated every read cycle
    from the actual register snapshot.

    No-op (and never adds duplicate entries) if called again for a
    registry_map that already has these entries — variable-name-checked, same
    as protocol_settings._add_code_description_entries().
    """
    _register_eg4_resolvers()

    if registry_type == Registry_Type.HOLDING:
        existing_names: set[str] = {e.variable_name for e in registry_map}
        is_battery_map: bool = "cell_01_voltage" in existing_names

        additions: list[registry_map_entry] = []

        def add(name: str, resolver_name: str, note: str) -> None:
            if name in existing_names:
                return
            additions.append(_make_synthetic_entry(name, resolver_name, note))
            existing_names.add(name)

        # hardware_kind is meaningful — and cheaply derivable from data
        # that's already read every cycle — on every EG4 holding map,
        # battery or inverter alike.
        add("hardware_kind", "eg4_hardware_kind", "Synthetic: 'battery' or 'inverter', derived from cell_01_voltage presence/range.")

        if not is_battery_map:
            # device_type_code/model/is_gridboss/firmware_version only make
            # sense on inverter-schema maps — on a battery map, holding
            # registers 0-1/7-10/19 mean something else entirely (or nothing).
            add("device_type_code", "eg4_device_type_code", "Synthetic: raw value of holding register 19.")
            add("model", "eg4_model", "Synthetic: model name derived from device type code (register 19) and the HOLD_MODEL bitfield (registers 0-1).")
            add("is_gridboss", "eg4_is_gridboss", "Synthetic: 1 if device type code indicates a GridBOSS/MID device, else 0.")
            add("firmware_version", "eg4_firmware_version", "Synthetic: assembled from holding registers 7-10.")

        registry_map.extend(additions)

    elif registry_type == Registry_Type.INPUT:
        # batteryserialnumber_<N>: correct the *existing* entries in place —
        # per the CSV these already declare an 8-register ASCII span, but
        # protocol_settings' ASCII decoder is single-register-only, so without
        # this they truncate to the field's first 2 characters. Same fix
        # (and same underlying protocol_settings limitation) as the inverter's
        # own serial number — see read_eg4_serial_number()'s docstring.
        for entry in registry_map:
            if entry.variable_name and _BATTERY_SERIAL_FIELD_REGEX.match(entry.variable_name):
                entry.synthetic_resolver = "eg4_battery_serial"
