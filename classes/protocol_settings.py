# Description: Implements protocol_settings functionality for the MultiProtocolGateway application.
# File: protocol_settings.py
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

from __future__ import annotations

import ast
import csv
import itertools
import json
import logging
import math
import os
import re
import struct
import sys
import time
from collections import ChainMap
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional, cast

from defs.common import TransportSettings, strtoint, strtoint_safe

# Largest finite IEEE-754 values, as ints, for the default FLOAT32/FLOAT64 value ranges.
_FLOAT32_MAX: int = int(3.4028234663852886e38)
_FLOAT64_MAX: int = int(sys.float_info.max)

# Template rows (dynamic register maps).  ``{i}`` in a CSV row's register / variable name /
# documented name is replaced by the 1-based instance number; the number of instances comes from
# the anchor ``<stem>_count`` where ``<stem>`` is the text before ``_{i}_`` in the register cell
# (``model_203_{i}_base`` -> ``model_203_count``).  See documentation/architecture/dynamic_register_discovery.md.
_INSTANCE_TOKEN: str = "{i}"  # noqa: S105
_INSTANCE_STEM_REGEX: re.Pattern[str] = re.compile(r"(?P<stem>[A-Za-z_][A-Za-z0-9_]*?)_\{i\}_")
_TEMPLATE_ROW_KEYS: tuple[str, ...] = ("register", "variable name", "documented name", "adjustments", "note")


class Data_Type(Enum):
    BYTE = 1
    '''8bit byte'''
    USHORT = 2
    '''16 bit unsigned int'''
    UINT = 3
    '''32 bit unsigned int'''
    SHORT = 4
    '''16 bit signed int'''
    INT = 5
    '''32 bit signed int'''
    UINT64 = 6
    '''64 bit unsigned int'''
    _16BIT_FLAGS = 7
    _8BIT_FLAGS = 8
    _32BIT_FLAGS = 9
    FLOAT32 = 10
    '''32 bit floating point'''
    FLOAT64 = 11
    '''64 bit floating point'''
    ACC32 = 12
    '''32 bit unsigned accumulator'''
    INT64 = 13
    '''64 bit signed int'''

    ASCII = 84
    ''' 2 characters '''
    HEX = 85
    ''' HEXADECIMAL STRING '''
    STRING16 = 86
    ''' 16 byte string '''
    STRING32 = 87
    ''' 32 byte string '''
    STRING = 88
    ''' variable length string '''

    _1BIT = 201
    _2BIT = 202
    _3BIT = 203
    _4BIT = 204
    _5BIT = 205
    _6BIT = 206
    _7BIT = 207
    _8BIT = 208
    _9BIT = 209
    _10BIT = 210
    _11BIT = 211
    _12BIT = 212
    _13BIT = 213
    _14BIT = 214
    _15BIT = 215
    _16BIT = 216
    # signed bits
    _2SBIT = 302
    _3SBIT = 303
    _4SBIT = 304
    _5SBIT = 305
    _6SBIT = 306
    _7SBIT = 307
    _8SBIT = 308
    _9SBIT = 309
    _10SBIT = 310
    _11SBIT = 311
    _12SBIT = 312
    _13SBIT = 313
    _14SBIT = 314
    _15SBIT = 315
    _16SBIT = 316

    # signed magnitude bits
    _2SMBIT = 402
    _3SMBIT = 403
    _4SMBIT = 404
    _5SMBIT = 405
    _6SMBIT = 406
    _7SMBIT = 407
    _8SMBIT = 408
    _9SMBIT = 409
    _10SMBIT = 410
    _11SMBIT = 411
    _12SMBIT = 412
    _13SMBIT = 413
    _14SMBIT = 414
    _15SMBIT = 415
    _16SMBIT = 416

    # Public aliases for the four underscore-prefixed members above that get
    # referenced by name from outside this class elsewhere in this file
    # (protocol_settings/DataAdjustments methods). Those members are
    # underscore-prefixed only because Python identifiers can't start with a
    # digit (_8BIT, _16BIT_FLAGS, etc.), not because they're meant to be
    # private — but pyright's reportPrivateUsage can't tell the difference
    # and flags any leading-underscore attribute access across a class
    # boundary. Enum members sharing a value automatically become aliases of
    # each other (`Data_Type.BIT8 is Data_Type._8BIT`), so this is zero
    # behavior change; external code should prefer these names.
    BIT8 = 208
    BIT8_FLAGS = 8
    BIT16_FLAGS = 7
    BIT32_FLAGS = 9

    @classmethod
    def fromString(cls, name: str) -> "Data_Type | None":
        """Return the ``Data_Type`` member matching ``name``, or ``None`` if unrecognized.

        Strips whitespace and uppercases before lookup.  Names that start with a
        digit are prefixed with ``_`` to match the enum member naming convention
        (e.g. ``"16bit"`` → ``_16BIT``).  A built-in alias table maps common
        alternative spellings (``uint16``, ``s16``, ``float``, etc.) to their
        canonical member names before the final lookup.  Logs a warning and
        returns ``None`` rather than raising for unknown names.
        """
        name = name.strip().upper()
        if not name:
            return None

        if name[0].isdigit():
            name = "_" + name

        # Common alternative names
        alias: dict[str, str] = {
            "UINT8": "BYTE",
            "INT16": "SHORT",
            "S16": "SHORT",
            "UINT16": "USHORT",
            "U16": "USHORT",
            "UINT32": "UINT",
            "U32": "UINT",
            "UINT64": "UINT64",
            "U64": "UINT64",
            "INT32": "INT",
            "S32": "INT",
            "INT64": "INT64",
            "S64": "INT64",
            "DOUBLE": "FLOAT64",
            "FLOAT": "FLOAT32",
            "REAL": "FLOAT32",
            "STRING": "STRING",
            "STR": "STRING"
        }

        if name in alias:
            name = alias[name]

        try:
            return getattr(cls, name)
        except AttributeError:
            msg: str = f"Unknown data type: '{name}'"
            logging.getLogger(__name__).warning(msg)
            return None

    @classmethod
    def getDefaultRange(cls, data_type: "Data_Type") -> tuple[int, int]:
        """Return the default ``(min, max)`` raw value range for ``data_type``.

        Used when a CSV row has no explicit ``values`` range.  Signed types get a
        range that includes negative numbers: two's-complement bounds for
        ``SHORT``/``INT``/``INT64`` and signed bit-fields (``_nSBIT``),
        sign-and-magnitude bounds for ``_nSMBIT`` (which cannot represent
        ``-2**(n-1)``), and the largest finite value either side of zero for
        ``FLOAT32``/``FLOAT64``.  Unsigned types, including unsigned bit-fields
        (``_nBIT``: ``0 .. 2**n - 1``), start at 0.  Types without a numeric range
        (strings, flags) keep the historical ``(0, 65535)`` default.
        """
        ranges: dict[Data_Type, tuple[int, int]] = {
            Data_Type.BYTE: (0, 0xFF),
            Data_Type.USHORT: (0, 0xFFFF),
            Data_Type.SHORT: (-(1 << 15), (1 << 15) - 1),
            Data_Type.UINT: (0, 0xFFFFFFFF),
            Data_Type.ACC32: (0, 0xFFFFFFFF),
            Data_Type.INT: (-(1 << 31), (1 << 31) - 1),
            Data_Type.UINT64: (0, 0xFFFFFFFFFFFFFFFF),
            Data_Type.INT64: (-(1 << 63), (1 << 63) - 1),
            Data_Type.FLOAT32: (-_FLOAT32_MAX, _FLOAT32_MAX),
            Data_Type.FLOAT64: (-_FLOAT64_MAX, _FLOAT64_MAX),
        }
        if data_type in ranges:
            return ranges[data_type]

        if 200 < data_type.value < 300:   # unsigned bit-field
            return 0, (1 << (data_type.value - 200)) - 1

        if 300 < data_type.value < 400:   # signed (two's complement) bit-field
            bits: int = data_type.value - 300
            return -(1 << (bits - 1)), (1 << (bits - 1)) - 1

        if data_type.value > 400:         # signed magnitude bit-field
            bits = data_type.value - 400
            limit: int = (1 << (bits - 1)) - 1
            return -limit, limit

        return 0, 0xFFFF

    @classmethod
    def getSize(cls, data_type: "Data_Type") -> int:
        """Return the bit-width of ``data_type``.

        Fixed-width types (``USHORT``, ``UINT``, ``FLOAT32``, etc.) are looked
        up in an explicit table.  Variable-width bit-field types derive their
        width from the enum value: unsigned bits subtract 200, signed bits
        subtract 300, signed-magnitude bits subtract 400.  Returns ``-1`` only
        when none of the above applies, which should never occur for valid members.
        """
        sizes: dict[Data_Type, int] = {
            Data_Type.BYTE: 8,
            Data_Type.USHORT: 16,
            Data_Type.UINT: 32,
            Data_Type.UINT64: 64,
            Data_Type.INT64: 64,
            Data_Type.SHORT: 16,
            Data_Type.INT: 32,
            Data_Type.FLOAT32: 32,
            Data_Type.FLOAT64: 64,
            Data_Type.ACC32: 32,
            Data_Type.STRING16: 16,
            Data_Type.STRING32: 32,
            Data_Type._8BIT_FLAGS: 8,
            Data_Type._16BIT_FLAGS: 16,
            Data_Type._32BIT_FLAGS: 32
        }

        if data_type in sizes:
            return sizes[data_type]

        if data_type.value > 400:   # signed magnitude bits
            return data_type.value - 400

        if data_type.value > 300:   # signed bits
            return data_type.value - 300

        if data_type.value > 200:   # unsigned bits
            return data_type.value - 200

        return -1  # should never happen


class WriteMode(Enum):
    READ = 0x00
    ''' READ ONLY '''
    READDISABLED = 0x01
    ''' DO NOT READ OR WRITE'''
    WRITE = 0x02
    ''' READ AND WRITE '''
    WRITEONLY = 0x03
    ''' WRITE ONLY'''

    @classmethod
    def fromString(cls, name: str) -> "WriteMode":
        """Return the ``WriteMode`` member matching ``name``, defaulting to ``READ``.

        Strips whitespace and uppercases before consulting an alias table that
        maps common shorthands (``rw``, ``r/w``, ``disabled``, ``wo``, etc.)
        to canonical member names.  Any unrecognized string maps silently to
        ``READ``.
        """
        name = name.strip().upper()

        alias: dict[str, str] = {
            "R": "READ",
            "NO": "READ",
            "READ": "READ",
            "WD": "READ",
            "RD": "READDISABLED",
            "READDISABLED": "READDISABLED",
            "DISABLED": "READDISABLED",
            "D": "READDISABLED",
            "R/W": "WRITE",
            "RW": "WRITE",
            "W": "WRITE",
            "YES": "WRITE",
            "WO": "WRITEONLY"
        }

        member_name: str = alias.get(name, "READ")
        return cls[member_name]


class Registry_Type(Enum):
    # for protocols that don't have a command / registry type
    CUSTOM_BUS = 0x00

    COIL = 0x01
    DISCRETE = 0x02
    HOLDING = 0x03
    INPUT = 0x04


@dataclass
class registry_map_entry:
    registry_type: Registry_Type
    register: int
    register_bit: int
    register_bit_end: int
    register_byte: int
    ''' byte offset for canbus etc... '''

    variable_name: str
    documented_name: str
    note: str
    unit: str
    unit_mod: float
    adjustments: dict[str, object]
    concatenate: bool
    concatenate_registers: list[int]
    values: list[int | str] = field(default_factory=lambda: [])
    # For multi-register entries encoded as "low_high[_mid_msb]" in the CSV
    # address field (e.g. "40_41" for a UINT32 pair where 40 is the low word
    # and 41 is the high word).  When populated, the decoder reads exactly these
    # addresses in this order rather than assuming consecutive addresses starting
    # at entry.register.  For standard consecutive entries this list is empty
    # and the legacy start_register + offset path is used unchanged.
    register_list: list[int] = field(default_factory=lambda: [])

    register_key: str = ""
    ''' literal address text for text-keyed protocols (``"register_key_type": "text"`` in the
    protocol JSON), e.g. a VE.Direct label ``"SOC"`` or an OBIS code ``"0:1.8.0"``.  Empty for
    numeric protocols.  ``register`` then holds a stable per-key integer id (0, 1, 2 ... in order
    of first appearance) so integer-keyed machinery keeps working; transports should match on
    ``register_key``, not on ``register``. '''

    value_regex: str = ""

    value_min: int = 0
    ''' min of value range for protocol analyzing'''
    value_max: int = 65535
    ''' max of value range for protocol analyzing'''

    data_type: Data_Type = Data_Type.USHORT
    data_type_size: int = -1
    ''' for non-fixed size types like ASCII'''

    read_command: bytes | None = None
    ''' for transports/protocols that require sending a command on top of "register" '''

    read_interval: float = 1000
    ''' how often to read register in ms'''

    next_read_timestamp: float = 0.0
    ''' unix timestamp in ms '''

    write_mode: WriteMode = WriteMode.READ
    ''' enable disable reading/writing '''

    has_enum_mapping: bool = False
    ''' indicates if this field has enum mappings that should be treated as strings '''

    description_source: str = ""
    ''' variable_name of the source metric when this is a synthetic _desc entry '''

    ha_device_class: str = ""
    ''' optional Home Assistant device_class override ("ha device class" CSV column).
    Blank = infer from the unit; "none" = suppress; anything else is passed through to HA verbatim. '''

    ha_state_class: str = ""
    ''' optional Home Assistant state_class override ("ha state class" CSV column).
    Blank = infer from the unit; "none" = suppress; otherwise measurement / total / total_increasing. '''

    ha_entity_category: str = ""
    ''' optional Home Assistant entity_category ("ha entity category" CSV column): config or diagnostic '''

    dynamic: bool = False
    ''' True when the register address was resolved from a template row (``[anchor+offset]`` and/or ``{i}`` in
    the CSV register cell) rather than written literally.  Runtime-only: it is never read from or written to a CSV,
    so discovered rows cannot be committed back and analysis tools can tell them apart from static rows.  Always
    False for static protocol maps. '''

    def __str__(self) -> str:
        """Return the entry's ``variable_name`` as its string representation."""
        return self.variable_name

    def __eq__(self, other: object) -> bool:
        """Return ``True`` when both entries address the same physical register location.

        Equality is based solely on ``register``, ``register_bit``,
        ``register_bit_end``, ``registry_type``, and ``register_byte`` — not on
        variable or documented names.  Two entries mapping different variable
        names to the same register position compare equal, which is intentional
        for deduplication and range-calculation purposes.
        """
        return (
            isinstance(other, registry_map_entry)
            and self.register == other.register
            and self.register_bit == other.register_bit
            and self.register_bit_end == other.register_bit_end
            and self.registry_type == other.registry_type
            and self.register_byte == other.register_byte
        )

    def __hash__(self) -> int:
        """Hash based on ``variable_name``, ``register_bit``, ``register_byte``, and ``registry_type``."""
        return hash((self.variable_name, self.register_bit, self.register_byte, self.registry_type))


@dataclass(frozen=True)
class WordOrder:
    """Two independent axes that fully describe how a manufacturer encodes a
    multi-register (32- or 64-bit) value over Modbus.

    Modbus transmits each 16-bit register in big-endian byte order on the wire.
    For values that span more than one register, manufacturers have adopted four
    distinct encoding conventions, corresponding to all combinations of these
    two boolean axes:

    +---------------------------------+--------------+----------------+------------------+
    | Canonical CSV name              | word_reversed| bytes_reversed | Byte sequence    |
    +=================================+==============+================+==================+
    | big_endian-ABCD                 | False        | False          | ABCD (default)   |
    | big_endian_byte_swap-BADC       | False        | True           | BADC             |
    | little_endian-CDAB              | True         | False          | CDAB             |
    | little_endian_byte_swap-DCBA    | True         | True           | DCBA             |
    +---------------------------------+--------------+----------------+------------------+

    ``word_reversed``
        True  → the low-significance word is stored at the *lower* register
                address (CDAB / DCBA).
        False → the high-significance word is stored at the *lower* register
                address (ABCD / BADC) — standard Modbus convention.

    ``bytes_reversed``
        True  → the two bytes *within* each 16-bit register are stored in
                little-endian order (BADC / DCBA).
        False → bytes within each register are in big-endian order (ABCD /
                CDAB) — standard Modbus wire format.

    These two axes are completely independent; controlling them separately
    makes it impossible to accidentally produce a fifth, invalid combination.
    """
    word_reversed: bool
    bytes_reversed: bool


# ---------------------------------------------------------------------------
# Pre-built WordOrder singletons — imported or referenced throughout this
# module so callers never construct raw WordOrder(False, False) inline.
# ---------------------------------------------------------------------------
WORD_ORDER_ABCD = WordOrder(word_reversed=False, bytes_reversed=False)
"""Big-endian, no byte swap — standard Modbus, SunSpec default."""

WORD_ORDER_BADC = WordOrder(word_reversed=False, bytes_reversed=True)
"""High word first, bytes within each word reversed."""

WORD_ORDER_CDAB = WordOrder(word_reversed=True, bytes_reversed=False)
"""Low word at lower address, bytes within each word big-endian.
Most common 'little-endian' Modbus convention (EG4, many inverters)."""

WORD_ORDER_DCBA = WordOrder(word_reversed=True, bytes_reversed=True)
"""Fully reversed — least-significant byte first (Intel/x86 convention)."""

_DEFAULT_WORD_ORDER: WordOrder = WORD_ORDER_ABCD
"""Protocol-level default when no ``Register_Endian`` adjustment is present."""

# ---------------------------------------------------------------------------
# Alias table — maps every accepted CSV string to a WordOrder singleton.
# Keys must be lowercase; the lookup always calls .strip().lower() first.
# ---------------------------------------------------------------------------
_WORD_ORDER_ALIASES: dict[str, WordOrder] = {
    # Canonical four-part names (CSV canonical form)
    "big_endian-abcd":              WORD_ORDER_ABCD,
    "big_endian_byte_swap-badc":    WORD_ORDER_BADC,
    "little_endian-cdab":           WORD_ORDER_CDAB,
    "little_endian_byte_swap-dcba": WORD_ORDER_DCBA,
    # Short mnemonic aliases
    "big_endian":                   WORD_ORDER_ABCD,
    "big_endian_byte_swap":         WORD_ORDER_BADC,
    "little_endian":                WORD_ORDER_CDAB,
    "little_endian_byte_swap":      WORD_ORDER_DCBA,
    # Byte-sequence mnemonics (accepted as values in their own right)
    "abcd":                         WORD_ORDER_ABCD,
    "badc":                         WORD_ORDER_BADC,
    "cdab":                         WORD_ORDER_CDAB,
    "dcba":                         WORD_ORDER_DCBA,
    # Legacy two-value shorthands — kept for backward compatibility
    "big":                          WORD_ORDER_ABCD,
    "be":                           WORD_ORDER_ABCD,
    "little":                       WORD_ORDER_CDAB,
    "le":                           WORD_ORDER_CDAB,
}


