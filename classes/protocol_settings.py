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
import os
import re
import struct
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, cast

from defs.common import TransportSettings, strtoint_safe


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
    ZERO = 0x00

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
    adjustments: dict[str, Any]
    concatenate: bool
    concatenate_registers: list[int]

    values: list
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

    def __str__(self) -> str:
        """Return the entry's ``variable_name`` as its string representation."""
        return self.variable_name

    def __eq__(self, other) -> bool:
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
    """

    def __init__(self, log: logging.Logger, default_word_order: WordOrder = _DEFAULT_WORD_ORDER) -> None:
        """Initialise with a shared logger and the protocol-level default ``WordOrder``.

        ``default_word_order`` is resolved from the JSON settings file's
        ``byteorder`` key (via ``_WORD_ORDER_ALIASES``) so it always reflects
        the correct protocol default (almost always ``WORD_ORDER_ABCD`` for
        standard Modbus).  Individual entries may override it via
        ``Register_Endian`` in their adjustments column.
        """
        self._log: logging.Logger = log
        self.default_word_order: WordOrder = default_word_order

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_adjustments(self, raw_adjustments: str | None) -> dict[str, Any]:
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
                    return parsed
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

    def get_adjustment(self, entry: "registry_map_entry", name: str) -> Any | None:
        """Return the value for *name* from ``entry.adjustments`` (case-insensitive)."""
        target: str = name.lower()
        for key, value in entry.adjustments.items():
            if key.lower() == target:
                return value
        return None

    def get_entry_byteorder(self, entry: "registry_map_entry") -> WordOrder:
        """Return the effective ``WordOrder`` for *entry*.

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
            endian = self.get_adjustment(entry, "Register_Endian")
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

        Stages
        ------
        byteorder
            Determine byte order before raw bytes are decoded.
            Only relevant when the entry carries a ``Register_Endian``
            adjustment.
        post_decode
            Apply ``unit_mod`` scaling and any numeric adjustments
            (``Offset``, ``High_Low``) after the raw value has been decoded.
            ``unit_mod`` is **always** applied here regardless of whether
            other adjustments are present.
        context
            Apply conditional transforms that depend on sibling metric values
            (e.g. direction-aware sign flipping).
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
            high_low: Any | None = self.get_adjustment(entry, "High_Low") if entry.adjustments else None
            if high_low is not None:
                adjusted = self.apply_range_formula(float(adjusted), str(high_low))
            else:
                if entry.unit_mod != 1.0:
                    adjusted = adjusted * entry.unit_mod

                if entry.adjustments:
                    offset: Any | None = self.get_adjustment(entry, "Offset")
                    if offset is not None:
                        try:
                            adjusted = adjusted + float(offset)
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

            context_adjustment: Any | None = self.get_adjustment(entry, "Context")
            if not isinstance(context_adjustment, dict):
                return value

            key: str = str(context_adjustment.get("key", "")).strip()
            if not key or key not in context:
                self._log.debug(
                    f"Context adjustment for {entry.variable_name} waiting for key '{key}'"
                )
                return value

            cases = context_adjustment.get("cases", {})
            formula = ""
            context_value: int | float | str = context[key]
            if isinstance(cases, dict):
                formula = str(cases.get(str(context_value), cases.get(context_value, "")))
            if not formula:
                formula = str(context_adjustment.get("default", ""))
            if not formula:
                return value

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

    def apply_range_formula(self, raw_value: float, logic: str) -> float:
        """Apply conditional range formulas based on *raw_value*.

        Used by the ``High_Low`` adjustment key.  Each block in *logic* takes
        the form ``x?(lo,hi)->formula`` and the matching block's formula is
        evaluated with ``x`` substituted by *raw_value*.
        """
        cleaned_logic: str = logic.replace(" ", "")

        pattern = (
            r"x\?\s*[\(\[]\s*([\-0-9.]+)\s*,\s*([\-0-9.]+)\s*[\)\]]\s*->\s*(.+?)(?=x\?|$)"
        )
        logic_blocks = re.findall(pattern, cleaned_logic)

        for lower, upper, formula in logic_blocks:
            lower_f = float(lower)
            upper_f = float(upper)

            if lower_f < raw_value <= upper_f:
                expression = formula.replace("x", str(raw_value))
                try:
                    evaluated: int | float = self.safe_eval_expression(expression)
                    self._log.debug(
                        f"Successful range formula evaluation {evaluated} for value {raw_value}"
                    )
                    return float(evaluated)
                except Exception as e:
                    self._log.warning(
                        f"Failed range formula evaluation {expression} for value {raw_value}: {e}"
                    )
                    return raw_value

        return raw_value

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

                ops: dict[type, Any] = {
                    ast.Add:      lambda a, b: a + b,
                    ast.Sub:      lambda a, b: a - b,
                    ast.Mult:     lambda a, b: a * b,
                    ast.Div:      lambda a, b: a / b,
                    ast.FloorDiv: lambda a, b: a // b,
                    ast.Mod:      lambda a, b: a % b,
                    ast.Pow:      lambda a, b: a ** b,
                }

                op_type = type(node.op)
                if op_type not in ops:
                    msg = f"Unsupported operator: {op_type.__name__}"
                    raise ValueError(msg)
                return ops[op_type](left, right)

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
        self.dynamic_registry_rows: list[dict[str, str]] = []
        self.dynamic_registry_resolved = False
        self.codes: dict[str, str | dict[str, str]] = {}
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
        self.variable_screen = self._load_filter_file(screen_file)

        self.load__json()

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
        self._adjustments: DataAdjustments = DataAdjustments(self._log, self.word_order)

        for registry_type in Registry_Type:
            self.load_registry_map(registry_type)

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

    def get_registry_map(self, registry_type: Registry_Type = Registry_Type.ZERO) -> list[registry_map_entry]:
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

    def get_code_by_value(self, entry: registry_map_entry, value: str, fallback: str) -> str:
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
        value: str | dict[str, str] | None = self.codes.get(key)
        if isinstance(value, dict):
            return value
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

    def load_registry_overrides(self, override_path: str, keys: list[str]) -> dict[str, dict[str, Any]]:
        """Parse a CSV override file and return a nested dict keyed by each column in ``keys``.

        For each row, normalizes the value of every ``keys`` column (strip,
        lowercase, spaces to underscores) and indexes the full row under that
        value.  The result allows O(1) lookup of override rows by either
        ``documented name`` or ``register``, so the caller can match and apply
        overrides without a linear scan.
        """
        overrides = {key: {} for key in keys}

        with open(override_path, newline="", encoding="latin-1") as csvfile:
            reader: csv.DictReader[str] = csv.DictReader(csvfile)
            for row in reader:
                for key in keys:
                    if key in row:
                        row[key] = row[key].strip().lower().replace(" ", "_")
                        key_value = row[key]
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
            r"(?P<register>\d{1,5}|0x[0-9A-Fa-f]{1,4})"
            r"(?:\.b(?P<bit_start>\d{1,2})(?:-(?P<bit_end>\d{1,2}))?)?"
            r"(?:\.(?P<byte>\d{1,2}))?"
        )

        read_interval_regex: re.Pattern[str] = re.compile(r"(?P<value>[\.\d]+)(?P<unit>[xs]|ms)")
        data_type_regex: re.Pattern[str] = re.compile(r"(?P<datatype>\w+)\.(?P<length>\d+)")
        range_regex: re.Pattern[str] = re.compile(r"(?P<reverse>r|)(?P<start>(?:0?x[\da-z]+|[\d]+))[\-~](?P<end>(?:0?x[\da-z]+|[\d]+))")
        ascii_value_regex: re.Pattern[str] = re.compile(r"(?P<regex>^\[.+\]$)")
        list_regex: re.Pattern[str] = re.compile(r"\s*(?:(?P<range_start>(?:0?x[\da-z]+|[\d]+))-(?P<range_end>(?:0?x[\da-z]+|[\d]+))|(?P<element>[^,\s][^,]*?))\s*(?:,|$)")

        transport_read_interval: int = 1000
        if self.transport_settings is not None:
            transport_read_interval = self.transport_settings.getint("read_interval", transport_read_interval)

        if not os.path.exists(path):
            return registry_map

        overrides: dict[str, dict] | None = None
        override_keys: list[str] = ["documented name", "register"]
        overrided_keys = set()

        override_path: str = path[:-4] + ".override.csv"

        if os.path.exists(override_path):
            self._log.info("loading override file: " + override_path)
            overrides = self.load_registry_overrides(override_path, override_keys)

        def determine_delimiter(first_row) -> str:
            """Detect whether the CSV uses semicolons or commas as its column delimiter."""
            if first_row.count(";") > first_row.count(","):
                return ";"
            else:
                return ","

        def process_row(row) -> None:
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
            adjustments: dict[str, Any] = self._adjustments.parse_adjustments(row.get("adjustments", ""))

            # Strip \r from all field values at parse time to handle Windows CRLF CSVs
            row = {k: v.strip("\r") if isinstance(v, str) else v for k, v in row.items()}

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
                override_row = None
                for key in override_keys:
                    key_value = row.get(key)
                    if key_value and key_value in overrides[key]:
                        override_row = overrides[key][key_value]
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
            values: list = []
            value_min: int = 0
            value_max: int = 65535
            if data_type in (Data_Type.UINT, Data_Type.ACC32, Data_Type.FLOAT32):
                value_max = 0xFFFFFFFF
            elif data_type in (Data_Type.UINT64, Data_Type.FLOAT64):
                value_max = 0xFFFFFFFFFFFFFFFF
            elif data_type == Data_Type.BYTE:
                value_max = 0xFF
            value_regex: str = ""
            value_is_json: bool = False

            if "{" in row["values"]:
                try:
                    codes_json = json.loads(row["values"])
                    value_is_json = True
                    name = row["documented name"] + "_codes"
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
                    val_match: re.Match[str] | None = range_regex.search(row["values"])
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

            register: int = -1
            register_bit: int = -1
            register_bit_end: int = -1
            register_byte: int = -1

            row["register"] = row["register"].lower()
            reg_match: re.Match[str] | None = register_regex.search(row["register"])

            if reg_match:
                try:
                    register: int = strtoint_safe(
                        reg_match.group("register"),
                        context="register address"
                    )

                    bit_start_str: str = reg_match.group("bit_start")
                    bit_end_str: str = reg_match.group("bit_end")

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
                range_match: re.Match[str] | None = range_regex.search(row["register"])
                if not range_match:
                    if "[" in row["register"]:
                        self._log.info(f"Deferred dynamic register expression: {row['register']}")
                        deferred_row = dict(row)
                        deferred_row["_registry_type"] = registry_type
                        self.dynamic_registry_rows.append(deferred_row)
                        return
                    else:
                        register = strtoint_safe(row["register"])
                else:
                    reverse = range_match.group("reverse")
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
                entry_kwargs = {
                    "registry_type": registry_type,
                    "register": register,
                    "register_bit": register_bit,
                    "register_bit_end": register_bit_end,
                    "register_byte": register_byte,
                    "variable_name": variable_name,
                    "documented_name": row["documented name"],
                    "unit": str(unit_symbol),
                    "unit_mod": unit_multiplier,
                    "adjustments": adjustments,
                    "data_type": data_type,
                    "data_type_size": data_type_len,
                    "note": note,
                    "concatenate": concatenate,
                    "concatenate_registers": concatenate_registers,
                    "values": values,
                    "value_min": value_min,
                    "value_max": value_max,
                    "value_regex": value_regex,
                    "read_command": read_command,
                    "read_interval": read_interval,
                    "write_mode": writeMode,
                    "has_enum_mapping": value_is_json,
                }

                item = registry_map_entry(**entry_kwargs)
                registry_map.append(item)
                register = register + 1

        with open(path, newline="", encoding="latin-1") as csvfile:
            delimeter = ";"
            first_row = next(csvfile).strip("\r\n").lower().replace("_", " ")
            if first_row.count(";") < first_row.count(","):
                delimeter = ","

            first_row: str = re.sub(r"\s+" + re.escape(delimeter) + "|" + re.escape(delimeter) + r"\s+", delimeter, first_row)
            csvfile_iter: itertools.chain[str] = itertools.chain([first_row], csvfile)
            reader: csv.DictReader[str] = csv.DictReader(csvfile_iter, delimiter=delimeter)

            for row in reader:
                process_row(row)

            if overrides is not None:
                for key in override_keys:
                    applied = False
                    for key_value, override_row in overrides[key].items():
                        if all(override_row.get(k) for k in override_keys):
                            if all(override_row.get(k) not in overrided_keys for k in override_keys):
                                self._log.info("Loading unique entry from overrides for both unique keys")
                                process_row(override_row)
                                for k in override_keys:
                                    overrided_keys.add(override_row.get(k))
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
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() not in self.variable_mask
                        and item.variable_name.strip().lower() not in self.variable_mask
                        and (item.documented_name.strip().lower() + "_l") not in self.variable_mask
                        and (item.variable_name.strip().lower() + "_l") not in self.variable_mask
                    ):
                        del registry_map[index]

            # Apply variable screen (denylist)
            if self.variable_screen:
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

                        register_end: int = register.register + self.entry_word_count(register) - 1

                        if window_min is None or register.register < window_min:
                            window_min: int | None = register.register
                        if window_max is None or register_end > window_max:
                            window_max: int | None = register_end

            if window_min is not None and window_max is not None:
                ranges.append((window_min, window_max - window_min + 1))

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
        ``ZERO``, ``<protocol>.<type>_registry_map.csv`` for all others).
        After loading, walks the entries to determine the highest register
        address for ``registry_map_size``, then pre-computes and caches the
        initial read ranges in ``registry_map_ranges``.  Silently returns when
        the file cannot be located.
        """
        if not settings_dir:
            settings_dir = self.settings_dir

        if not file:
            if registry_type == Registry_Type.ZERO:
                file = self.protocol + ".registry_map.csv"
            else:
                file = self.protocol + "." + registry_type.name.lower() + "_registry_map.csv"

        path: str | None = self.find_protocol_file(file, settings_dir)

        if not path:
            return

        self.registry_map[registry_type] = self.load__registry(path, registry_type)

        size: int = 0
        for item in self.registry_map[registry_type]:
            item_end_register: int = item.register + self.entry_word_count(item) - 1
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
            words = [data[i*2:(i+1)*2] for i in range(word_count)]
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

        elif entry.data_type in (Data_Type._16BIT_FLAGS, Data_Type._8BIT_FLAGS, Data_Type._32BIT_FLAGS):
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
                flags = []
                for i in range(start_bit, end_bit):
                    flags.append("1" if (val >> i) & 1 else "0")
                value = "".join(flags)

        elif entry.data_type.value > 400:  # signed-magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1
            bit_index = entry.register_bit
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
            value = self._decode_text_bytes(bytes(register))

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
            Data_Type._8BIT,
            Data_Type._8BIT_FLAGS,
            Data_Type._16BIT_FLAGS,
            Data_Type._32BIT_FLAGS,
        ):
            return True

        return entry.data_type.value > 200

    def entry_word_count(self, entry: registry_map_entry) -> int:
        """Return the number of 16-bit Modbus registers occupied by ``entry``.

        For concatenated entries the count is the length of
        ``concatenate_registers``.  Variable-length ``STRING`` entries derive
        the count from ``data_type_size`` (rounding up to whole words).  All
        other types use a fixed lookup table (``UINT``/``INT``/``FLOAT32``/
        ``ACC32`` → 2, ``UINT64``/``FLOAT64`` → 4, ``STRING16`` → 8,
        ``STRING32`` → 16); any type not in the table defaults to 1.
        """
        if entry.concatenate and entry.concatenate_registers:
            return len(entry.concatenate_registers)

        word_counts: dict[Data_Type, int] = {
            Data_Type.UINT: 2,
            Data_Type.INT: 2,
            Data_Type.FLOAT32: 2,
            Data_Type.ACC32: 2,
            Data_Type._32BIT_FLAGS: 2,
            Data_Type.UINT64: 4,
            Data_Type.FLOAT64: 4,
            Data_Type.STRING16: 8,
            Data_Type.STRING32: 16,
        }

        if entry.data_type == Data_Type.STRING and entry.data_type_size > 0:
            return max(1, (entry.data_type_size + 1) // 2)

        return word_counts.get(entry.data_type, 1)

    def _register_words_to_bytes(
        self,
        registry: Mapping[int, int],
        start_register: int,
        word_count: int,
        word_order: WordOrder,
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
            register_num: int = start_register + offset
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

    def _decode_text_bytes(self, raw_bytes: bytes) -> str:
        """Decode ``raw_bytes`` as UTF-8, replace invalid sequences, strip null characters and surrounding whitespace."""
        return raw_bytes.decode("utf-8", errors="replace").replace("\x00", "").strip()

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
            register_bytes: bytes | None = self._register_words_to_bytes(registry, entry.register, 2, word_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.UINT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, word_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.ACC32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.FLOAT32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order)
            if register_bytes is None:
                return None
            value = struct.unpack(">f", register_bytes)[0]

        elif entry.data_type == Data_Type.FLOAT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, word_order)
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
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, word_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type._8BIT:
            # Single-register sub-byte field.  Swap bytes first when the
            # hardware delivers them in reversed order (bytes_reversed=True).
            raw = registry[entry.register] & 0xFFFF
            if word_order.bytes_reversed:
                raw = self._swap_bytes_16(raw)
            start_bit = entry.register_bit if entry.register_bit >= 0 else 0
            value = (raw >> start_bit) & 0xFF

        elif entry.data_type in (Data_Type._8BIT_FLAGS, Data_Type._16BIT_FLAGS, Data_Type._32BIT_FLAGS):
            bit_size: int = Data_Type.getSize(entry.data_type)
            total_registers = max(1, bit_size // 16)
            if total_registers > 1:
                # Multi-register flags: both word_reversed and bytes_reversed apply.
                flag_bytes = self._register_words_to_bytes(registry, entry.register, total_registers, word_order)
                if flag_bytes is None:
                    return None
                val = int.from_bytes(flag_bytes, byteorder="big", signed=False)
            else:
                # Single register: only bytes_reversed applies.
                val = registry[entry.register] & 0xFFFF
                if word_order.bytes_reversed:
                    val = self._swap_bytes_16(val)

            start_bit: int = entry.register_bit if entry.register_bit >= 0 else 0
            end_bit: int = start_bit + bit_size

            code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")

            flags = []
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
            raw_bytes: bytes | None = self._register_words_to_bytes(registry, entry.register, 1, word_order)
            if raw_bytes is None:
                return None
            value = self._decode_text_bytes(raw_bytes)

        elif entry.data_type in (Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            word_count: int = self.entry_word_count(entry)
            raw_bytes = self._register_words_to_bytes(registry, entry.register, word_count, word_order)
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

        for entry in registry_map:
            if entry.variable_name in info:
                info[entry.variable_name] = self._adjustments.apply_adjustments(info[entry.variable_name], entry, "context", info)

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

    def evaluate_expressions(self, expression: str, variables: dict[str, str | float | int]) -> list[str]:
        """Resolve a dynamic register expression and return the list of concrete register name strings it expands to.

        Three transform passes are applied in order: (1) variable substitution —
        ``[name]`` tokens are replaced with the matching value from ``variables``;
        (2) range expansion — ``[x~y]`` tokens are expanded into one copy of the
        expression per integer in ``[x, y]``, recursively; (3) arithmetic
        evaluation — ``[expr]`` tokens containing only digits and operators are
        replaced with their computed result via ``safe_eval_expression``.
        Unresolvable tokens are left unchanged.
        """

        def evaluate_variables(expr: str) -> str:
            """Replace ``[name]`` tokens with their values from ``variables``; leave unmatched tokens unchanged."""
            var_pattern: re.Pattern[str] = re.compile(r"\[([^\[\]]+)\]")

            def replace_vars(match: re.Match[str]) -> str:
                """Return the variable's value as a string, or the original token if the name is not in ``variables``."""
                var_name = match.group(1)
                if var_name in variables:
                    return str(variables[var_name])
                return match.group(0)

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
                    maths: str | Any = match.group("maths")
                    result: int | float = self._adjustments.safe_eval_expression(maths)
                    return str(result)
                except Exception:
                    return match.group(0)

            return math_pattern.sub(replace_maths, expr)

        substituted: str = evaluate_variables(expression)
        expanded: list[str] = evaluate_ranges(substituted)
        return [evaluate_math(r) for r in expanded]

    def resolve_dynamic_registry_entries(self, live_values: dict[str, int | float | str]) -> None:
        """Attempt to resolve deferred dynamic register expressions using ``live_values``.
        NOTE not implemented yet TODO as a full dependency resolution since the expected use case
        is simple one-level variable substitution (e.g. register names like ``[prefix][1~3]`` where
        ``prefix`` is defined in a static registry row and the range expands to concrete register names).
        More complex cases with interdependent variables or multi-level nesting may require multiple calls to this
        method as values become resolvable in stages, but should still resolve correctly as long as there are
        no circular dependencies.

        Iterates ``dynamic_registry_rows`` — rows whose register field contained
        a ``[variable]`` expression that could not be resolved at load time.  For
        each row, calls ``evaluate_expressions`` with the current ``live_values``
        dict.  Rows that resolve successfully are removed from the deferred list;
        rows that fail (missing variables or evaluation error) remain for the next
        call.  Logs the count of newly resolved entries when any succeed.
        """
        if not self.dynamic_registry_rows:
            return

        resolved_count = 0

        for row in self.dynamic_registry_rows.copy():
            try:
                resolved_registers: list[str] = self.evaluate_expressions(row["register"], live_values)

                for resolved_register in resolved_registers:
                    resolved_row: dict[str, str] = dict(row)
                    resolved_row["register"] = resolved_register
                    self._log.info(f"Resolved dynamic register {row['register']} -> {resolved_register}")
                    resolved_count += 1

                self.dynamic_registry_rows.remove(row)

            except Exception as e:
                self._log.warning(f"Failed resolving dynamic register '{row['register']}': {e}")

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