@dataclass(frozen=True)
class AdjustmentValidation:
    """Result of checking an ``adjustments`` cell (see ``DataAdjustments.validate_adjustments``).

    ``errors`` make the value unusable (it would be ignored, or fail, when MPG loads the map);
    ``warnings`` are legal but probably unintended; ``notes`` are information only (for example which
    JSON file defines a context variable the formula uses).  ``normalized`` is the canonical one-line
    JSON to store (``""`` for an empty cell) and is only meaningful when ``ok``.
    """

    ok: bool
    errors: list[str]
    warnings: list[str]
    normalized: str
    notes: list[str] = field(default_factory=lambda: [])


@dataclass
class _PreviewEntry:
    """The three registry-entry attributes ``apply_adjustments`` reads, for previews without a loaded map."""

    adjustments: dict[str, object]
    variable_name: str = "preview"
    unit_mod: float = 1.0


class DataAdjustments:
    """Parses, queries, and applies CSV-driven adjustments for registry entries.

    Construct once per ``protocol_settings`` instance, passing the protocol's
    default ``WordOrder`` and the shared logger.  The word order stored here is
    the protocol-level default; individual entries may override it via the
    ``Register_Endian`` adjustment key in the CSV.

    All public methods are pure with respect to external state — they read only
    from the supplied ``entry.adjustments`` dict and the value being transformed.
    Adding a new adjustment type (a new stage or a new key such as Scale_Factor
    or Clamp) should require changes only within this class.

    Multi-register word ordering
    ----------------------------
    Modbus transmits each 16-bit register in big-endian byte order on the wire.
    For values that span two or more registers (32- or 64-bit types), the
    protocol does not define a standard word order, so manufacturers have adopted
    four conventions described by two independent boolean axes (see ``WordOrder``).

    The CSV ``adjustments`` column accepts the following ``Register_Endian``
    values (case-insensitive):

    +---------------------------------+------------------+
    | CSV value                       | Encoding         |
    +=================================+==================+
    | ``big_endian-ABCD``             | ABCD — default   |
    | ``big_endian_byte_swap-BADC``   | BADC             |
    | ``little_endian-CDAB``          | CDAB             |
    | ``little_endian_byte_swap-DCBA``| DCBA             |
    +---------------------------------+------------------+

    Short aliases ``big``, ``little``, ``be``, ``le``, ``abcd``, ``badc``,
    ``cdab``, ``dcba``, and the canonical names without the byte-sequence
    suffix are also accepted.  ``"little"`` maps to CDAB (low word at lower
    address, bytes within each word big-endian) which is the most common
    little-endian Modbus convention used by EG4 and many other inverter
    manufacturers.

    Adjustment reference
    --------------------
    The CSV ``adjustments`` column holds one JSON object (or a ``Key:value`` shorthand).
    Four keys are recognized, matched case-insensitively.  Everything else in the object
    is ignored.  ``unit_mod`` (the numeric prefix of the ``unit`` column, e.g. ``0.1V``) is
    not an adjustment key but is applied in the same stage, so it is listed for reference.

    ``Register_Endian`` -- stage ``byteorder``
        Input:    a word-order name or alias as a string, e.g. ``"little_endian-CDAB"``,
                  ``"little"``, ``"cdab"`` (full list above).
        Output:   the ``WordOrder`` used to assemble this row's registers before decoding.
                  Unknown values log a warning and fall back to the protocol default.
        Use case: a vendor that sends 32/64-bit values low word first while the rest of the
                  protocol is high word first, or the reverse.  Only affects rows that span
                  more than one register.
        Example:  ``{"Register_Endian": "little_endian-CDAB"}``

    ``High_Low`` -- stage ``post_decode``
        Input:    a string of ``x?(lo,hi]->formula`` blocks, separated by spaces.  A block
                  matches when ``lo < x <= hi`` (lower bound exclusive, upper inclusive), where
                  ``x`` is the decoded value.  The first matching block wins.
        Output:   the matching block's formula evaluated with ``x`` substituted.  If no block
                  matches (including ``x == 0`` for an ``(0,...`` block) or the formula fails,
                  the decoded value is returned unchanged.  When present it replaces ``unit_mod``
                  and ``Offset`` for the row: the formula is the whole transform.
        Use case: value ranges that decode differently, such as signed values stored in an
                  unsigned register (``x?(32768,65535]->x-65536``) or an offset scale such as
                  tenths of a kelvin to Celsius.
        Example:  ``{"High_Low": "x?(0,65535]->(x-2731)/10"}``

    ``Offset`` -- stage ``post_decode``
        Input:    a number (JSON number or numeric string).
        Output:   decoded value * ``unit_mod`` + offset.  Skipped when ``High_Low`` is present.
                  A non-numeric offset logs a warning and is ignored.
        Use case: a constant bias in the register, e.g. temperature stored with a fixed -40 shift.
        Example:  ``{"Offset": -40}`` or the shorthand ``Offset:-40``

    ``Context`` -- stage ``context`` (after every value of the read has been decoded)
        Input:    an object choosing one formula, which is then evaluated with ``x`` = the row's
                  value (after ``unit_mod``/``Offset``).  Ways to choose it:

                  * ``{"formula": "x*{a}/{b}"}``: no ``key``; always applies.
                  * ``{"key": "name", "cases": {"0": "x", "1": "-x"}, "default": "x"}``: the decoded
                    value of variable ``name`` picks the formula.  A cases key may list several
                    values (``"333,80,100"``).  ``default`` applies when no case matches.
                  * ``{"key": "name", "range": [-10, 10]}``: for an integer ``name`` in the range the
                    formula is ``x*10**n`` (``x/10**n`` for negative n), the SunSpec scale-factor pattern.

                  A formula may contain ``{name}``: the current value of another decoded variable,
                  or of a protocol ``context_variables`` entry (see ``evaluate_context_variables``).
                  Arithmetic is ``+ - * / // % **``, parentheses and unary minus; no names or calls.
        Output:   the formula's result (an ``int`` when it is a whole number).  The raw value is
                  returned unchanged when no formula applies, the ``key`` or any ``{name}`` was not
                  read in this pass, the value is not numeric, or evaluation fails (logged).
        Use case: a scale or sign that depends on another register: SunSpec scale factors, a
                  direction flag that flips the sign, a meter whose units follow a mode register,
                  instrument-transformer ratios.
        Example:  ``{"Context": {"key": "power_direction", "cases": {"1": "-x"}, "default": "x"}}``

    Order within a read: ``Register_Endian`` (decode) -> ``High_Low`` or ``unit_mod`` + ``Offset``
    -> ``Context``.  Rows are adjusted in registry order and a ``Context`` row sees the already
    adjusted values of earlier rows, which is why a row used as a ``key`` or ``{name}`` should itself
    carry no ``Context``.
    """

    # Context ``range`` support (see ``context_range_bounds``).  A power of ten beyond +/-50 is never a real scale factor.
    CONTEXT_RANGE_LIMIT: ClassVar[int] = 50
    _CONTEXT_RANGE_TEXT: ClassVar[re.Pattern[str]] = re.compile(r"^\s*(-?\d+)\s*(?:~|\.\.|,|-)\s*(-?\d+)\s*$")
    _context_range_warned: ClassVar[set[str]] = set()      # invalid ranges already reported, so each is logged once per process

    # Adjustment keys MPG acts on, in the order the editor offers them, and the Context sub-keys it reads.
    KNOWN_ADJUSTMENTS: ClassVar[tuple[str, ...]] = ("Register_Endian", "High_Low", "Offset", "Context")
    CONTEXT_KEYS: ClassVar[tuple[str, ...]] = ("formula", "key", "cases", "range", "default")
    REGISTER_ENDIAN_CHOICES: ClassVar[tuple[str, ...]] = (
        "big_endian-ABCD", "big_endian_byte_swap-BADC", "little_endian-CDAB", "little_endian_byte_swap-DCBA",
    )
    _FORMULA_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    _HIGH_LOW_BLOCK_RE: ClassVar[re.Pattern[str]] = re.compile(r"x\?[\(\[]([\-0-9.]+),([\-0-9.]+)[\)\]]->(.+?)(?=x\?|$)")
    _VARIABLE_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(
        self,
        log: logging.Logger,
        default_word_order: WordOrder = _DEFAULT_WORD_ORDER,
        context_variables: Mapping[str, object] | None = None,
    ) -> None:
        """Initialise with a shared logger and the protocol-level default ``WordOrder``.

        ``default_word_order`` is resolved from the JSON settings file's
        ``byteorder`` key (via ``_WORD_ORDER_ALIASES``) so it always reflects
        the correct protocol default (almost always ``WORD_ORDER_ABCD`` for
        standard Modbus).  Individual entries may override it via
        ``Register_Endian`` in their adjustments column.
        """
        self._log: logging.Logger = log
        self.default_word_order: WordOrder = default_word_order
        # Protocol-level named expressions (the JSON file's "context_variables" object), usable as
        # {name} in any Context formula.  See evaluate_context_variables.
        self.context_variables: dict[str, object] = dict(context_variables) if context_variables else {}

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_adjustments(self, raw_adjustments: str | None) -> dict[str, object]:
        """Parse the CSV adjustments field into a plain dict.

        Canonical form is a JSON object.  Simple shorthand such as
        ``"Offset:-50"`` is also accepted.
        """
        text: str = (raw_adjustments or "").strip()
        if not text:
            return {}

        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return cast(dict[str, object], parsed)
                else:
                    self._log.warning(f"Ignoring non-object adjustments JSON: {text}")
                    return {}
            except json.JSONDecodeError as e:
                self._log.warning(f"Invalid adjustments JSON '{text}': {e}")
                return {}

        key, sep, value = text.partition(":")
        if not sep:
            self._log.warning(f"Ignoring malformed adjustments field: {text}")
            return {}

        key: str = key.strip()
        value: str = value.strip()
        try:
            return {key: json.loads(value)}
        except json.JSONDecodeError:
            return {key: value}

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def get_adjustment(self, entry: "registry_map_entry", name: str) -> object | None:
        """Return the value for *name* from ``entry.adjustments`` (case-insensitive)."""
        target: str = name.lower()
        for key, value in entry.adjustments.items():
            if key.lower() == target:
                return value
        return None

    def get_entry_byteorder(self, entry: "registry_map_entry") -> WordOrder:
        """Return the effective ``WordOrder`` for *entry* (the ``Register_Endian`` adjustment).

        Input:    ``entry.adjustments["Register_Endian"]``, a string (optional).
        Output:   the matching ``WordOrder``; the protocol default when the key is absent, and the
                  default plus a logged warning when the value is not a known name or alias.
        Use case: a row (or protocol) whose multi-register values do not use the default ABCD
                  order.  Single-register rows are unaffected.

        Defaults to ``self.default_word_order`` (set at construction time from
        the protocol-level ``byteorder`` setting).  Can be overridden per-entry
        via ``Register_Endian`` in the CSV adjustments column.

        Accepted values (case-insensitive) — see ``_WORD_ORDER_ALIASES`` for
        the complete mapping:

        * ``big_endian-ABCD``              → ABCD (default, standard Modbus)
        * ``big_endian_byte_swap-BADC``    → BADC
        * ``little_endian-CDAB``           → CDAB (EG4 / most inverters)
        * ``little_endian_byte_swap-DCBA`` → DCBA
        * Short aliases: ``big``/``be`` → ABCD, ``little``/``le`` → CDAB,
          ``abcd``, ``badc``, ``cdab``, ``dcba``
        """
        if entry.adjustments:
            endian: object | None = self.get_adjustment(entry, "Register_Endian")
            if endian is not None:
                endian_str: str = str(endian).strip().lower()
                word_order: WordOrder | None = _WORD_ORDER_ALIASES.get(endian_str)
                if word_order is not None:
                    return word_order
                self._log.warning(
                    f"Unsupported Register_Endian '{endian}' for "
                    f"{entry.variable_name} — using protocol default. "
                    f"Valid values: {list(_WORD_ORDER_ALIASES.keys())}"
                )
        return self.default_word_order

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def apply_adjustments(
        self,
        value: int | float | str,
        entry: "registry_map_entry",
        stage: Literal["byteorder", "post_decode", "context"],
        context: Mapping[str, int | float | str] | None = None,
    ) -> int | float | str:
        """Central adjustment dispatcher for CSV-driven post-processing.

        Inputs: ``value`` (the decoded or partly adjusted value), the registry ``entry`` whose
        ``adjustments`` dict drives the transform, the ``stage`` to run and, for ``context``
        only, ``context`` (the decoded values of this read).  Output: the adjusted value, or
        ``value`` unchanged when no adjustment of that stage applies.  The full definition of each
        adjustment key (input, output, use case, example) is in the class docstring.

        Stages
        ------
        byteorder
            Determine byte order before raw bytes are decoded.
            Only relevant when the entry carries a ``Register_Endian``
            adjustment.  Handled by ``get_entry_byteorder``; this stage returns ``value`` as is.
        post_decode
            Apply ``unit_mod`` scaling and any numeric adjustments
            (``Offset``, ``High_Low``) after the raw value has been decoded.
            ``unit_mod`` is **always** applied here regardless of whether
            other adjustments are present, except that ``High_Low`` replaces both
            ``unit_mod`` and ``Offset``.  Non-numeric values pass through.  A whole-number
            float is returned as ``int``.
        context
            Apply conditional transforms that depend on sibling metric values
            (e.g. direction-aware sign flipping, SunSpec scale factors, PT/CT ratios).
            Only the ``Context`` key is used.  Numeric values only; see the class docstring.
        """

        # ------------------------------------------------------------------
        # byteorder stage
        # ------------------------------------------------------------------
        if stage == "byteorder":
            # The byteorder stage is handled directly by get_entry_byteorder();
            # callers that need a WordOrder should call that method instead.
            # This branch is retained for any legacy callers that go through
            # apply_adjustments; it simply delegates and returns the WordOrder
            # cast to str so the method signature (int|float|str) is satisfied.
            # In practice no code path calls apply_adjustments with stage="byteorder"
            # any longer — word-order resolution happens entirely in
            # get_entry_byteorder() which is called directly by the decode methods.
            return value

        # ------------------------------------------------------------------
        # post_decode stage
        # unit_mod must always be applied; it is independent of adjustments.
        # ------------------------------------------------------------------
        if stage == "post_decode":
            if not isinstance(value, (int, float)):
                return value

            adjusted: int | float = value

            # High_Low encodes the complete scaling formula (e.g. x/1000),
            # so unit_mod must NOT also be applied — the formula is the
            # complete transform.
            high_low: object | None = self.get_adjustment(entry, "High_Low") if entry.adjustments else None
            if high_low is not None:
                adjusted = self.apply_range_formula(float(adjusted), str(high_low))
            else:
                if entry.unit_mod != 1.0:
                    adjusted = adjusted * entry.unit_mod

                if entry.adjustments:
                    offset: object | None = self.get_adjustment(entry, "Offset")
                    if offset is not None:
                        try:
                            adjusted = adjusted + float(cast(str | int | float, offset))
                        except (TypeError, ValueError):
                            self._log.warning(f"Unsupported Offset adjustment '{offset}' for {entry.variable_name}")

            if isinstance(adjusted, float) and adjusted.is_integer():
                return int(adjusted)
            return adjusted

        # ------------------------------------------------------------------
        # context stage
        # Context transforms are opt-in and only apply when an explicit
        # Context adjustment is defined.
        # ------------------------------------------------------------------
        if stage == "context":
            if not entry.adjustments:
                return value
            if context is None or not isinstance(value, (int, float)):
                return value

            context_adjustment: object | None = self.get_adjustment(entry, "Context")
            if not isinstance(context_adjustment, dict):
                return value
            context_adjustment = cast(dict[str, object], context_adjustment)

            formula: str = self.select_context_formula(entry.variable_name, context_adjustment, context)
            if not formula:
                return value

            # {name} in a formula stands for the current value of another decoded variable or a
            # protocol "context_variables" entry (e.g. {pt1}/{pt2}).  If one of them is unavailable
            # in this pass the raw value is kept.
            if "{" in formula:
                substituted: str | None = self.substitute_context_variables(formula, context)
                if substituted is None:
                    self._log.debug(
                        f"Context adjustment for {entry.variable_name} waiting for a variable used in '{formula}'"
                    )
                    return value
                formula = substituted

            expression: str = formula.replace("x", str(float(value)))
            try:
                adjusted_context: int | float = self.safe_eval_expression(expression)
                if isinstance(adjusted_context, float) and adjusted_context.is_integer():
                    return int(adjusted_context)
                else:
                    return adjusted_context
            except Exception as e:
                self._log.warning(f"Failed context adjustment '{formula}' for {entry.variable_name}: {e}")
                return value

        return value  # unreachable but satisfies type checker

    # ------------------------------------------------------------------
    # Context scale-factor helpers
    # ------------------------------------------------------------------

    @classmethod
    def context_range_bounds(cls, spec: object) -> tuple[int, int] | None:
        """Parse the ``range`` of a ``Context`` adjustment into ordered inclusive ``(low, high)`` integers.

        Accepts a two-item JSON list (``[-10, 10]``) or text (``"-10~10"``, ``"-10..10"``, ``"-10,10"``, ``"-10--5"``).
        Reversed bounds are put in order.  Returns None for anything else, or when a bound is outside
        +/-``CONTEXT_RANGE_LIMIT`` (a power of ten that large is never a real scale factor).
        """
        low: int
        high: int
        if isinstance(spec, (list, tuple)):
            items: list[object] = list(cast("list[object]", spec))
            if len(items) != 2:
                return None
            try:
                low, high = int(cast("int | str", items[0])), int(cast("int | str", items[1]))
            except (TypeError, ValueError):
                return None
        elif isinstance(spec, str):
            match: re.Match[str] | None = cls._CONTEXT_RANGE_TEXT.match(spec)
            if match is None:
                return None
            low, high = int(match.group(1)), int(match.group(2))
        else:
            return None
        if low > high:
            low, high = high, low
        if max(abs(low), abs(high)) > cls.CONTEXT_RANGE_LIMIT:
            return None
        return low, high

    @staticmethod
    def context_power_of_ten_formula(exponent: int) -> str:
        """The formula a ``Context`` range gives for ``exponent``: ``x/10**n`` below zero, ``x*1`` at zero, ``x*10**n`` above."""
        if exponent < 0:
            return f"x/{10 ** -exponent}"
        if exponent == 0:
            return "x*1"
        return f"x*{10 ** exponent}"

    def apply_range_formula(self, raw_value: float, logic: str) -> float:
        """Apply conditional range formulas based on *raw_value* (the ``High_Low`` adjustment).

        Input:    ``raw_value`` (the decoded number) and ``logic``, blocks of the form
                  ``x?(lo,hi)->formula`` (either bracket style).  A block matches when
                  ``lo < raw_value <= hi``; the first match is used.
        Output:   the matching formula evaluated with ``x`` = ``raw_value``, as a float.  Returns
                  ``raw_value`` unchanged when no block matches (note ``0`` never matches a block
                  that starts at ``(0``) or when evaluation fails (logged as a warning).
        Use case: ranges that decode differently, e.g. two's-complement from an unsigned register:
                  ``x?(0,32768]->x x?(32768,65535)->x-65536``; or a unit conversion that also
                  needs an offset: ``x?(0,65535]->(x-2731)/10``.
        """
        cleaned_logic: str = logic.replace(" ", "")

        pattern = (
            r"x\?\s*[\(\[]\s*([\-0-9.]+)\s*,\s*([\-0-9.]+)\s*[\)\]]\s*->\s*(.+?)(?=x\?|$)"
        )
        logic_blocks: list[Any] = re.findall(pattern, cleaned_logic)

        for lower, upper, formula in logic_blocks:
            lower_f = float(lower)
            upper_f = float(upper)

            if lower_f < raw_value <= upper_f:
                expression: str = formula.replace("x", str(raw_value))
                try:
                    evaluated: int | float = self.safe_eval_expression(expression)
                    self._log.debug(f"Successful range formula evaluation {evaluated} for value {raw_value}")
                    return float(evaluated)
                except Exception as e:
                    self._log.warning(f"Failed range formula evaluation {expression} for value {raw_value}: {e}")
                    return raw_value

        return raw_value

    @staticmethod
    def match_context_case(cases: Mapping[object, object], value: int | float | str) -> str:
        """Return the formula whose ``cases`` key matches ``value``.

        Input:    ``cases`` (a Context ``cases`` object, formula strings by key) and ``value``, the
                  key variable's decoded value.  A whole-number float is matched as an integer.
        Output:   the matching formula, or ``""`` when nothing matches.  An exact key wins over a
                  comma-separated group key.
        Use case: listing one formula for several values (``"333,80,100,200"``) instead of
                  repeating it once per value.
        """
        if isinstance(value, float) and value.is_integer():
            value = int(value)                       # a scale factor read as 2.0 matches case "2"
        exact: object | None = cases.get(str(value), cases.get(value))
        if exact is not None:
            return str(exact)
        wanted: str = str(value)
        for case_key, case_formula in cases.items():
            if isinstance(case_key, str) and "," in case_key and wanted in (part.strip() for part in case_key.split(",")):
                return str(case_formula)
        return ""

    def select_context_formula(self, label: str, spec: Mapping[str, object], context: Mapping[str, int | float | str]) -> str:
        """Choose the formula a Context spec yields for ``context``; ``""`` means leave the value alone.

        Input:    ``label`` (variable name, for log messages), ``spec`` (a row's Context object, or one
                  ``context_variables`` entry) and ``context`` (decoded values of this read).
        Output:   the selected formula text, still containing any ``{name}`` placeholders, or ``""``
                  when none applies (no match and no ``default``, or the ``key`` was not read).
        Use case: the shared choice step of a row's ``Context`` adjustment and of a protocol
                  ``context_variables`` entry.

        A ``formula`` with no ``key`` always applies.  Otherwise ``key`` names a decoded variable whose
        value picks a formula from ``cases`` (comma-grouped keys allowed), then ``range`` (powers of ten),
        then ``default``.
        """
        key: str = str(spec.get("key", "")).strip()
        always_formula: str = str(spec.get("formula", "")).strip()
        if not key:
            return always_formula
        if key not in context:
            self._log.debug(f"Context adjustment for {label} waiting for key '{key}'")
            return ""
        context_value: int | float | str = context[key]
        if isinstance(context_value, float) and context_value.is_integer():
            context_value = int(context_value)
        formula: str = ""
        cases: object = spec.get("cases", {})
        if isinstance(cases, dict):
            formula = self.match_context_case(cast(dict[object, object], cases), context_value)
        if not formula and "range" in spec:
            # "range": [-10, 10] -> 10**k scaling for every integer k in the range (explicit cases win)
            bounds: tuple[int, int] | None = self.context_range_bounds(spec["range"])
            if bounds is None:
                bad: str = f"{label}:{spec['range']!r}"
                if bad not in self._context_range_warned:
                    self._context_range_warned.add(bad)
                    self._log.warning(
                        f"Ignoring invalid Context range {spec['range']!r} for {label} "
                        f"(use [low, high] or \"low~high\", each within +/-{self.CONTEXT_RANGE_LIMIT})"
                    )
            elif isinstance(context_value, int) and bounds[0] <= context_value <= bounds[1]:
                formula = self.context_power_of_ten_formula(context_value)
        return formula or str(spec.get("default", ""))

    def evaluate_context_variables(self, context: Mapping[str, int | float | str]) -> dict[str, float]:
        """Evaluate the protocol's ``context_variables`` against ``context`` (the decoded values).

        Input:    ``context``, the decoded values of this read.  The definitions come from the protocol
                  JSON's ``context_variables`` object: name -> expression string, or a Context spec
                  (``key``/``cases``/``default``).  An entry may use ``{name}`` for decoded variables and
                  for entries defined above it.
        Output:   a dict of name -> float for every entry that could be evaluated.  An entry whose inputs
                  were not read in this pass (or that fails to evaluate, logged) is left out, so rows that
                  use it keep their raw value.  The values are not published as variables.
        Use case: one long scale formula shared by many rows, such as the Acuvim II primary/secondary
                  PT/CT scale, written once in the JSON instead of in every CSV row.
        Example:  ``{"voltage_scale": "{mode}+(1-{mode})*{pt1}/{pt2}"}`` then, in a row,
                  ``{"Context": {"formula": "x*{voltage_scale}"}}``
        """
        derived: dict[str, float] = {}
        for name, definition in self.context_variables.items():
            spec: Mapping[str, object]
            if isinstance(definition, str):
                spec = {"formula": definition}
            elif isinstance(definition, dict):
                spec = cast(dict[str, object], definition)
            else:
                continue
            view: Mapping[str, int | float | str] = ChainMap(cast(dict[str, int | float | str], derived), cast(dict[str, int | float | str], context))
            formula: str = self.select_context_formula(name, spec, view)
            if not formula:
                continue
            if "{" in formula:
                substituted: str | None = self.substitute_context_variables(formula, view)
                if substituted is None:
                    continue
                formula = substituted
            try:
                derived[name] = float(self.safe_eval_expression(formula))
            except Exception as exc:
                self._log.warning(f"Failed context variable '{name}' ({formula}): {exc}")
        return derived

    _CONTEXT_VARIABLE_RE: ClassVar[re.Pattern[str]] = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    @classmethod
    def substitute_context_variables(cls, formula: str, context: Mapping[str, int | float | str]) -> str | None:
        """Replace each ``{name}`` in ``formula`` with the numeric value of the decoded variable ``name``.

        Input:    ``formula`` text possibly containing ``{name}`` placeholders, and ``context`` (decoded
                  values and derived ``context_variables``).
        Output:   the formula with every placeholder replaced by ``(<value>)``, or ``None`` when a named
                  variable is absent from ``context`` or is not a number (text values and booleans are
                  rejected), so the caller can leave the raw value alone.
        Use case: letting a Context formula use another register, e.g. ``x*{pt1}/{pt2}``.

        Values are inserted in parentheses so a negative value cannot change the meaning of the
        expression.
        """
        missing: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            name: str = match.group(1)
            found: int | float | str | None = context.get(name)
            if isinstance(found, bool) or not isinstance(found, (int, float)):
                missing.append(name)
                return ""
            return f"({float(found)!r})"

        result: str = cls._CONTEXT_VARIABLE_RE.sub(_replace, formula)
        return None if missing else result

    # ------------------------------------------------------------------
    # Validation and preview (used by the protocol editor's adjustments popup)
    # ------------------------------------------------------------------

    def check_formula(self, formula: str, *, allow_names: bool) -> tuple[str, list[str]]:
        """Check one formula the way the loader will read it.

        Input:    ``formula`` text and whether ``{name}`` placeholders are allowed (``High_Low`` formulas
                  cannot use them; ``Context`` formulas can).
        Output:   ``(error, names)``: ``error`` is ``""`` when the formula is valid, otherwise a message
                  for the user; ``names`` lists the ``{name}`` placeholders found.
        Use case: rejecting a typo (unbalanced bracket, unsupported operator, ``2x`` instead of ``2*x``)
                  before it is saved, instead of finding it as a runtime warning.

        Placeholders and ``x`` are replaced by ``1`` and the result is parsed with ``safe_eval_expression``,
        so exactly the operators MPG supports are accepted.  A division by zero in the probe is not an error.
        """
        if not formula.strip():
            return "the formula is empty", []
        names: list[str] = self._FORMULA_NAME_RE.findall(formula)
        if names and not allow_names:
            return "{name} variables cannot be used here", names
        probe: str = self._FORMULA_NAME_RE.sub("1", formula)
        if "{" in probe or "}" in probe:
            return "unmatched { or } (a variable is written {name} with letters, digits and underscores)", names
        if re.search(r"[0-9.)]\s*x|x\s*[0-9.(]|x\s*x", probe):
            return "put an operator next to x (for example 2*x, not 2x)", names
        try:
            self.safe_eval_expression(probe.replace("x", "1"))
        except ZeroDivisionError:
            pass
        except (SyntaxError, ValueError, TypeError) as exc:
            return f"not a valid arithmetic expression ({exc}); allowed: numbers, x, {{name}}, + - * / // % ** and parentheses", names
        return "", names

    def validate_adjustments(
        self,
        text: str | None,
        known_variables: Collection[str] = (),
        context_variables: Collection[str] = (),
        context_definitions: Mapping[str, object] | None = None,
        context_source: str = "",
    ) -> AdjustmentValidation:
        """Check an ``adjustments`` cell and return its canonical form.

        Input:    ``text`` (the cell: a JSON object, or the ``Key:value`` shorthand, or empty), the variable
                  names of the protocol (``known_variables``) and its ``context_variables`` entries.  The two
                  collections only drive "unknown variable" warnings and may be empty.  ``context_definitions``
                  (name -> definition) and ``context_source`` (a label for the JSON file that holds them) let the
                  result explain where a context variable used by the cell comes from.
        Output:   an ``AdjustmentValidation``.  Errors: invalid JSON, duplicate keys, a recognized key with a
                  value MPG cannot use, an invalid formula.  Warnings: a key MPG does not use (it is kept), a
                  ``{name}`` or ``key`` that is not a known variable, overlapping ``High_Low`` ranges, a
                  setting that another one overrides.  Notes: each context variable the cell uses, with the
                  JSON file that defines it and its definition.  ``normalized`` is one-line JSON with
                  recognized key names in canonical case.
        Use case: guided editing without narrowing it too far: the four recognized adjustments are checked
                  strictly, anything else is allowed but flagged.
        """
        errors: list[str] = []
        warnings: list[str] = []
        stripped: str = (text or "").strip()
        if not stripped:
            return AdjustmentValidation(True, [], [], "")  # noqa: FBT003

        parsed: object
        if stripped.startswith("{"):
            def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
                seen: dict[str, object] = {}
                for key, value in pairs:
                    if key in seen:
                        msg: str = f"duplicate key '{key}'"
                        raise ValueError(msg)
                    seen[key] = value
                return seen

            try:
                parsed = json.loads(stripped, object_pairs_hook=_no_duplicates)
            except ValueError as exc:
                return AdjustmentValidation(False, [f"Invalid JSON: {exc}"], [], stripped)  # noqa: FBT003
        else:
            key_part, separator, value_part = stripped.partition(":")
            if not separator or not key_part.strip():
                return AdjustmentValidation(False, ["Expected a JSON object such as {\"Offset\": -40} or the shorthand Offset:-40"], [], stripped)  # noqa: FBT003
            try:
                parsed = {key_part.strip(): json.loads(value_part.strip())}
            except json.JSONDecodeError:
                parsed = {key_part.strip(): value_part.strip()}
        if not isinstance(parsed, dict):
            return AdjustmentValidation(False, ["The adjustments must be a JSON object ({...})"], [], stripped)  # noqa: FBT003

        canonical: dict[str, object] = {}
        lowered: dict[str, str] = {name.lower(): name for name in self.KNOWN_ADJUSTMENTS}
        for raw_key, value in cast(dict[str, object], parsed).items():
            name: str = lowered.get(raw_key.strip().lower(), raw_key.strip())
            if not name:
                errors.append("A key name is empty")
                continue
            if name in canonical:
                errors.append(f"'{name}' is given twice (key names are not case-sensitive)")
                continue
            canonical[name] = value

        known: set[str] = set(known_variables) | set(context_variables)
        notes: list[str] = []
        for name, value in canonical.items():
            if name == "Register_Endian":
                canonical[name] = self._check_register_endian(value, errors)
            elif name == "Offset":
                canonical[name] = self._check_offset(value, errors)
            elif name == "High_Low":
                self._check_high_low(value, errors, warnings)
            elif name == "Context":
                self._check_context(
                    value, known, bool(known_variables or context_variables), errors, warnings,
                    notes, set(known_variables), dict(context_definitions or {}), context_source,
                )
            else:
                warnings.append(f"'{name}' is not an adjustment MPG uses; it is kept as is but has no effect")
        if "High_Low" in canonical and "Offset" in canonical:
            warnings.append("Offset is ignored when High_Low is present (High_Low is the whole transform)")

        normalized: str = json.dumps(canonical, ensure_ascii=False)
        return AdjustmentValidation(not errors, errors, warnings, normalized, notes)

    def _check_register_endian(self, value: object, errors: list[str]) -> object:
        """Validate ``Register_Endian``; returns the value to store."""
        if not isinstance(value, str) or value.strip().lower() not in _WORD_ORDER_ALIASES:
            errors.append(
                f"Register_Endian must be one of {', '.join(self.REGISTER_ENDIAN_CHOICES)} (or an alias such as little, cdab)"
            )
            return value
        return value.strip()

    def _check_offset(self, value: object, errors: list[str]) -> object:
        """Validate ``Offset``; returns the number to store (a numeric string becomes a number)."""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors.append("Offset must be a number")
            return value
        try:
            number: float = float(value)
        except ValueError:
            errors.append(f"Offset must be a number, not '{value}'")
            return value
        if not math.isfinite(number):
            errors.append("Offset must be a finite number")
            return value
        return int(number) if number.is_integer() and not isinstance(value, float) else number

    def _check_high_low(self, value: object, errors: list[str], warnings: list[str]) -> None:
        """Validate ``High_Low``: blocks of ``x?(lo,hi]->formula`` with ``lo < x <= hi`` semantics."""
        if not isinstance(value, str):
            errors.append("High_Low must be text such as x?(0,32768]->x x?(32768,65535]->x-65536")
            return
        cleaned: str = value.replace(" ", "")
        blocks: list[re.Match[str]] = list(self._HIGH_LOW_BLOCK_RE.finditer(cleaned))
        if not blocks:
            errors.append("High_Low needs at least one block of the form x?(lower,upper]->formula")
            return
        if "".join(block.group(0) for block in blocks) != cleaned:
            errors.append("High_Low has text outside its x?(lower,upper]->formula blocks")
        spans: list[tuple[float, float]] = []
        for number, block in enumerate(blocks, start=1):
            try:
                lower: float = float(block.group(1))
                upper: float = float(block.group(2))
            except ValueError:
                errors.append(f"High_Low block {number}: the bounds must be numbers")
                continue
            if lower >= upper:
                errors.append(f"High_Low block {number}: the lower bound ({block.group(1)}) must be below the upper bound ({block.group(2)})")
            else:
                spans.append((lower, upper))
            problem, _names = self.check_formula(block.group(3), allow_names=False)
            if problem:
                errors.append(f"High_Low block {number}: {problem}")
        ordered: list[tuple[float, float]] = sorted(spans)
        if any(later[0] < earlier[1] for earlier, later in zip(ordered, ordered[1:])):
            warnings.append("High_Low ranges overlap; the first matching block wins")

    def _check_context(
        self,
        value: object,
        known: set[str],
        have_known: bool,
        errors: list[str],
        warnings: list[str],
        notes: list[str] | None = None,
        real_variables: set[str] | None = None,
        definitions: Mapping[str, object] | None = None,
        source: str = "",
    ) -> None:
        """Validate a ``Context`` object (``formula`` | ``key`` with ``cases``/``range``/``default``).

        ``notes`` receives one line per context variable the object uses (from ``definitions``, defined
        in the file described by ``source``); ``real_variables`` are the protocol's own variables, which
        take precedence over a context variable of the same name.
        """
        if not isinstance(value, dict):
            errors.append("Context must be an object such as {\"formula\": \"x*{scale}\"}")
            return
        spec: dict[str, object] = cast(dict[str, object], value)
        for unused in sorted(set(spec) - set(self.CONTEXT_KEYS)):
            warnings.append(f"Context key '{unused}' is not used by MPG (expected {', '.join(self.CONTEXT_KEYS)})")

        used_names: list[str] = []

        def _formula(label: str, candidate: object) -> None:
            if not isinstance(candidate, str):
                errors.append(f"Context {label} must be text")
                return
            problem, names = self.check_formula(candidate, allow_names=True)
            if problem:
                errors.append(f"Context {label}: {problem}")
            used_names.extend(names)
            if "x" not in self._FORMULA_NAME_RE.sub("", candidate):
                warnings.append(f"Context {label} does not use x, so the row's own value is ignored")

        key: object = spec.get("key", "")
        has_key: bool = isinstance(key, str) and bool(key.strip())
        if "key" in spec and not has_key:
            errors.append("Context key must be the name of another variable")
        if has_key:
            key_text: str = cast(str, key).strip()
            # "{i}" is the per-meter/per-block placeholder of template rows (expanded when the map loads).
            if not self._VARIABLE_NAME_RE.match(key_text.replace("{i}", "1")):
                errors.append(f"Context key '{key_text}' is not a valid variable name (letters, digits and underscores)")
            elif have_known and "{i}" not in key_text and key_text not in known:
                warnings.append(f"Context key '{key_text}' is not a variable of this protocol")
            if "formula" in spec:
                warnings.append("Context formula is ignored when key is set (use cases or default)")
            cases: object = spec.get("cases")
            if cases is not None:
                if not isinstance(cases, dict) or not cases:
                    errors.append("Context cases must be an object of value -> formula, for example {\"0\": \"x\", \"1\": \"-x\"}")
                else:
                    for case_key, case_formula in cast(dict[str, object], cases).items():
                        if not str(case_key).strip():
                            errors.append("A Context cases value is empty")
                        _formula(f"case '{case_key}'", case_formula)
            if "range" in spec and self.context_range_bounds(spec["range"]) is None:
                errors.append(
                    f"Context range must be [low, high] or \"low~high\" with whole numbers within +/-{self.CONTEXT_RANGE_LIMIT}"
                )
            if "default" in spec:
                _formula("default", spec["default"])
            if not any(item in spec for item in ("cases", "range", "default")):
                errors.append("Context with a key needs cases, range or default to choose a formula")
        elif "formula" in spec:
            _formula("formula", spec["formula"])
            for orphan in ("cases", "range", "default"):
                if orphan in spec:
                    warnings.append(f"Context {orphan} is ignored without a key")
        else:
            errors.append("Context needs a formula, or a key with cases, range or default")

        defs: Mapping[str, object] = definitions or {}
        where: str = f"defined in {source}" if source else "defined in the protocol's JSON file"
        if have_known:
            unknown: list[str] = sorted({name for name in used_names if name != "i" and name not in known})
            if unknown:
                hint: str = (
                    f" Context variables {where} (under context_variables) that you can use: " + ", ".join(sorted(defs)) + "."
                    if defs else ""
                )
                warnings.append(
                    "Not a variable of this protocol or one of its context_variables: " + ", ".join("{" + name + "}" for name in unknown)
                    + " (fine if another row supplies it)." + hint
                )
        if notes is not None and defs:
            reals: set[str] = real_variables or set()
            used: list[tuple[str, str]] = []                        # (how it is written, name)
            if has_key:
                used.append((f"'{cast(str, key).strip()}'", cast(str, key).strip()))
            used.extend((f"{{{name}}}", name) for name in used_names)
            for written, name in dict.fromkeys(used):
                if name not in defs:
                    continue
                definition: object = defs[name]
                shown: str = definition if isinstance(definition, str) else json.dumps(definition, ensure_ascii=False)
                if name in reals:
                    notes.append(
                        f"{written} is also the name of a context variable {where} (context_variables), "
                        "but a row with that name exists and its value is used instead."
                    )
                else:
                    notes.append(f"{written} is a context variable {where} (under context_variables): {shown}")

    def preview_adjustments(
        self,
        text: str,
        value: float,
        unit_mod: float = 1.0,
        variables: Mapping[str, int | float] | None = None,
        context_variable_defs: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Run an ``adjustments`` cell on a sample value, through the same code the gateway uses.

        Input:    the cell ``text``, a sample decoded ``value`` (``x``), the row's ``unit_mod`` and sample values
                  for the other variables a ``Context`` formula reads; optionally the protocol's
                  ``context_variables`` definitions.
        Output:   a dict: ``ok``, ``errors``, ``warnings``, ``post_decode`` (after ``High_Low`` or ``unit_mod``
                  and ``Offset``), ``final`` (after ``Context``), ``formula`` (the Context formula chosen, with
                  values substituted) and ``waiting_for`` (names it needs that were not supplied).  When a
                  Context input is missing the gateway publishes the post-decode value, so ``final`` equals it.
        Use case: letting the editor show what a formula does before it is saved.
        """
        check: AdjustmentValidation = self.validate_adjustments(text)
        result: dict[str, object] = {
            "ok": check.ok, "errors": check.errors, "warnings": check.warnings,
            "post_decode": None, "final": None, "formula": "", "waiting_for": [],
        }
        if not check.ok:
            return result
        entry: registry_map_entry = cast("registry_map_entry", _PreviewEntry(self.parse_adjustments(check.normalized), "preview", unit_mod))
        post: int | float | str = self.apply_adjustments(value, entry, "post_decode")
        result["post_decode"] = post

        supplied: dict[str, int | float | str] = dict(variables or {})
        engine: DataAdjustments = DataAdjustments(self._log, self.default_word_order, context_variable_defs)
        derived: dict[str, float] = engine.evaluate_context_variables(supplied)
        view: Mapping[str, int | float | str] = ChainMap(supplied, cast(dict[str, int | float | str], derived))
        context_spec: object | None = self.get_adjustment(entry, "Context")
        if isinstance(context_spec, dict):
            formula: str = engine.select_context_formula("preview", cast(dict[str, object], context_spec), view)
            result["waiting_for"] = sorted({name for name in self._FORMULA_NAME_RE.findall(formula) if name not in view})
            if formula and not result["waiting_for"]:
                result["formula"] = engine.substitute_context_variables(formula, view) or formula
        result["final"] = engine.apply_adjustments(post, entry, "context", view)
        return result

    def safe_eval_expression(self, expression: str) -> int | float:
        """Safely evaluate arithmetic expressions using AST parsing.

        Supports: ``+ - * / // % **``

        Does **not** allow function calls, attribute access, imports,
        variables, comprehensions, lambdas, or arbitrary execution.
        """

        def _safe_eval(node: ast.AST) -> int | float:
            """Recursively evaluate a whitelisted AST node, raising ``TypeError`` on anything unsafe."""
            if isinstance(node, ast.Expression):
                return _safe_eval(node.body)

            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                msg: str = f"Unsupported constant: {type(node.value)}"
                raise ValueError(msg)

            if isinstance(node, ast.BinOp):
                left: int | float = _safe_eval(node.left)
                right: int | float = _safe_eval(node.right)

                # Type the dictionary explicitly with Callable
                ops: dict[type[ast.operator], Callable[[int | float, int | float], int | float]] = {
                    ast.Add:      lambda a, b: a + b,
                    ast.Sub:      lambda a, b: a - b,
                    ast.Mult:     lambda a, b: a * b,
                    ast.Div:      lambda a, b: a / b,
                    ast.FloorDiv: lambda a, b: a // b,
                    ast.Mod:      lambda a, b: a % b,
                    ast.Pow:      lambda a, b: a ** b,
                }

                # Check instance type directly instead of type() comparison
                for op_class, op_func in ops.items():
                    if isinstance(node.op, op_class):
                        return op_func(left, right)

                msg = f"Unsupported operator: {type(node.op).__name__}"
                raise ValueError(msg)

            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return -_safe_eval(node.operand)

            msg = f"Unsupported AST node: {type(node).__name__}"
            raise TypeError(msg)

        expression = re.sub(r"\s+", "", expression)
        tree: ast.Expression = ast.parse(expression, mode="eval")
        result: int | float = _safe_eval(tree)

        if isinstance(result, float) and result.is_integer():
            return int(result)
        return result

# Prefix marking a synthetic field's entry in a variable_screen_*.txt file.
SYNTHETIC_FILTER_PREFIX: str = "synthetic:"


class protocol_settings:
    """Load and expose a protocol's register map, code tables, and settings from JSON and CSV files.

    One instance per transport section.  Owns the ``DataAdjustments`` instance
    used to decode and scale all register values for the protocol.
    """

    @classmethod
    def get_transport_type(cls, protocol_version: str, settings_dir: str = "protocols") -> str:
        """Return the transport class name for ``protocol_version`` by reading its JSON file.

        Looks up ``protocol_version + ".json"`` in ``settings_dir`` via
        ``find_protocol_file``.  Returns the value of the ``transport`` key if
        present, then ``reader`` as a fallback, then ``"modbus_rtu"`` as the
        ultimate default.  Raises ``ValueError`` if the file is not found or
        cannot be parsed.
        """
        path: str | None = cls.find_protocol_file(protocol_version + ".json", settings_dir)
        if path is None:
            msg1: str = f"Protocol '{protocol_version}' not found in '{settings_dir}'"
            raise ValueError(msg1)
        try:
            with open(path, encoding="utf-8") as f:
                data: dict[str, str] = json.loads(f.read())
        except (OSError, json.JSONDecodeError) as e:
            msg2: str = f"Failed to read protocol JSON: {e}"
            raise ValueError(msg2) from e

        settings: dict[str, str] = {k: v for k, v in data.items() if not k.endswith("_codes")}
        if "transport" in settings:
            return str(settings["transport"])
        if "reader" in settings:
            return str(settings["reader"])
        return "modbus_rtu"

    def __init__(self, protocol: str, transport_settings: Optional[TransportSettings] = None, settings_dir: str = "protocols") -> None:
        """Load the protocol JSON and all CSV registry maps, applying transport-level settings.

        Resolves ``byteorder`` from the JSON after loading, then constructs
        ``_adjustments`` so it captures the correct protocol-level default.
        Derives variable-mask and variable-screen filenames from the transport
        section name (``transport.<device>``) unless overridden by
        ``variable_mask`` / ``variable_screen`` keys in ``transport_settings``.
        Iterates all ``Registry_Type`` values and loads each corresponding CSV
        registry map file if one exists.
        """
        # Default word order for assembling multi-register (32/64-bit) values.
        # Separate from transport-level endianness: the transport always delivers
        # 16-bit registers in big-endian byte order per the Modbus spec.
        # This controls word ordering and per-word byte ordering; both axes are
        # captured in a WordOrder instance and can be overridden per-entry via
        # Register_Endian in the adjustments column.
        self.word_order: WordOrder = _DEFAULT_WORD_ORDER

        self._log_level = getattr(logging, logging.getLevelName(logging.getLogger().getEffectiveLevel()), logging.INFO)
        self._log: logging.Logger = logging.getLogger(__name__)
        self._log.setLevel(self._log_level)
        self.protocol: str = protocol
        self.transport: str = ""
        self.registry_map: dict[Registry_Type, list[registry_map_entry]] = {}
        self.registry_map_size: dict[Registry_Type, int] = {}
        self.registry_map_ranges: dict[Registry_Type, list[tuple[int, int]]] = {}
        self.dynamic_registry_rows: list[dict[str, object]] = []
        self.dynamic_registry_resolved = False
        self.discovery_provider: str = ""
        ''' name of the discovery provider declared by the protocol JSON ``"discovery"`` key; empty for static maps '''
        self.anchor_defaults: dict[str, str | float | int] = {}
        ''' anchor values from the protocol JSON ``"discovery_defaults"`` object; the map is fully materialized from these before any discovery runs '''
        self.anchors: dict[str, str | float | int] = {}
        ''' anchors the loaded maps were last built with: the defaults overlaid with whatever discovery reported '''
        self.dynamic_registry_types: set[Registry_Type] = set()
        ''' registry types whose CSV contains template rows and so must be reloaded when the anchors change '''
        self.codes: dict[str, object] = {}
        self.settings: dict[str, str] = {}
        self.variable_mask: list[str] = []
        self.mask_file_name: str = ""
        self.variable_screen: list[str] = []
        self.screen_file_name: str = ""
        self.settings_dir: str = settings_dir
        self.transport_settings: Optional[TransportSettings] = transport_settings

        raw_device: str | None = self.transport_settings.name if self.transport_settings else None

        if raw_device is not None and raw_device.startswith("transport."):
            device_name: str = raw_device.removeprefix("transport.")
        else:
            device_name: str = "unknown_device"

        mask_file: str = "variable_mask_" + device_name + ".txt"
        screen_file: str = "variable_screen_" + device_name + ".txt"

        if transport_settings is not None:
            mask_file = transport_settings.get("variable_mask", fallback=mask_file)
            screen_file = transport_settings.get("variable_screen", fallback=screen_file)

        self.mask_file_name = mask_file
        self.screen_file_name = screen_file

        self.variable_mask = self._load_filter_file(mask_file)
        raw_screen: list[str] = self._load_filter_file(screen_file)
        # Synthetic fields (model, firmware_version, ...) that the user screened
        # are written to the screen file as "synthetic:<name>". They are split
        # out here so they never reach variable_screen: that list drives the
        # per-registry-type rule in load__registry ("screening is in use, so a
        # type with none of its own entries is fully screened"), and a name
        # that matches no register must not be able to trip it.
        self.synthetic_screen: set[str] = {
            e[len(SYNTHETIC_FILTER_PREFIX):] for e in raw_screen if e.startswith(SYNTHETIC_FILTER_PREFIX)
        }
        self.variable_screen = [e for e in raw_screen if not e.startswith(SYNTHETIC_FILTER_PREFIX)]

        self.load__json()
        self._init_discovery_settings()

        if "transport" in self.settings:
            self.transport = self.settings["transport"]
        elif "reader" in self.settings:
            self.transport = self.settings["reader"]
        else:
            self.transport = "modbus_rtu"

        if "byteorder" in self.settings:
            raw_byteorder: str = self.settings["byteorder"].strip().lower()
            resolved: WordOrder | None = _WORD_ORDER_ALIASES.get(raw_byteorder)
            if resolved is not None:
                self.word_order = resolved
            else:
                self._log.warning(
                    f"Invalid byteorder '{raw_byteorder}' in protocol settings — "
                    f"using default ABCD. Valid values: {list(_WORD_ORDER_ALIASES.keys())}"
                )

        # DataAdjustments owns all adjustment parsing/application logic.
        # Constructed here, after word_order is resolved, so it captures the
        # correct protocol-level default (nearly always WORD_ORDER_ABCD for Modbus).
        context_variable_defs: object = self.codes.get("context_variables")
        self._adjustments: DataAdjustments = DataAdjustments(
            self._log,
            self.word_order,
            cast(dict[str, object], context_variable_defs) if isinstance(context_variable_defs, dict) else None,
        )

        for registry_type in Registry_Type:
            self.load_registry_map(registry_type)

    def get_entry_byteorder(self, entry: "registry_map_entry") -> WordOrder:
        """
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Public accessor delegating to ``self._adjustments.get_entry_byteorder()``,
        so external callers (e.g. modbus_base's ``_entry_byte_order``) can get
        the ``WordOrder`` for *entry* without reaching through the private
        ``_adjustments`` attribute.
        """
        return self._adjustments.get_entry_byteorder(entry)

    def synthetic_forwarded(self, name: str) -> bool:
        """Return True when the synthetic field ``name`` should be forwarded to bridges.

        Synthetic fields (EG4 ``model`` / ``firmware_version``, LL-S derived
        cell metrics, ...) follow the same rule as every other metric:

        * a mask file exists -> forwarded only if named in it (M selected);
        * otherwise -> forwarded unless the user screened it (S selected),
          which includes the "nothing selected anywhere" default.
        """
        lowered: str = name.lower()
        if self.variable_mask:
            return lowered in self.variable_mask
        return lowered not in self.synthetic_screen

    def _load_filter_file(self, filename: str) -> list[str]:
        """Load a line-delimited filter file and return a list of cleaned, lowercased metric names.

        Searches for ``filename`` under the ``config`` directory via
        ``find_protocol_file``.  Lines beginning with ``#`` and blank lines are
        skipped.  Returns an empty list if the file is not found or an I/O
        error occurs, so missing filter files are always treated as a no-op.
        """
        file_path: str | None = self.find_protocol_file(filename, "config")

        if not file_path or not os.path.isfile(file_path):
            self._log.debug(f"Filter file '{filename}' not found for protocol '{self.protocol}' — skipping.")
            return []

        entries: list[str] = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    clean_line: str = line.strip().lower()
                    if not clean_line or clean_line.startswith('#'):
                        continue
                    entries.append(clean_line)
        except Exception as e:
            self._log.error(f"Error reading filter file '{filename}': {e}")

        self._log.debug(f"Loaded {len(entries)} entries from filter file '{filename}'")
        return entries

    def get_registry_map(self, registry_type: Registry_Type = Registry_Type.CUSTOM_BUS) -> list[registry_map_entry]:
        """Return the loaded registry map for ``registry_type``, or an empty list if not yet loaded."""
        return self.registry_map.get(registry_type, [])

    def get_registry_ranges(self, registry_type: Registry_Type) -> list[tuple[int, int]]:
        """Return the pre-calculated ``(start, count)`` read ranges for ``registry_type``, or an empty list."""
        return self.registry_map_ranges.get(registry_type, [])

    def get_registry_entry(self, name: str, registry_type: Optional[Registry_Type] = None) -> Optional[registry_map_entry]:
        """Return the first entry whose ``documented_name`` matches ``name``, or ``None``.

        ``name`` is normalized (stripped, lowercased, spaces replaced with
        underscores) before comparison.  When ``registry_type`` is supplied only
        that type's map is searched; otherwise all registry types are searched in
        iteration order.
        """
        cleaned_name: str = name.strip().lower().replace(" ", "_")

        if registry_type is not None:
            for item in self.registry_map.get(registry_type, []):
                if item.documented_name == cleaned_name:
                    return item
            return None

        for r_type in self.registry_map:
            for item in self.registry_map[r_type]:
                if item.documented_name == cleaned_name:
                    return item

        return None

    def get_code_by_value(self, entry: registry_map_entry | None, value: str | None, fallback: str) -> str:
        """Return the code key whose description matches ``value`` for ``entry``, or ``fallback``.

        Performs a case-insensitive reverse lookup in the entry's code dict —
        the inverse of ``_code_description_for_value``.  Returns ``fallback``
        when ``entry`` or ``value`` is ``None``, or when no description matches.
        """
        if value is None or entry is None:
            return fallback

        value = value.strip().lower()
        for code, description in self.get_entry_code_dict(entry).items():
            if value == description.lower():
                return code
        return fallback

    def get_code_dict(self, key: str) -> dict[str, str]:
        """Return the code mapping dict stored under ``key``, or an empty dict if absent or not a dict.

        ``self.codes`` holds both plain string values (protocol settings) and
        dict values (enum/flag code tables keyed by ``<name>_codes``).  This
        method safely returns only dict values so callers always receive a
        iterable mapping.
        """
        value: object | None = self.codes.get(key)
        if isinstance(value, dict):
            return cast(dict[str, str], value)
        return {}

    def get_entry_code_dict(self, entry: registry_map_entry) -> dict[str, str]:
        """Return the code mapping for ``entry``, trying ``documented_name`` then ``variable_name`` conventions.

        Looks up ``<name>_codes`` in ``self.codes`` for each name form in turn
        and returns the first non-empty dict found.  Returns an empty dict when
        the entry has no code mapping under either convention.
        """
        for key_base in (entry.documented_name, entry.variable_name):
            code_dict: dict[str, str] = self.get_code_dict(key_base + "_codes")
            if code_dict:
                return code_dict
        return {}

    def _code_description_for_value(self, entry: registry_map_entry, value: int | float | str) -> str | None:
        """Return the human-readable description for ``value`` from ``entry``'s code dict, or ``None``.

        Tries an integer-normalized key first (``str(int(float(value)))``), then
        the raw string form, so both ``"1"`` and ``1`` resolve to the same
        description.  Returns ``None`` when the entry has no code dict or the
        value is not present in it.
        """
        code_dict: dict[str, str] = self.get_entry_code_dict(entry)
        if not code_dict:
            return None

        lookup_keys: list[str] = [str(value)]
        try:
            lookup_keys.insert(0, str(int(float(value))))
        except (TypeError, ValueError):
            pass

        for key in lookup_keys:
            if key in code_dict:
                return code_dict[key]
        return None

    def load__json(self, file: str = "", settings_dir: str = "") -> None:
        """Load the protocol JSON file into ``self.codes`` and extract plain-string keys into ``self.settings``.

        The JSON file serves dual purpose: keys ending in ``_codes`` are enum or
        flag tables kept only in ``self.codes``; all other string-valued keys are
        also copied to ``self.settings`` for use as protocol configuration
        (``byteorder``, ``transport``, ``batch_size``, etc.).  Defaults ``file``
        to ``<protocol>.json`` and ``settings_dir`` to ``self.settings_dir``
        when not supplied.
        """
        if not settings_dir:
            settings_dir = self.settings_dir

        if not file:
            file = self.protocol + ".json"

        path: str | None = self.find_protocol_file(file, settings_dir)

        if path is None:
            self._log.error(f"ERROR: '{file}' not found")
            return

        with open(path) as f:
            self.codes = json.loads(f.read())

        self.settings = {}
        # Extract non-code settings into self.settings for easy access, while keeping code tables in self.codes.
        for key, value in self.codes.items():
            if not key.endswith("_codes") and isinstance(value, (str, bool, int, float)):
                self.settings[key] = str(value).lower() if isinstance(value, bool) else str(value)

    def load_registry_overrides(self, override_path: str, keys: list[str]) -> dict[str, dict[str, dict[str, str]]]:
        """Parse a CSV override file and return a nested dict keyed by each column in ``keys``.

        For each row, normalizes the value of every ``keys`` column (strip,
        lowercase, spaces to underscores) and indexes the full row under that
        value.  The result allows O(1) lookup of override rows by either
        ``documented name`` or ``register``, so the caller can match and apply
        overrides without a linear scan.
        """
        overrides: dict[str, dict[str, dict[str, str]]] = {key: {} for key in keys}

        with open(override_path, newline="", encoding="latin-1") as csvfile:
            # restval="": a row shorter than the header yields "" (not None) for its missing
            # trailing cells, so a ragged row is treated as blank cells instead of crashing.
            reader: csv.DictReader[str] = csv.DictReader(csvfile, restval="")
            for row in reader:
                for key in keys:
                    if key in row:
                        row[key] = row[key].strip().lower().replace(" ", "_")
                        key_value: str | Any = row[key]
                        if key_value:
                            overrides[key][key_value] = row
        return overrides

    def load__registry(self, path: str, registry_type: Registry_Type = Registry_Type.INPUT) -> list[registry_map_entry]:
        """Parse the registry-map CSV at ``path`` and return a list of ``registry_map_entry`` objects.

        Handles delimiter auto-detection (comma vs semicolon), optional
        ``.override.csv`` sidecar files, read-interval parsing (``ms``, ``s``,
        and ``x`` multiplier), unit-symbol/multiplier splitting, data-type
        resolution, value-range and enum parsing, register address parsing
        (decimal, hex, bit-offset ``N.bX``, byte-offset ``N.Y``, range
        ``A-B``), and dynamic register expressions (deferred into
        ``dynamic_registry_rows``).  After all rows are processed, adjacent
        ``_l``/``_h`` pairs are merged into single 32-bit entries, the variable
        mask (allowlist) and variable screen (denylist) are applied, and
        ``_add_code_description_entries`` appends synthetic ``_desc`` entries for
        any entry that has a code mapping.
        """
        registry_map: list[registry_map_entry] = []

        register_regex: re.Pattern[str] = re.compile(
            # Matches a single register address: hex (``0x`` or bare ``x`` prefix) or
            # plain decimal of any length, with an optional bit-offset (``.b#`` or a
            # bit range ``.b#-#`` / ``.b#-b#``) or byte-offset (``.#``) suffix.
            # Hex is listed first and the whole cell must match (^...$), otherwise
            # ``0x5000`` would match as the decimal ``0`` and ``115~119`` as ``115``.
            # This regex is only reached when register_list is empty (i.e. the
            # "low_high" underscore format was not detected above), so it will
            # never be applied to multi-register addresses like "40_41".
            r"^(?P<register>0?x[0-9a-f]+|\d+)"
            r"(?:\.b(?P<bit_start>\d{1,2})(?:-b?(?P<bit_end>\d{1,2}))?)?"
            r"(?:\.(?P<byte>\d{1,2}))?$",
            re.IGNORECASE,
        )

        # Text-keyed protocols (VE.Direct labels, OBIS codes, ...) declare
        # "register_key_type": "text" in the protocol JSON.  Every register cell is
        # then a literal key instead of a numeric address.
        register_key_type: str = self.settings.get("register_key_type", "numeric").strip().lower()
        if register_key_type not in ("numeric", "text"):
            self._log.warning(f"Unknown register_key_type '{register_key_type}' (use 'numeric' or 'text'); assuming 'numeric'")
            register_key_type = "numeric"
        text_keys: bool = register_key_type == "text"
        text_key_ids: dict[str, int] = {}

        read_interval_regex: re.Pattern[str] = re.compile(r"(?P<value>[\.\d]+)(?P<unit>[xs]|ms)")
        data_type_regex: re.Pattern[str] = re.compile(r"(?P<datatype>\w+)\.(?P<length>\d+)")
        range_regex: re.Pattern[str] = re.compile(r"(?P<reverse>r|)(?P<start>(?:0?x[\da-z]+|[\d]+))[\-~](?P<end>(?:0?x[\da-z]+|[\d]+))")
        # Validation range in the ``values`` column: "0-100", "0~65535", "-100~100", "-32768-32767".
        # Either end may be negative.  (range_regex above is for register ranges and has no sign.)
        value_range_regex: re.Pattern[str] = re.compile(r"(?P<start>-?(?:0?x[\da-z]+|\d+))[\-~](?P<end>-?(?:0?x[\da-z]+|\d+))", re.IGNORECASE)
        ascii_value_regex: re.Pattern[str] = re.compile(r"(?P<regex>^\[.+\]$)")
        list_regex: re.Pattern[str] = re.compile(r"\s*(?:(?P<range_start>(?:0?x[\da-z]+|[\d]+))-(?P<range_end>(?:0?x[\da-z]+|[\d]+))|(?P<element>[^,\s][^,]*?))\s*(?:,|$)")

        transport_read_interval: int = 1000
        if self.transport_settings is not None:
            transport_read_interval = self.transport_settings.getint("read_interval", transport_read_interval)

        if not os.path.exists(path):
            return registry_map

        overrides: dict[str, dict[str, dict[str, str]]] | None = None
        override_keys: list[str] = ["documented name", "register"]
        overrided_keys: set[str] = set()

        override_path: str = path[:-4] + ".override.csv"

        if os.path.exists(override_path):
            self._log.info("loading override file: " + override_path)
            overrides = self.load_registry_overrides(override_path, override_keys)

        def process_row(row: dict[str, str]) -> None:
            """Parse one CSV row and append the resulting ``registry_map_entry`` objects to ``registry_map``.

            Skips commented rows (leading ``#``), entirely empty rows, and rows
            whose register field contains an unresolved dynamic expression
            (deferred to ``dynamic_registry_rows`` instead).  Applies any
            matching override row before constructing the entry.  A range
            register (``A-B``) produces one entry per address in the range.
            """
            unit_multiplier: float = 1
            unit_symbol: str = ""
            read_interval: int = 0
            adjustments: dict[str, object] = self._adjustments.parse_adjustments(row.get("adjustments", ""))

            # Strip \r from all field values at parse time to handle Windows CRLF CSVs
            # Strip CR from all string field values at parse time to handle Windows CRLF CSVs
            # (values are expected to be strings)
            row = {k: v.strip("\r") for k, v in row.items()}

            if row["variable name"].startswith("#") or row["register"].startswith("#"):
                return

            if not any((row.get("register", ""), row.get("variable name", ""), row.get("documented name", ""))):
                return

            row["documented name"] = row["documented name"].strip().lower().replace(" ", "_")

            # region read_interval
            if "read interval" in row:
                row["read interval"] = row["read interval"].lower()
                match: re.Match[str] | None = read_interval_regex.search(row["read interval"])
                if match:
                    unit: str | None = match.group("unit")
                    value_str: str | None = match.group("value")
                    if value_str and unit:
                        value_float: float = float(value_str)
                        if unit == "x":
                            read_interval = int((transport_read_interval * 1000) * value_float)
                        else:
                            if unit != "ms":
                                value_float *= 1000
                            read_interval = int(value_float)

            if read_interval == 0:
                read_interval = transport_read_interval * 1000
                if "read_interval" in self.settings:
                    try:
                        read_interval = int(self.settings["read_interval"])
                    except ValueError:
                        read_interval = transport_read_interval * 1000
            # endregion read_interval

            # region overrides
            if overrides is not None:
                override_row: dict[str, str] | None = None
                for key in override_keys:
                    key_value: str | None = row.get(key)
                    if key_value and key_value in overrides[key]:
                        override_row: dict[str, str] | None = overrides[key][key_value]
                        overrided_keys.add(key_value)
                        break

                if override_row:
                    for field, override_value in override_row.items():
                        if override_value:
                            row[field] = override_value
            # endregion overrides

            # region unit
            if "or" in row["unit"].lower() or ":" in row["unit"].lower():
                unit_multiplier = 1
                unit_symbol = row["unit"]
            else:
                unit_matches: list[tuple[str, str]] = re.findall(
                    r"(\-?[0-9.]+)|(.*?)$",
                    row["unit"]
                )

                for unit_match in unit_matches:
                    if unit_match[0]:
                        unit_multiplier = float(unit_match[0])
                    elif unit_match[1]:
                        unit_symbol = unit_match[1].strip()

            try:
                unit_multiplier = float(unit_multiplier)
            except Exception:
                unit_multiplier = 1.0

            if unit_multiplier == 0:
                unit_multiplier = 1.0
            # endregion unit

            variable_name: str = row["variable name"] if row["variable name"] else row["documented name"]
            variable_name = variable_name.strip().lower().replace(" ", "_").replace("__", "_")

            if re.search(r"[^a-zA-Z0-9\_]", variable_name):
                self._log.warning("Invalid Name : " + str(variable_name) + " reg: " + str(row["register"]) + " doc name: " + str(row["documented name"]) + " path: " + str(path))

            if not variable_name and not row["documented name"]:
                return

            # region data type
            data_type = Data_Type.USHORT
            data_type_len: int = -1
            if "data type" in row and row["data type"]:
                data_type_str: str = ''

                matches: re.Match[str] | None = data_type_regex.search(row["data type"])
                if matches:
                    data_type_len = int(matches.group("length"))
                    data_type_str = matches.group("datatype")
                else:
                    data_type_str = row["data type"]

                data_type_parsed: Data_Type | None = Data_Type.fromString(data_type_str)
                if data_type_parsed is None:
                    self._log.warning(f"Unknown data type '{row['data type']}' for variable '{variable_name}' in path: {path}. Defaulting to USHORT.")
                    data_type = Data_Type.USHORT
                else:
                    data_type: Data_Type = data_type_parsed

            # Guard: some registry maps duplicate the bit index in the unit column
            # for 1bit/nbit rows (e.g. 21.b11,...,11,1bit,...). That numeric token
            # is not a real engineering scale factor and must not become unit_mod.
            if (
                data_type.value > 200
                and row.get("unit", "").strip()
                and re.fullmatch(r"-?\d+(?:\.\d+)?", row["unit"].strip())
            ):
                unit_multiplier = 1.0
                unit_symbol = ""

            if "note" in row and row["note"]:
                note: str = row["note"]
            else:
                note: str = ""

            if "values" not in row:
                row["values"] = ""
                self._log.warning("No Value Column : path: " + str(path))
            # endregion data type

            # region values
            values: list[int | str] = []
            value_min: int
            value_max: int
            value_min, value_max = Data_Type.getDefaultRange(data_type)
            value_regex: str = ""
            value_is_json: bool = False

            if "{" in row["values"]:
                try:
                    codes_json: object = json.loads(row["values"])
                    value_is_json = True
                    name: str = row["documented name"] + "_codes"
                    if name not in self.codes:
                        self.codes[name] = codes_json
                except ValueError:
                    value_is_json = False

            if not value_is_json:
                if "," in row["values"]:
                    list_matches: Iterator[re.Match[str]] = list_regex.finditer(row["values"])

                    for list_match in list_matches:
                        range_start: str | None = list_match.groupdict().get("range_start")
                        range_end: str | None = list_match.groupdict().get("range_end")
                        element: str | None = list_match.groupdict().get("element")

                        if range_start and range_end:
                            start: int = strtoint_safe(range_start)
                            end: int = strtoint_safe(range_end)
                            values.extend(range(start, end + 1))
                        elif element:
                            values.append(element)
                else:
                    unit_matched: bool = False
                    val_match: re.Match[str] | None = value_range_regex.search(row["values"])
                    if val_match:
                        value_min = strtoint_safe(val_match.group("start"))
                        value_max = strtoint_safe(val_match.group("end"))
                        unit_matched = True

                    if data_type == Data_Type.ASCII:
                        val_match = ascii_value_regex.search(row["values"])
                        if val_match:
                            value_regex = val_match.group("regex")
                            unit_matched = True

                    if not unit_matched:
                        values.append(row["values"])
            # endregion values

            # region register
            concatenate: bool = False
            concatenate_registers: list[int] = []
            register_list: list[int] = []   # populated for "low_high[_mid_msb]" address format

            register: int = -1
            register_bit: int = -1
            register_bit_end: int = -1
            register_byte: int = -1

            register_key: str = row["register"].strip() if text_keys else ""
            row["register"] = row["register"].lower()

            # ── "low_high" address format (e.g. "40_41", "41_40", "40_50") ──
            # The underscore-separated format explicitly lists every register
            # address in the order they contribute words to the combined value.
            # Token 0 is the low word, token 1 is the high word (for UINT32),
            # tokens 0-3 for UINT64/INT64/FLOAT64 etc.  This differs from the range format
            # "40-43" which implies consecutive addresses in ascending order.
            # Detection rule: all tokens are plain non-negative integers and
            # there is at least one underscore.
            _reg_str: str = row["register"].strip()
            if not register_key and "_" in _reg_str and not _reg_str.startswith("_"):
                _tokens: list[str] = _reg_str.split("_")
                _parsed: list[int] = []
                _all_int: bool = True
                for _tok in _tokens:
                    _tok = _tok.strip().lower()
                    # An empty token ("1_", "40__41") is malformed; strtoint("") would
                    # otherwise turn it into a phantom register 0.
                    _val: int | str = strtoint(_tok) if _tok else ""  # decimal ("40"), "0x28" or "x28"
                    if isinstance(_val, int):
                        _parsed.append(_val)
                    else:
                        _all_int = False
                        break

                if _all_int and len(_parsed) >= 2:
                    register_list = _parsed
                    register = _parsed[0]  # low word address — used as entry.register

                    # Cross-check token count against data_type word width.
                    # data_type is parsed below so we defer the check until after
                    # entry construction — see post-entry cross-check block.

            if register_key:
                # Text-keyed protocol: the whole cell is the key (case preserved, dots
                # and colons are part of it).  Identical keys share one integer id.
                register = text_key_ids.setdefault(register_key, len(text_key_ids))
                register_bit = -1
                register_bit_end = -1
                register_byte = 0

            elif not register_list:
                reg_match: re.Match[str] | None = register_regex.search(row["register"])

                if reg_match:
                    try:
                        register: int = strtoint_safe(
                            reg_match.group("register"),
                            context="register address"
                        )

                        bit_start_str: str | None = reg_match.group("bit_start")
                        bit_end_str: str | None = reg_match.group("bit_end")

                        if bit_start_str is not None:
                            register_bit = strtoint_safe(bit_start_str, context="register bit start")
                            if bit_end_str is not None:
                                register_bit_end = strtoint_safe(bit_end_str, context="register bit end")
                            else:
                                register_bit_end = register_bit
                        else:
                            register_bit = -1
                            register_bit_end = -1

                        byte_str: str | None = reg_match.group("byte")
                        register_byte = strtoint_safe(byte_str, context="register byte") if byte_str else 0

                    except ValueError as e:
                        self._log.warning(f"Skipping malformed register definition '{row['register']}': {e}")
                        return

                else:
                    range_match: re.Match[str] | None = range_regex.fullmatch(row["register"].strip())
                    if not range_match:
                        if "[" in row["register"]:
                            self._log.info(f"Deferred dynamic register expression: {row['register']}")
                            deferred_row: dict[str, object] = dict(row)
                            deferred_row["_registry_type"] = registry_type
                            self.dynamic_registry_rows.append(deferred_row)
                            return
                        else:
                            try:
                                register = strtoint_safe(row["register"], context="register address")
                            except ValueError:
                                self._log.warning(
                                    f"Skipping non-numeric register address '{row['register']}'. "
                                    f"If this protocol is keyed by labels (VE.Direct, OBIS, ...) set "
                                    f"\"register_key_type\": \"text\" in its JSON."
                                )
                                return
                    else:
                        reverse: str = range_match.group("reverse")
                        start = strtoint_safe(range_match.group("start"))
                        end = strtoint_safe(range_match.group("end"))
                        register = start
                        if end > start:
                            concatenate = True
                            if reverse:
                                for i in range(end, start - 1, -1):
                                    concatenate_registers.append(i)
                            else:
                                for i in range(start, end + 1):
                                    concatenate_registers.append(i)

            if concatenate_registers:
                r = range(len(concatenate_registers))
            else:
                r = range(1)
            # endregion register

            read_command = None
            if "read command" in row and row["read command"]:
                if row["read command"][0] == "x":
                    read_command: bytes | None = bytes.fromhex(row["read command"][1:])
                else:
                    read_command = row["read command"].encode("utf-8")

            writeMode: WriteMode = WriteMode.READ
            if "writable" in row:
                writeMode = WriteMode.fromString(row["writable"])
            if "write" in row:
                writeMode = WriteMode.fromString(row["write"])

            for i in r:
                item = registry_map_entry(
                    registry_type=registry_type,
                    register=register,
                    register_bit=register_bit,
                    register_bit_end=register_bit_end,
                    register_byte=register_byte,
                    register_list=register_list,
                    register_key=register_key,
                    variable_name=variable_name,
                    documented_name=row["documented name"],
                    unit=str(unit_symbol),
                    unit_mod=unit_multiplier,
                    adjustments=adjustments,
                    data_type=data_type,
                    data_type_size=data_type_len,
                    note=note,
                    concatenate=concatenate,
                    concatenate_registers=concatenate_registers,
                    values=values,
                    value_min=value_min,
                    value_max=value_max,
                    value_regex=value_regex,
                    read_command=read_command,
                    read_interval=read_interval,
                    write_mode=writeMode,
                    has_enum_mapping=value_is_json,
                    ha_device_class=(row.get("ha device class") or "").strip().lower(),
                    ha_state_class=(row.get("ha state class") or "").strip().lower(),
                    ha_entity_category=(row.get("ha entity category") or "").strip().lower(),
                    dynamic=row.get("_dynamic") == "1",
                )

                # Cross-check register_list token count against data_type word width.
                # Mismatch means the address format and data_type disagree —
                # log a WARNING and trust the address list (it's more explicit).
                if item.register_list:
                    expected_words: dict[Data_Type, int] = {
                        Data_Type.UINT: 2, Data_Type.INT: 2,
                        Data_Type.FLOAT32: 2, Data_Type.ACC32: 2,
                        Data_Type.BIT32_FLAGS: 2,
                        Data_Type.UINT64: 4, Data_Type.INT64: 4, Data_Type.FLOAT64: 4,
                    }
                    expected: int | None = expected_words.get(item.data_type)
                    actual: int = len(item.register_list)
                    if expected is not None and actual != expected:
                        self._log.warning(
                            f"Register address '{row['register']}' has {actual} token(s) "
                            f"but data_type {item.data_type.name} expects {expected} words "
                            f"for {item.variable_name!r} — address list takes precedence"
                        )

                registry_map.append(item)
                register = register + 1

        with open(path, newline="", encoding="latin-1") as csvfile:
            delimeter = ";"
            first_row = next(csvfile).strip("\r\n").lower().replace("_", " ")
            if first_row.count(";") < first_row.count(","):
                delimeter = ","

            first_row: str = re.sub(r"\s+" + re.escape(delimeter) + "|" + re.escape(delimeter) + r"\s+", delimeter, first_row)
            csvfile_iter: itertools.chain[str] = itertools.chain([first_row], csvfile)
            # restval="": missing trailing cells become blank. Without it csv.DictReader fills them with
            # None and the per-row .strip() below raised AttributeError — for ANY short row, in any
            # protocol CSV, not just ones with the optional ha_* columns.
            reader: csv.DictReader[str] = csv.DictReader(csvfile_iter, delimiter=delimeter, restval="")

            for row in reader:
                for expanded_row in self._expand_template_row(row, registry_type):
                    process_row(expanded_row)

            if overrides is not None:
                for key in override_keys:
                    applied = False
                    for _key_value, override_row in overrides[key].items():
                        if all(override_row.get(k) for k in override_keys):
                            if all(override_row.get(k) not in overrided_keys for k in override_keys):
                                self._log.info("Loading unique entry from overrides for both unique keys")
                                process_row(override_row)
                                for k in override_keys:
                                    override_value = override_row.get(k)
                                    if override_value is not None:
                                        overrided_keys.add(override_value)
                                applied = True
                                break

                    if applied:
                        continue

            # Merge _h/_l register pairs into single 32-bit entries.
            # CSV layout: _l at lower register address (lower list index N),
            #             _h at higher register address (list index N+1).
            # The _l row survives as the combined entry; the _h row is deleted.
            # The _l row already carries Register_Endian:little — no propagation needed.

            # DEBUG-MERGE: log every _l entry to show what the merge loop will examine
            # _l_candidates = [
            #     (registry_map[i].documented_name, registry_map[i+1].documented_name)
            #     for i in range(len(registry_map) - 1)
            #     if registry_map[i].documented_name.endswith('_l')
            #]
            # self._log.warning(
            #     f"[DEBUG-MERGE] path={path} "
            #     f"_l_candidates (index, index+1)={_l_candidates}"
            # )

            for index in reversed(range(len(registry_map) - 1)):
                item: registry_map_entry = registry_map[index]
                next_item: registry_map_entry = registry_map[index + 1]
                if (
                    item.documented_name.endswith("_l")
                    and next_item.documented_name == item.documented_name[:-2] + "_h"
                ):
                    # _l row is the surviving combined entry
                    combined_item: registry_map_entry = item

                    if not combined_item.data_type or combined_item.data_type == Data_Type.USHORT:
                        if next_item.data_type != Data_Type.USHORT:
                            combined_item.data_type = next_item.data_type
                        else:
                            combined_item.data_type = Data_Type.UINT

                    if combined_item.documented_name == combined_item.variable_name:
                        combined_item.variable_name = combined_item.variable_name[:-2].strip()

                    combined_item.documented_name = combined_item.documented_name[:-2].strip()

                    if not combined_item.unit:
                        combined_item.unit = next_item.unit
                        combined_item.unit_mod = next_item.unit_mod

                    # Copy adjustments from _h if _l has none
                    if not combined_item.adjustments and next_item.adjustments:
                        combined_item.adjustments = dict(next_item.adjustments)

                    # Same rule for the optional Home Assistant columns.
                    for ha_attr in ("ha_device_class", "ha_state_class", "ha_entity_category"):
                        if not getattr(combined_item, ha_attr) and getattr(next_item, ha_attr):
                            setattr(combined_item, ha_attr, getattr(next_item, ha_attr))

                    # self._log.warning(
                    #     f"[DEBUG-MERGE] MERGED: '{combined_item.variable_name}' "
                    #     f"reg={combined_item.register} "
                    #     f"dtype={combined_item.data_type} "
                    #     f"unit_mod={combined_item.unit_mod} "
                    #     f"adj={combined_item.adjustments}"
                    # )
                    del registry_map[index + 1]


            # Apply variable mask (allowlist)
            if self.variable_mask:
                mask_matched_count: int = sum(
                    1 for item in registry_map
                    if item.documented_name.strip().lower() in self.variable_mask
                    or item.variable_name.strip().lower() in self.variable_mask
                    or (item.documented_name.strip().lower() + "_l") in self.variable_mask
                    or (item.variable_name.strip().lower() + "_l") in self.variable_mask
                )
                if mask_matched_count == 0 and registry_map:
                    self._log.warning(
                        f"[{self.protocol}] variable_mask has {len(self.variable_mask)} "
                        f"entries but none match any {registry_type.name} register "
                        f"({len(registry_map)} available) — excluding the ENTIRE "
                        f"{registry_type.name} registry type (0 metrics will be scraped from "
                        f"it). This almost always means the mask file references register "
                        f"names that no longer exist (e.g. after a protocol CSV update)"
                        f"or that no metrics have been chosen from only one of the registers."
                    )
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() not in self.variable_mask
                        and item.variable_name.strip().lower() not in self.variable_mask
                        and (item.documented_name.strip().lower() + "_l") not in self.variable_mask
                        and (item.variable_name.strip().lower() + "_l") not in self.variable_mask
                    ):
                        del registry_map[index]

            # Apply variable screen (denylist). Screen files are a single
            # flat, type-agnostic list shared by every registry type on a
            # device (variable_screen_<device>.txt has no per-type
            # sections), so when this runs for e.g. INPUT, a screen entry
            # that happens to name a HOLDING register is indistinguishable
            # at the file level from "this device doesn't screen INPUT at
            # all." That ambiguity used to resolve as "nothing in INPUT
            # matched the screen list, so nothing gets removed" — INPUT was
            # silently left fully unscreened (de facto whitelisted) the
            # moment screening was used for any *other* type on the same
            # device, rather than being treated as fully screened itself.
            #
            # Correct behavior: once screening is in use anywhere on this
            # device, a registry type with zero of its *own* entries in the
            # screen list is fully screened (nothing scraped from it), not
            # left untouched — only a type that has at least one of its own
            # entries in the list gets the normal per-item denylist
            # treatment, scoped to just those entries. A device whose
            # screen file is entirely empty is unaffected either way
            # (self.variable_screen is falsy, this block is skipped
            # entirely, same "no selections = forward everything" device
            # default as before). Mask (allowlist) needs no equivalent fix:
            # an allowlist with zero of its own type's entries already
            # deletes everything in that type via the loop above, which is
            # the correct behavior for a whitelist with nothing listed.
            #
            # Mask always wins over screen: when a mask exists the screen is
            # not applied at all (a metric that is both mask- and
            # screen-selected is still forwarded), so only the mask-checked
            # metrics are sent. The UI disables the S checkboxes while any
            # metric is mask-selected, so this only matters for stale or
            # hand-edited files.
            if self.variable_screen and not self.variable_mask:
                type_has_own_screen_entries: bool = any(
                    item.documented_name.strip().lower() in self.variable_screen
                    or item.variable_name.strip().lower() in self.variable_screen
                    or (item.documented_name.strip().lower() + "_l") in self.variable_screen
                    or (item.variable_name.strip().lower() + "_l") in self.variable_screen
                    for item in registry_map
                )
                if not type_has_own_screen_entries:
                    self._log.warning(
                        f"[{self.protocol}] variable_screen has {len(self.variable_screen)} "
                        f"entries but none match any {registry_type.name} register "
                        f"({len(registry_map)} available) — excluding the ENTIRE "
                        f"{registry_type.name} registry type (0 metrics will be scraped from "
                        f"it). This almost always means the screen file references register "
                        f"names that no longer exist (e.g. after a protocol CSV update). "
                        f"If this type should still be scraped, remove its entries from "
                        f"the screen file or verify the names against the current registry map."
                    )
                    registry_map.clear()
                else:
                    for index in reversed(range(len(registry_map))):
                        item = registry_map[index]
                        if (
                            item.documented_name.strip().lower() in self.variable_screen
                            or item.variable_name.strip().lower() in self.variable_screen
                            or (item.documented_name.strip().lower() + "_l") in self.variable_screen
                            or (item.variable_name.strip().lower() + "_l") in self.variable_screen
                        ):
                            del registry_map[index]

            self._add_code_description_entries(registry_map)

            return registry_map

    def _add_code_description_entries(self, registry_map: list[registry_map_entry]) -> None:
        """Append synthetic ``<name>_desc`` entries for entries that have a code mapping but no existing description entry.

        For each entry that has a code dict and no ``description_source``,
        creates a companion ``STRING``/``READDISABLED`` entry whose
        ``description_source`` points back at the source entry's
        ``variable_name``.  Skips entries whose ``_desc`` name already exists
        or appears in ``variable_screen``.  Additions are batched and appended
        after iteration completes to avoid modifying the list mid-loop.
        """
        existing_names: set[str] = {entry.variable_name for entry in registry_map}
        additions: list[registry_map_entry] = []

        for entry in registry_map:
            if entry.description_source:
                continue
            if not self.get_entry_code_dict(entry):
                continue

            desc_name: str = f"{entry.variable_name}_desc"
            if desc_name in existing_names:
                continue
            if desc_name.lower() in self.variable_screen:
                continue

            additions.append(
                registry_map_entry(
                    registry_type=entry.registry_type,
                    register=entry.register,
                    register_bit=entry.register_bit,
                    register_bit_end=entry.register_bit_end,
                    register_byte=entry.register_byte,
                    register_key=entry.register_key,
                    variable_name=desc_name,
                    documented_name=f"{entry.documented_name}_desc",
                    note=f"Decoded description for {entry.note}" if entry.note else f"Decoded description for {entry.variable_name}",
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
                    read_interval=entry.read_interval,
                    write_mode=WriteMode.READDISABLED,
                    has_enum_mapping=False,
                    description_source=entry.variable_name,
                )
            )
            existing_names.add(desc_name)

        registry_map.extend(additions)

    @property
    def register_address_step(self) -> int:
        """Address distance between consecutive 16-bit words of a multi-word read (default 1).

        Almost every Modbus device numbers its registers consecutively, so a read of ``n``
        words starting at address ``a`` returns the words for addresses ``a, a+1, ...``.  A few
        devices (for example the JK BMS Modbus map) number registers by *byte offset*, so
        consecutive 16-bit words are 2 addresses apart.  Setting ``register_address_step`` in
        the protocol JSON tells MPG that word ``i`` of a read starting at ``a`` belongs to address
        ``a + i * step``.  The map itself keeps the device's own addresses.  ``batch_size`` and
        every register address stay in address units; only the word counts are divided by the step.
        """
        try:
            step: int = int(self.settings.get("register_address_step", 1))
        except (ValueError, TypeError, AttributeError):
            return 1
        return step if step >= 1 else 1

    def calculate_registry_ranges(self, registry_map: list[registry_map_entry], max_register: int, init: bool = False, timestamp: float = 0.0) -> list[tuple[int, int]]:
        """Return the minimal set of ``(start, count)`` register ranges needed for one poll cycle.

        Divides the address space into windows of ``max_batch_size`` (default
        40, overridable via ``batch_size`` in the JSON settings) and finds the
        tightest span covering all due entries in each window.  An entry is
        considered due when ``init=True`` (first load) or its
        ``next_read_timestamp`` is in the past relative to ``timestamp``.  When
        not initializing, ``next_read_timestamp`` is advanced by
        ``read_interval`` for each included entry.  ``READDISABLED`` and
        ``WRITEONLY`` entries are always excluded.  Returns a list of
        ``(start_register, count)`` tuples suitable for direct use in a Modbus
        read call.
        """
        max_batch_size = 40
        if "batch_size" in self.settings:
            try:
                max_batch_size = int(self.settings["batch_size"])
            except ValueError:
                pass

        self._log.debug(f"calculate_registry_ranges: max_register={max_register}, max_batch_size={max_batch_size}, map_size={len(registry_map)}, init={init}")

        timestamp_ms: float = timestamp * 1000 if timestamp > 0 else float(time.time() * 1000)
        ranges: list[tuple[int, int]] = []
        step: int = self.register_address_step

        for start in range(0, max_register + 1, max_batch_size):
            end: int = start + max_batch_size

            window_min = None
            window_max = None

            for register in registry_map:
                if start <= register.register < end:
                    if register.write_mode in (WriteMode.READDISABLED, WriteMode.WRITEONLY):
                        continue

                    if init or register.next_read_timestamp < timestamp_ms:
                        if not init:
                            register.next_read_timestamp = timestamp_ms + register.read_interval

                        # For explicit address-list entries, the window must cover
                        # ALL addresses in the list — they may be non-contiguous or
                        # higher than start_register + word_count - 1.
                        if register.register_list:
                            register_end: int = max(register.register_list)
                        else:
                            register_end: int = register.register + (self.entry_word_count(register) - 1) * step

                        if window_min is None or register.register < window_min:
                            window_min: int | None = register.register
                        if window_max is None or register_end > window_max:
                            window_max: int | None = register_end

            if window_min is not None and window_max is not None:
                ranges.append((window_min, (window_max - window_min) // step + 1))

        return ranges

    @staticmethod
    def find_protocol_file(file: str, base_dir: str = "") -> Optional[str]:
        """Search for ``file`` under ``base_dir`` relative to the project root and return its absolute path, or ``None``.

        Tries two candidate paths first: ``<root>/<base_dir>/<file>`` and
        ``<root>/<base_dir>/<prefix>/<file>`` where prefix is the part of the
        filename before the first ``_``.  Falls back to a recursive ``rglob``
        under the base directory.  Static so it can be called from
        ``get_transport_type`` before any instance is constructed.
        """
        base_path: Path = Path(__file__).resolve().parent.parent / base_dir

        candidates: list[Path] = [
            base_path / file,
            base_path / file.split("_", 1)[0] / file,
        ]
        found: Path | None = next((p for p in candidates if p.exists()), None)
        if found:
            return str(found)
        try:
            return str(next(base_path.rglob(file)))
        except StopIteration:
            return None

    def load_registry_map(self, registry_type: Registry_Type, file: str = "", settings_dir: str = "") -> None:
        """Load the CSV registry map for ``registry_type`` and populate ``registry_map``, ``registry_map_size``, and ``registry_map_ranges``.

        Derives the filename from the protocol name and registry type when
        ``file`` is not supplied (``<protocol>.registry_map.csv`` for
        ``CUSTOM_BUS``, ``<protocol>.<type>_registry_map.csv`` for all others).
        After loading, walks the entries to determine the highest register
        address for ``registry_map_size``, then pre-computes and caches the
        initial read ranges in ``registry_map_ranges``.  Silently returns when
        the file cannot be located.
        """
        if not settings_dir:
            settings_dir = self.settings_dir

        if not file:
            if registry_type == Registry_Type.CUSTOM_BUS:
                file = self.protocol + ".registry_map.csv"
            else:
                file = self.protocol + "." + registry_type.name.lower() + "_registry_map.csv"

        path: str | None = self.find_protocol_file(file, settings_dir)

        if not path:
            return

        # A reload (see rebuild_dynamic_maps) must not accumulate the deferred rows of the previous load.
        self.dynamic_registry_rows = [row for row in self.dynamic_registry_rows if row.get("_registry_type") != registry_type]
        self.dynamic_registry_types.discard(registry_type)

        self.registry_map[registry_type] = self.load__registry(path, registry_type)

        size: int = 0
        for item in self.registry_map[registry_type]:
            if item.register_list:
                item_end_register: int = max(item.register_list)
            else:
                item_end_register = item.register + (self.entry_word_count(item) - 1) * self.register_address_step
            if item_end_register > size:
                size = item_end_register

        self.registry_map_size[registry_type] = size
        self._log.debug(f"load_registry_map: {registry_type.name} - loaded {len(self.registry_map[registry_type])} entries, max_register={size}")
        self.registry_map_ranges[registry_type] = self.calculate_registry_ranges(self.registry_map[registry_type], self.registry_map_size[registry_type], init=True)

    def process_register_bytes(self, registry: Mapping[int, bytes | tuple[bytes, float]], entry: registry_map_entry) -> int | float | str | None:
        """Process a bytes-oriented registry entry into a typed value.

        Endian contract for the bytes transport path
        --------------------------------------------
        The transport delivers raw wire bytes in Modbus big-endian order within
        each 16-bit word.  ``WordOrder`` controls two independent axes:

        ``word_reversed``
            For multi-register types: when True the word sequence is reversed
            before integer unpacking (low word was at the lower register address).
            Has no effect on single-register types.

        ``bytes_reversed``
            When True the two bytes within each 16-bit word are swapped.
            Applies to both single- and multi-register types (BADC / DCBA).

        All four ABCD / BADC / CDAB / DCBA encodings are handled correctly by
        combining these two axes independently.
        """

        raw: bytes | tuple[bytes, float] = registry[entry.register]

        if isinstance(raw, tuple):
            register: bytes = raw[0]
        else:
            register = raw

        word_order: WordOrder = self._adjustments.get_entry_byteorder(entry)

        if entry.register_byte > 0:
            register = register[entry.register_byte:]

        if entry.data_type_size > 0:
            register = register[:entry.data_type_size]

        # Ensure register is plain bytes so that slice concatenation is always
        # valid (memoryview slices do not support the + operator).
        register = bytes(register)

        # ------------------------------------------------------------------
        # Helper: swap bytes within every 16-bit word of a byte string.
        # Used for BADC / DCBA encodings (bytes_reversed=True).
        # ------------------------------------------------------------------
        def _swap_words(data: bytes) -> bytes:
            """Return *data* with the two bytes of every 16-bit word swapped."""
            out = bytearray(data)
            for i in range(0, len(out) - 1, 2):
                out[i], out[i + 1] = out[i + 1], out[i]
            return bytes(out)

        # ------------------------------------------------------------------
        # Helper: reverse the word order of a byte string whose words are
        # each 2 bytes wide.  Used for CDAB / DCBA encodings (word_reversed=True).
        # ------------------------------------------------------------------
        def _reverse_words(data: bytes, word_count: int) -> bytes:
            """Return *data* with its ``word_count`` 16-bit words in reversed order."""
            words: list[bytes] = [data[i*2:(i+1)*2] for i in range(word_count)]
            return b"".join(reversed(words))

        # Default fallback: single 16-bit unsigned read.
        _fb: bytes = register[:2]
        if word_order.bytes_reversed and len(_fb) == 2:
            _fb = bytes([_fb[1], _fb[0]])
        value: int | float | str = int.from_bytes(_fb, byteorder="big", signed=False)

        if entry.data_type == Data_Type.UINT:
            raw_bytes: bytes = register[:4]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 2)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.INT:
            raw_bytes = register[:4]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 2)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = int.from_bytes(raw_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type.UINT64:
            raw_bytes = register[:8]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 4)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.INT64:
            raw_bytes = register[:8]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 4)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = int.from_bytes(raw_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type.ACC32:
            raw_bytes = register[:4]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 2)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.FLOAT32:
            raw_bytes = register[:4]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 2)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = struct.unpack(">f", raw_bytes)[0]

        elif entry.data_type == Data_Type.FLOAT64:
            raw_bytes = register[:8]
            if word_order.word_reversed:
                raw_bytes = _reverse_words(raw_bytes, 4)
            if word_order.bytes_reversed:
                raw_bytes = _swap_words(raw_bytes)
            value = struct.unpack(">d", raw_bytes)[0]

        elif entry.data_type == Data_Type.USHORT:
            # Single register — only bytes_reversed is meaningful.
            raw_bytes = register[:2]
            if word_order.bytes_reversed:
                raw_bytes = bytes([raw_bytes[1], raw_bytes[0]])
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.SHORT:
            # Single register — only bytes_reversed is meaningful.
            raw_bytes = register[:2]
            if word_order.bytes_reversed:
                raw_bytes = bytes([raw_bytes[1], raw_bytes[0]])
            value = int.from_bytes(raw_bytes, byteorder="big", signed=True)

        elif entry.data_type in (Data_Type.BIT16_FLAGS, Data_Type.BIT8_FLAGS, Data_Type.BIT32_FLAGS):
            flag_size: int = Data_Type.getSize(entry.data_type)
            flag_word_count: int = max(1, flag_size // 16)
            flag_bytes: bytes = register[:flag_word_count * 2]
            if flag_word_count > 1 and word_order.word_reversed:
                flag_bytes = _reverse_words(flag_bytes, flag_word_count)
            if word_order.bytes_reversed:
                flag_bytes = _swap_words(flag_bytes)
            val: int = int.from_bytes(flag_bytes, byteorder="big", signed=False)

            start_bit: int = entry.register_bit if entry.register_bit >= 0 else 0
            end_bit: int = start_bit + flag_size

            if entry.documented_name + "_codes" in self.codes:
                code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")
                flags: list[str] = []
                flag_indexes: list[str] = []

                if code_dict:
                    for i in range(start_bit, end_bit):
                        if (val >> i) & 1:
                            flag_index: str = "b" + str(i)
                            flag_indexes.append(flag_index)
                            if flag_index in code_dict:
                                flags.append(code_dict[flag_index])

                multibit_flags: list[str] = [key for key in self.codes if "&" in key]
                if multibit_flags:
                    flag_indexes_set: set[str] = set(flag_indexes)
                    for multibit_flag in multibit_flags:
                        bits: list[str] = multibit_flag.split("&")
                        if all(bit in flag_indexes_set for bit in bits):
                            if multibit_flag in code_dict:
                                flags.append(code_dict[multibit_flag])

                value = ",".join(flags)
            else:
                flags: list[str] = []
                for i in range(start_bit, end_bit):
                    flags.append("1" if (val >> i) & 1 else "0")
                value = "".join(flags)

        elif entry.data_type.value > 400:  # signed-magnitude bit types
            bit_size: int = Data_Type.getSize(entry.data_type)
            bit_mask: int = (1 << bit_size) - 1
            bit_index: int = entry.register_bit
            reg_bytes: bytes = register[:2]
            if word_order.bytes_reversed:
                reg_bytes = bytes([reg_bytes[1], reg_bytes[0]])
            register_int: int = int.from_bytes(reg_bytes, byteorder="big")
            if (register_int >> bit_index) & 1:
                sign_extension: int = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> (bit_index + 1)) | sign_extension
            else:
                value = (register_int >> (bit_index + 1)) & bit_mask

        elif entry.data_type.value > 300:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1
            bit_index = entry.register_bit
            reg_bytes = register[:2]
            if word_order.bytes_reversed:
                reg_bytes = bytes([reg_bytes[1], reg_bytes[0]])
            register_int = int.from_bytes(reg_bytes, byteorder="big")
            if (register_int >> (bit_index + bit_size - 1)) & 1:
                sign_extension = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> bit_index) | sign_extension
            else:
                value = (register_int >> bit_index) & bit_mask

        elif entry.data_type == Data_Type.BYTE:
            # Single register — bytes_reversed only.
            reg_bytes = register[:2]
            if word_order.bytes_reversed:
                reg_bytes = bytes([reg_bytes[1], reg_bytes[0]])
            value = reg_bytes[0]

        elif entry.data_type.value > 200:  # unsigned bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1
            bit_index = entry.register_bit
            reg_bytes = register[:2]
            if word_order.bytes_reversed:
                reg_bytes = bytes([reg_bytes[1], reg_bytes[0]])
            register_int = int.from_bytes(reg_bytes, byteorder="big")
            value = (register_int >> bit_index) & bit_mask

        elif entry.data_type == Data_Type.HEX:
            value = bytes(register).hex()

        elif entry.data_type == Data_Type.ASCII:
            value = self._decode_text_bytes(self._ascii_fragment(bytes(register), entry))

        elif entry.data_type in (Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            value = self._decode_text_bytes(bytes(register))

        value = self._adjustments.apply_adjustments(value, entry, "post_decode")

        return value

    def _extract_bits(self, raw_value: int, entry: registry_map_entry) -> int:
        """Extract a contiguous bit field from ``raw_value`` using the entry's bit-offset metadata.

        Uses ``entry.register_bit`` as the LSB index and
        ``entry.register_bit_end`` as the MSB index (both inclusive).  Returns
        ``raw_value`` unchanged when ``register_bit < 0`` (no bit offset
        specified).  Supports single-bit extraction (``b7``) and multi-bit
        ranges (``b4-b7``).
        """
        start: int = entry.register_bit
        end: int = entry.register_bit_end

        if start < 0:
            return raw_value

        width: int = end - start + 1
        mask: int = (1 << width) - 1

        return (raw_value >> start) & mask

    def _decoded_value_already_honors_bit_offset(self, entry: registry_map_entry) -> bool:
        """Return ``True`` for decoder paths that consume ``register_bit`` internally during decoding.

        The ``_8BIT``, ``_8BIT_FLAGS``, ``_16BIT_FLAGS``, ``_32BIT_FLAGS``, and
        all variable-width bit-field types (``data_type.value > 200``) apply the
        bit offset as part of their decode logic.  Calling ``_extract_bits``
        again on their output would double-shift the value, so
        ``process_registery`` skips that step for these types.
        """
        if entry.data_type in (
            Data_Type.BIT8,
            Data_Type.BIT8_FLAGS,
            Data_Type.BIT16_FLAGS,
            Data_Type.BIT32_FLAGS,
        ):
            return True

        return entry.data_type.value > 200

    def entry_word_count(self, entry: registry_map_entry) -> int:
        """Return the number of 16-bit Modbus registers occupied by ``entry``.

        Priority order:
        1. ``entry.register_list`` — explicit address list from the "low_high"
           address format (e.g. "40_41").  Length is the definitive word count.
        2. ``entry.concatenate_registers`` — concatenated range format.
        3. ``data_type`` lookup table — legacy consecutive-address path.
        """
        if entry.register_list:
            return len(entry.register_list)

        if entry.concatenate and entry.concatenate_registers:
            return len(entry.concatenate_registers)

        word_counts: dict[Data_Type, int] = {
            Data_Type.UINT: 2,
            Data_Type.INT: 2,
            Data_Type.FLOAT32: 2,
            Data_Type.ACC32: 2,
            Data_Type.BIT32_FLAGS: 2,
            Data_Type.UINT64: 4,
            Data_Type.INT64: 4,
            Data_Type.FLOAT64: 4,
            Data_Type.STRING16: 8,
            Data_Type.STRING32: 16,
        }

        # Fixed-length strings ("STRING.16", "ASCII.16"): the size is in characters
        # (bytes), two per register.  Without this an "ASCII.16" entry counted as one
        # register, so a 16-bit-word read returned only its first two characters and
        # the read range could stop short of the string.
        if entry.data_type in (Data_Type.STRING, Data_Type.ASCII) and entry.data_type_size > 0:
            return max(1, (entry.data_type_size + 1) // 2)

        return word_counts.get(entry.data_type, 1)

    def _register_words_to_bytes(
        self,
        registry: Mapping[int, int],
        start_register: int,
        word_count: int,
        word_order: WordOrder,
        register_addresses: list[int] | None = None,
    ) -> bytes | None:
        """Assemble ``word_count`` consecutive 16-bit registers into a contiguous byte string.

        Returns ``None`` if any register in the range is absent from
        ``registry``.

        The two axes of ``word_order`` are applied independently:

        ``word_order.word_reversed``
            If True, the list of 16-bit words is reversed before serialization
            so that the low-significance word (stored at the lower register
            address by the hardware) ends up at the high end of the resulting
            byte string, as required for big-endian integer unpacking by callers.
            This covers CDAB and DCBA encodings.

        ``word_order.bytes_reversed``
            If True, each 16-bit word is serialized in little-endian byte order.
            Modbus always transmits registers in big-endian byte order on the
            wire, so this flag is only set for the unusual BADC/DCBA encodings
            where the hardware byte-swaps within each register.

        The Modbus transport always delivers each 16-bit register as a correctly
        oriented integer (per the Modbus spec); byte-swapping within a word is
        therefore a register-map-level concern handled here, not a transport concern.

        Encoding matrix:
            ABCD: word_reversed=False, bytes_reversed=False (standard Modbus)
            BADC: word_reversed=False, bytes_reversed=True
            CDAB: word_reversed=True,  bytes_reversed=False (EG4, most inverters)
            DCBA: word_reversed=True,  bytes_reversed=True  (Intel/x86 convention)
        """
        words: list[int] = []
        for offset in range(word_count):
            # When an explicit address list is supplied (the "low_high" format,
            # e.g. "40_41" or "41_40" or "40_50"), use it directly — no
            # consecutive or ascending-order assumption is made.
            # Fall back to start_register + offset for all legacy entries.
            if register_addresses and offset < len(register_addresses):
                register_num: int = register_addresses[offset]
            else:
                register_num = start_register + offset * self.register_address_step
            if register_num not in registry:
                return None
            words.append(registry[register_num] & 0xFFFF)

        if word_order.word_reversed:
            # Low-significance word is at the lower register address.
            # Reverse so the high word is first — callers always unpack big-endian.
            words.reverse()

        # Bytes within each word: Modbus wire format is big-endian (bytes_reversed=False).
        # bytes_reversed=True only for BADC/DCBA where the hardware byte-swaps each word.
        per_word_byteorder: str = "little" if word_order.bytes_reversed else "big"
        return b"".join(
            word.to_bytes(2, byteorder=per_word_byteorder, signed=False) for word in words
        )

    def _swap_bytes_16(self, val: int) -> int:
        """Swap the high and low bytes of a 16-bit integer."""
        return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)

    @staticmethod
    def _ascii_fragment(raw_bytes: bytes, entry: registry_map_entry) -> bytes:
        """Prepare the bytes of one register of a concatenated ASCII range for decoding.

        A range member ("1008~1013") is a fragment of a longer string, not a null-terminated
        field.  Some devices (ABB's native Modbus maps) send one character per register in
        the low byte, so a member reads ``00 31``; skipping the NUL keeps the character
        where ``_decode_text_bytes`` would stop at it and return nothing.  Whole-field
        entries (no range) keep the null-terminator behavior.
        """
        return raw_bytes.replace(b"\x00", b"") if entry.concatenate else raw_bytes

    def _decode_text_bytes(self, raw_bytes: bytes) -> str:
        """Decode raw register bytes as a printable ASCII string.

        Behavior matches the EG4 BMS firmware's ``extract_ascii_string_``
        and is appropriate for any Modbus device that uses fixed-length
        null-padded or space-padded ASCII fields:

        * Accepts only bytes in the printable ASCII range ``0x20``-``0x7E``.
        * Stops at the first null byte ``0x00`` (null terminator).
        * Silently drops all other bytes — non-printable control characters,
          high bytes (``0x80``-``0xFE``), and ``0xFF`` register-padding.
          Using ``errors="replace"`` would substitute ``\ufffd`` for these,
          polluting the string with garbage characters.
        * Strips trailing spaces from the final result (leading spaces are
          preserved — some devices left-pad strings).

        This replaces the previous ``raw_bytes.decode("utf-8", errors="replace")``
        approach which passed ``\ufffd`` replacement characters through to the
        output for every non-ASCII byte.
        """
        result: list[str] = []
        for b in raw_bytes:
            if b == 0x00:
                break                   # null terminator — discard remainder
            if 0x20 <= b <= 0x7E:
                result.append(chr(b))   # printable ASCII — keep
            # else: 0x01-0x1F control chars, 0x7F DEL, 0x80-0xFF high/padding — drop
        return "".join(result).rstrip()

    def process_register_ushort(self, registry: Mapping[int, int], entry: registry_map_entry) -> int | float | str | None:
        """Process a ushort (integer-per-register) registry entry into a typed value.

        All multi-register types delegate to ``_register_words_to_bytes`` which
        handles both word-order reversal (``WordOrder.word_reversed``) and
        per-word byte swapping (``WordOrder.bytes_reversed``) independently,
        covering all four ABCD/BADC/CDAB/DCBA encoding conventions.

        For single-register types only ``bytes_reversed`` is relevant: when True
        the two bytes within the 16-bit register are swapped before the value is
        extracted.  ``word_reversed`` has no meaning for a single word.
        """
        word_order: WordOrder = self._adjustments.get_entry_byteorder(entry)

        if entry.data_type == Data_Type.UINT:
            register_bytes: bytes | None = self._register_words_to_bytes(registry, entry.register, 2, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.UINT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.INT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type.ACC32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.FLOAT32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = struct.unpack(">f", register_bytes)[0]

        elif entry.data_type == Data_Type.FLOAT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = struct.unpack(">d", register_bytes)[0]

        elif entry.data_type == Data_Type.SHORT:
            # Single-register signed int.  Only bytes_reversed matters here
            # (there is no second word to re-order).  Swap the two bytes within
            # the register when the hardware uses BADC/DCBA byte ordering.
            raw = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            if raw & (1 << 15):
                value = raw - (1 << 16)
            else:
                value = raw

        elif entry.data_type == Data_Type.INT:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order, entry.register_list or None)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type.BIT8:
            # Single-register sub-byte field.  Swap bytes first when the
            # hardware delivers them in reversed order (bytes_reversed=True).
            raw = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            start_bit = entry.register_bit if entry.register_bit >= 0 else 0
            value = (raw >> start_bit) & 0xFF

        elif entry.data_type in (Data_Type.BIT8_FLAGS, Data_Type.BIT16_FLAGS, Data_Type.BIT32_FLAGS):
            bit_size: int = Data_Type.getSize(entry.data_type)
            total_registers: int = max(1, bit_size // 16)
            if total_registers > 1:
                # Multi-register flags: both word_reversed and bytes_reversed apply.
                flag_bytes: bytes | None = self._register_words_to_bytes(registry, entry.register, total_registers, word_order, entry.register_list or None)
                if flag_bytes is None:
                    return None
                val: int = int.from_bytes(flag_bytes, byteorder="big", signed=False)
            else:
                # Single register: only bytes_reversed applies.
                val = registry[entry.register] & 0xFFFF
                if word_order.bytes_reversed:
                    val = self._swap_bytes_16(val)

            start_bit: int = entry.register_bit if entry.register_bit >= 0 else 0
            end_bit: int = start_bit + bit_size

            code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")

            flags: list[str] = []
            for i in range(start_bit, end_bit):
                if (val >> i) & 1:
                    if code_dict:
                        flag_index: str = "b" + str(i)
                        if flag_index in code_dict:
                            flags.append(code_dict[flag_index])
                    else:
                        flags.append("1")
                elif not code_dict:
                    flags.append("0")

            value = ",".join(flags) if code_dict else "".join(flags)

        elif entry.data_type.value > 400:  # signed-magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index: int = entry.register_bit if entry.register_bit >= 0 else 0
            register_int = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                register_int: int = self._swap_bytes_16(register_int)
            sign_bit: int = (register_int >> bit_index) & 1
            magnitude: int = (register_int >> (bit_index + 1)) & ((1 << (bit_size - 1)) - 1)
            value = -magnitude if sign_bit else magnitude

        elif entry.data_type.value > 300:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            register_int = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                register_int = self._swap_bytes_16(register_int)
            raw_bits: int = (register_int >> bit_index) & ((1 << bit_size) - 1)
            sign_mask: int = 1 << (bit_size - 1)
            if raw_bits & sign_mask:
                value = raw_bits - (1 << bit_size)
            else:
                value = raw_bits

        elif entry.data_type.value > 200:  # unsigned bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            register_int = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                register_int = self._swap_bytes_16(register_int)
            value = (register_int >> bit_index) & ((1 << bit_size) - 1)

        elif entry.data_type == Data_Type.BYTE:
            # Extract the low byte of the register.  Swap first when the
            # hardware delivers bytes in reversed order (BADC/DCBA).
            raw: int = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            value = raw & 0xFF

        elif entry.data_type == Data_Type.HEX:
            raw = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            value = raw.to_bytes(2, byteorder="big").hex()

        elif entry.data_type == Data_Type.ASCII:
            # A range member ("115~119") is one register whose text is concatenated with
            # its neighbors later; a sized entry ("ASCII.16") holds its whole string.
            ascii_words: int = 1 if entry.concatenate or entry.data_type_size <= 0 else self.entry_word_count(entry)
            raw_bytes: bytes | None = self._register_words_to_bytes(registry, entry.register, ascii_words, word_order, entry.register_list or None)
            if raw_bytes is None:
                return None
            if entry.data_type_size > 0 and not entry.concatenate:
                raw_bytes = raw_bytes[:entry.data_type_size]
            value = self._decode_text_bytes(self._ascii_fragment(raw_bytes, entry))

        elif entry.data_type in (Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            word_count: int = self.entry_word_count(entry)
            raw_bytes = self._register_words_to_bytes(registry, entry.register, word_count, word_order, entry.register_list or None)
            if raw_bytes is None:
                return None
            if entry.data_type_size > 0:
                raw_bytes = raw_bytes[:entry.data_type_size]
            value = self._decode_text_bytes(raw_bytes)

        else:
            # USHORT fallback — single register.  Swap bytes if bytes_reversed.
            raw = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            value = raw

        value: int | float | str = self._adjustments.apply_adjustments(value, entry, "post_decode")

        # Collapse whole floats to int (e.g. 52.0 → 52) after scaling
        if isinstance(value, float) and value.is_integer():
            value = int(value)

        return value

    def process_registery(
        self,
        registry: Mapping[int, int | bytes | tuple[bytes, float]],
        registry_map: list[registry_map_entry],
    ) -> dict[str, int | float | str]:
        """Decode a raw register snapshot into a dict of typed, named values.

        Dispatches each entry to ``process_register_bytes`` (for ``bytes`` or
        ``tuple`` values) or ``process_register_ushort`` (for plain ``int``
        values).  Skips entries with a ``description_source`` (handled in a
        second pass) and entries whose register is absent from ``registry``.
        After all values are decoded, applies any ``context``-stage adjustments
        that depend on sibling values.  In a final pass, resolves
        ``description_source`` entries by looking up the decoded source value
        in the entry's code dict and populating a human-readable description
        string.  Concatenated registers are accumulated until all component
        registers have been decoded before their combined value is emitted.
        """
        concatenate_registry: dict[int, int | float | str] = {}
        info: dict[str, int | float | str] = {}

        for entry in registry_map:
            if entry.description_source:
                continue
            if entry.register not in registry:
                continue

            raw: int | bytes | tuple[bytes, float] = registry[entry.register]

            # _WATCH5 = {'tinner','tradiator1','tradiator2','tbat',
            #            'maxcelltemp_bms','mincelltemp_bms','batcurrent_bms',
            #            'epv1_all','epv2_all','epv1_all_l','runningtime'}
            # if entry.variable_name in _WATCH5:
            #     self._log.warning(
            #         f"[DEBUG-DISPATCH] {entry.variable_name} "
            #         f"reg={entry.register} dtype={entry.data_type.name} "
            #         f"adj={entry.adjustments} "
            #         f"raw_type={type(raw).__name__} "
            #         f"raw_value={raw!r}"
            #     )
            if isinstance(raw, (bytes, tuple)):
                bytes_registry: Mapping[int, bytes | tuple[bytes, float]] = cast(Mapping[int, bytes | tuple[bytes, float]], registry)
                value: int | float | str | None = self.process_register_bytes(bytes_registry, entry)
            else:
                int_registry: Mapping[int, int] = cast(Mapping[int, int], registry)
                value = self.process_register_ushort(int_registry, entry)

            if value is None:
                self._log.debug(f"Skipping '{entry.variable_name}' — partial read")
                continue

            if (
                isinstance(value, (int, float))
                and entry.register_bit >= 0
                and not self._decoded_value_already_honors_bit_offset(entry)
            ):
                value = self._extract_bits(int(value), entry)

            if entry.concatenate:
                concatenate_registry[entry.register] = value

                all_exist = True
                for key in entry.concatenate_registers:
                    if key not in concatenate_registry:
                        all_exist = False
                        break
                if all_exist:
                    concatenated_value = ""
                    for key in entry.concatenate_registers:
                        concatenated_value = concatenated_value + str(concatenate_registry[key])
                        del concatenate_registry[key]

                    if entry.data_type == Data_Type.ASCII:
                        concatenated_value: str = concatenated_value.replace("\x00", " ").strip()

                    info[entry.variable_name] = concatenated_value
            else:
                info[entry.variable_name] = value

        # Protocol-level named expressions ("context_variables" in the JSON) are evaluated once from the
        # decoded values, then offered to every row's Context formula behind the real variables.
        derived_context: dict[str, float] = self._adjustments.evaluate_context_variables(info)
        context_view: Mapping[str, int | float | str] = ChainMap(info, cast(dict[str, int | float | str], derived_context))
        for entry in registry_map:
            if entry.variable_name in info:
                info[entry.variable_name] = self._adjustments.apply_adjustments(info[entry.variable_name], entry, "context", context_view)

        entries_by_name: dict[str, registry_map_entry] = {entry.variable_name: entry for entry in registry_map}
        for entry in registry_map:
            if not entry.description_source:
                continue
            source_name: str = entry.description_source
            if source_name not in info:
                continue
            source_entry: registry_map_entry | None = entries_by_name.get(source_name)
            if source_entry is None:
                continue
            description: str | None = self._code_description_for_value(source_entry, info[source_name])
            if description is not None:
                info[entry.variable_name] = description

        return info

    def validate_registry_entry(self, entry: registry_map_entry, val: str | int | float) -> int:
        """Validate ``val`` against the entry's configured constraints and return 1 if valid, 0 otherwise.

        Validation priority: (1) if the entry has a code dict, ``val`` must
        appear as a key (accepts both raw string and integer-normalized forms);
        (2) for ASCII/string types, ``val`` must be a non-empty alphanumeric
        string and, if ``value_regex`` is set, must match it (concatenated
        entries return the register count on success); (3) for all other types,
        ``val`` is coerced to ``int`` and checked against ``[value_min,
        value_max]``.  Logs a warning for non-convertible values and an error
        for out-of-range integers; never raises.
        """
        code_dict: dict[str, str] = self.get_entry_code_dict(entry)
        if code_dict:
            lookup_keys: list[str] = [str(val)]
            try:
                lookup_keys.insert(0, str(int(float(val))))
            except (TypeError, ValueError):
                pass
            for key in lookup_keys:
                if key in code_dict:
                    return 1
            return 0

        if entry.data_type in (Data_Type.ASCII, Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            if not isinstance(val, str):
                self._log.warning(
                    f"validate_registry_entry: expected str for ASCII entry "
                    f"'{entry.variable_name}', got {type(val).__name__} — skipping"
                )
                return 0

            if val and not re.match(r"[^a-zA-Z0-9_\-]", val):
                if entry.value_regex:
                    if re.match(entry.value_regex, val):
                        if entry.concatenate:
                            return len(entry.concatenate_registers)
                        return 1
                    return 0
                return 1

        try:
            intval: int = int(float(val))
        except (ValueError, TypeError):
            self._log.warning(f"validate_registry_entry: cannot convert '{val}' to int for entry '{entry.variable_name}'")
            return 0

        if intval >= entry.value_min and intval <= entry.value_max:
            return 1

        self._log.error(
            f"validate_registry_entry '{entry.variable_name}' fail (INT) "
            f"{intval} != {entry.value_min}~{entry.value_max}"
        )

        return 0

    def _init_discovery_settings(self) -> None:
        """Read the discovery declaration from the protocol JSON and seed ``anchors`` with its defaults.

        ``"discovery"`` names the provider to run on connect (see ``classes/discovery``); it is absent for static
        protocols, which then never pay for discovery.  ``"discovery_defaults"`` is an object of anchor name to
        value (for SolarEdge: where each SunSpec model normally sits).  Template rows are resolved from these
        defaults at load time, so the registry map is complete before anything has been discovered and tools
        that read the map without a device (tests, the web UI, protocol analysis) keep working.
        """
        self.discovery_provider = self.settings.get("discovery", "").strip().lower()

        defaults: dict[str, str | float | int] = {}
        raw_defaults: object = self.codes.get("discovery_defaults")
        if isinstance(raw_defaults, dict):
            for name, value in cast("dict[str, object]", raw_defaults).items():
                if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                    defaults[str(name)] = value
                else:
                    self._log.warning(f"[{self.protocol}] ignoring discovery_defaults entry '{name}': expected a number or string")
        elif raw_defaults is not None:
            self._log.warning(f"[{self.protocol}] ignoring discovery_defaults: expected a JSON object")

        self.anchor_defaults = defaults
        self.anchors = dict(defaults)

    @property
    def is_dynamic(self) -> bool:
        """Return True when the protocol JSON declares a discovery provider or discovery probes.

        Static protocols (the great majority) return False, and the transport skips discovery entirely.
        """
        return bool(self.discovery_provider) or "discovery_probes" in self.codes

    def _expand_template_row(self, row: dict[str, str], registry_type: Registry_Type) -> list[dict[str, str]]:
        """Expand one CSV row that uses anchors into the concrete row(s) the normal parser can read.

        A row is a template when its register cell contains a ``[...]`` expression or any of its register /
        variable name / documented name cells contains ``{i}``.  ``[anchor+offset]`` is resolved from
        ``anchors``.  ``{i}`` repeats the row once per instance (1-based); the instance count is the anchor
        ``<stem>_count`` named by the ``<stem>_{i}_`` part of the register cell, and a count of zero or a
        missing count anchor yields no rows (the equipment is not present).

        Resolved rows get ``_dynamic = "1"`` so the entry is marked ``dynamic``.  Rows that still contain an
        unresolved ``[...]`` pass through unchanged and are deferred by ``process_row`` exactly as before.
        Plain rows, comments and everything in a static protocol come back untouched as a one-item list.
        """
        register: str = (row.get("register") or "").strip()
        has_instance_token: bool = any(_INSTANCE_TOKEN in (row.get(key) or "") for key in _TEMPLATE_ROW_KEYS)

        if not has_instance_token and "[" not in register:
            return [row]
        if register.startswith("#") or (row.get("variable name") or "").startswith("#"):
            return [row]

        self.dynamic_registry_types.add(registry_type)

        instances: list[int | None] = [None]
        if has_instance_token:
            stem_match: re.Match[str] | None = _INSTANCE_STEM_REGEX.search(register)
            if stem_match is None:
                self._log.warning(
                    f"[{self.protocol}] template row '{row.get('variable name')}' uses {_INSTANCE_TOKEN} but its register "
                    f"cell has no '<name>_{_INSTANCE_TOKEN}_' anchor to take the instance count from; row skipped"
                )
                return []
            count_name: str = f"{stem_match.group('stem')}_count"
            try:
                count: int = int(self.anchors.get(count_name, 0))
            except (TypeError, ValueError):
                self._log.warning(f"[{self.protocol}] anchor '{count_name}' is not a number; treating as 0 instances")
                count = 0
            instances = list(range(1, count + 1))

        expanded: list[dict[str, str]] = []
        for instance in instances:
            new_row: dict[str, str] = dict(row)
            if instance is not None:
                for key in _TEMPLATE_ROW_KEYS:
                    new_row[key] = (new_row.get(key) or "").replace(_INSTANCE_TOKEN, str(instance))
            resolved: list[str] = self.evaluate_expressions(new_row.get("register") or "", self.anchors)
            if len(resolved) == 1 and "[" not in resolved[0]:
                new_row["register"] = resolved[0]
                new_row["_dynamic"] = "1"
            expanded.append(new_row)
        return expanded

    def apply_anchors(
        self,
        discovered: Mapping[str, str | float | int],
        owned_patterns: Sequence[re.Pattern[str]] = (),
    ) -> bool:
        """Rebuild the dynamic registry maps from ``discovered`` anchors laid over the JSON defaults.

        Scheduling path: N/A — runs on connect / reconnect from the transport's discovery step, never inside a read cycle.

        The anchors are always ``anchor_defaults`` overlaid with ``discovered``, never accumulated across
        calls.  Default anchors whose names match one of ``owned_patterns`` (the names the discovery provider is
        authoritative for) are dropped first, so equipment that has disappeared since the last discovery leaves
        the map instead of lingering from the defaults.  Returns False without touching the maps when the result
        equals the anchors they were already built with.
        """
        kept_defaults: dict[str, str | float | int] = {
            name: value
            for name, value in self.anchor_defaults.items()
            if not any(pattern.match(name) for pattern in owned_patterns)
        }
        merged: dict[str, str | float | int] = {**kept_defaults, **discovered}
        if merged == self.anchors:
            return False
        self.anchors = merged
        self.rebuild_dynamic_maps()
        return True

    def rebuild_dynamic_maps(self) -> list[Registry_Type]:
        """Reload every registry map that contains template rows so it reflects the current ``anchors``.

        Scheduling path: N/A — see ``apply_anchors``.

        Reuses ``load_registry_map``, so masks, screens, ``_l``/``_h`` merging, read ranges and
        ``registry_map_size`` are all recomputed by the same code that built the original map.  Returns the
        registry types that were reloaded (empty for a static protocol).
        """
        rebuilt: list[Registry_Type] = []
        for registry_type in sorted(self.dynamic_registry_types, key=lambda item: item.value):
            self.load_registry_map(registry_type)
            rebuilt.append(registry_type)
        return rebuilt

    def evaluate_expressions(self, expression: str, variables: Mapping[str, str | float | int]) -> list[str]:
        """Resolve a dynamic register expression and return the list of concrete register name strings it expands to.

        Three transform passes are applied in order: (1) variable substitution —
        ``[name]`` tokens are replaced with the matching value from ``variables``, and a
        token that is an expression over known names (``[model_103_1_base+14]``,
        ``[1~meter_count]``) has each name replaced in place and keeps its brackets so the
        later passes can finish it;
        (2) range expansion — ``[x~y]`` tokens are expanded into one copy of the
        expression per integer in ``[x, y]``, recursively; (3) arithmetic
        evaluation — ``[expr]`` tokens containing only digits and operators are
        replaced with their computed result via ``safe_eval_expression``.
        Unresolvable tokens are left unchanged.
        """

        identifier_pattern: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

        def evaluate_variables(expr: str) -> str:
            """Replace ``[name]`` tokens with their values from ``variables``; leave unmatched tokens unchanged."""
            var_pattern: re.Pattern[str] = re.compile(r"\[([^\[\]]+)\]")

            def replace_vars(match: re.Match[str]) -> str:
                """Return the variable's value as a string, or the original token if a name in it is not in ``variables``."""
                var_name: str = match.group(1)
                if var_name in variables:
                    return str(variables[var_name])
                names: list[str] = identifier_pattern.findall(var_name)
                if not names or any(name not in variables for name in names):
                    return match.group(0)
                # Expression over known names: substitute in place and keep the brackets for the math/range passes.
                return "[" + identifier_pattern.sub(lambda name: str(variables[name.group(0)]), var_name) + "]"

            return var_pattern.sub(replace_vars, expr)

        def evaluate_ranges(expr: str) -> list[str]:
            """Expand the first ``[x~y]`` range token in ``expr`` recursively, returning one string per integer in the range."""
            range_pattern: re.Pattern[str] = re.compile(r"\[.*?(?P<start>\d+)\s*~\s*(?P<end>\d+).*?\]")
            match: re.Match[str] | None = range_pattern.search(expr)

            if not match:
                return [expr]

            range_start: int = int(match.group("start"))
            range_end: int = int(match.group("end"))

            if range_start > range_end:
                range_start, range_end = range_end, range_start

            results: list[str] = []
            for i in range(range_start, range_end + 1):
                replaced: str = expr[:match.start()] + str(i) + expr[match.end():]
                results.extend(evaluate_ranges(replaced))

            return results

        def evaluate_math(expr: str) -> str:
            """Replace all ``[arithmetic_expr]`` tokens in ``expr`` with their computed numeric values."""
            math_pattern: re.Pattern[str] = re.compile(r"\[(?P<maths>[0-9\+\-\*\/%\(\)\.\s]+)\]")

            def replace_maths(match: re.Match[str]) -> str:
                """Evaluate one arithmetic token; return the original token unchanged on any error."""
                try:
                    maths: str = match.group("maths")
                    result: int | float = self._adjustments.safe_eval_expression(maths)
                    return str(result)
                except Exception:
                    return match.group(0)

            return math_pattern.sub(replace_maths, expr)

        substituted: str = evaluate_variables(expression)
        expanded: list[str] = evaluate_ranges(substituted)
        return [evaluate_math(r) for r in expanded]

    def resolve_dynamic_registry_entries(self, live_values: Mapping[str, int | float | str]) -> None:
        """Resolve deferred dynamic register expressions using ``live_values`` and load the resulting entries.

        Rows whose register cell held a ``[expression]`` that could not be resolved at load time are
        parked in ``dynamic_registry_rows``.  This merges ``live_values`` into ``anchors`` and reloads the
        affected registry maps (see ``rebuild_dynamic_maps``), so every row that can now be resolved becomes a
        real ``registry_map_entry`` (with ``dynamic=True``) and the rest stay deferred.  Callers may invoke it
        repeatedly as more values become known; it is a no-op when nothing is deferred.

        Logs the number of rows that left the deferred list.
        """
        if not self.dynamic_registry_rows:
            return

        deferred_before: int = len(self.dynamic_registry_rows)
        self.anchors = {**self.anchors, **live_values}
        self.rebuild_dynamic_maps()
        resolved_count: int = max(deferred_before - len(self.dynamic_registry_rows), 0)

        if resolved_count:
            self._log.info(f"Resolved {resolved_count} dynamic registry entries")

    def reset_register_timestamps(self) -> None:
        """Reset ``next_read_timestamp`` to ``0.0`` for every entry in all loaded registry maps.

        Forces all entries to be treated as immediately due on the next
        ``calculate_registry_ranges`` call.  Used after a reconnect to ensure a
        full re-read of all registers rather than waiting out the normal polling
        intervals.
        """
        for registry_type in Registry_Type:
            if registry_type in self.registry_map:
                for entry in self.registry_map[registry_type]:
                    entry.next_read_timestamp = 0.0
        self._log.debug(f"Reset timestamps for all registry entries in protocol {self.protocol}")
