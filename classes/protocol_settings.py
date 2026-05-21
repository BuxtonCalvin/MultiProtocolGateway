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
        sizes = {
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

        member_name = alias.get(name, "READ")
        return cls[member_name]


class Registry_Type(Enum):
    ZERO = 0x00
    ''' for protocols that don't have a command / registry type '''
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

    def __str__(self):
        return self.variable_name

    def __eq__(self, other):
        return (
            isinstance(other, registry_map_entry)
            and self.register == other.register
            and self.register_bit == other.register_bit
            and self.register_bit_end == other.register_bit_end
            and self.registry_type == other.registry_type
            and self.register_byte == other.register_byte
        )

    def __hash__(self):
        return hash((self.variable_name, self.register_bit, self.register_byte, self.registry_type))


class protocol_settings:

    @classmethod
    def get_transport_type(cls, protocol_version: str, settings_dir: str = "protocols") -> str:
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

        # Default byteorder for interpreting multi-byte values.
        # Separate from transport-level endianness: the transport always delivers
        # 16-bit registers in big-endian byte order per the Modbus spec.
        # This setting controls how multi-register (32/64-bit) values are assembled,
        # and can be overridden per-entry via Register_Endian in the adjustments column.
        self.byteorder: Literal['little', 'big'] = "big"

        self._log_level = getattr(logging, logging.getLevelName(logging.getLogger().getEffectiveLevel()), logging.INFO)
        self._log: logging.Logger = logging.getLogger(__name__)
        self._log.setLevel(self._log_level)

        # DEBUG-IDENTITY: confirms this exact file is executing
        import os as _os
        self._log.warning(
            f"[DEBUG-IDENTITY] protocol_settings loaded from: {_os.path.abspath(__file__)} "
            f"| BUILD-TAG: 2026-05-19-FIX-ALL "
            f"| protocol={protocol}"
        )

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
        self.variable_screen: list[str] = []
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
            if raw_byteorder in ('little', 'big'):
                self.byteorder = cast(Literal['little', 'big'], raw_byteorder)
            else:
                self._log.warning(f"Invalid byteorder '{raw_byteorder}' in protocol settings — using default 'big'")

        for registry_type in Registry_Type:
            self.load_registry_map(registry_type)

    def _load_filter_file(self, filename: str) -> list[str]:
        """
        Loads a line-delimited filter file (mask or screen) and returns
        a list of cleaned, lowercased metric names.
        Returns an empty list if the file is not found or cannot be read.
        """
        file_path: str | None = self.find_protocol_file(filename, "config")

        if not file_path or not os.path.isfile(file_path):
            self._log.debug(f"Filter file '{filename}' not found for protocol '{self.protocol}' — skipping.")
            return []

        entries: list[str] = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    clean_line = line.strip().lower()
                    if not clean_line or clean_line.startswith('#'):
                        continue
                    entries.append(clean_line)
        except Exception as e:
            self._log.error(f"Error reading filter file '{filename}': {e}")

        self._log.debug(f"Loaded {len(entries)} entries from filter file '{filename}'")
        return entries

    def get_registry_map(self, registry_type: Registry_Type = Registry_Type.ZERO) -> list[registry_map_entry]:
        return self.registry_map[registry_type]

    def get_registry_ranges(self, registry_type: Registry_Type) -> list[tuple[int, int]]:
        return self.registry_map_ranges[registry_type]

    def get_registry_entry(self, name: str, registry_type: Optional[Registry_Type] = None) -> Optional[registry_map_entry]:
        """Retrieve a registry entry, optionally filtering by type."""
        cleaned_name = name.strip().lower().replace(" ", "_")

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
        if value is None or entry is None:
            return fallback

        value = value.strip().lower()
        for code, description in self.get_entry_code_dict(entry).items():
            if value == description.lower():
                return code
        return fallback

    def get_code_dict(self, key: str) -> dict[str, str]:
        """
        Returns the code mapping for a given key, or an empty dict if
        the key doesn't exist or is not a dict.
        """
        value: str | dict[str, str] | None = self.codes.get(key)
        if isinstance(value, dict):
            return value
        return {}

    def get_entry_code_dict(self, entry: registry_map_entry) -> dict[str, str]:
        """
        Return the code mapping for an entry, accepting both documented-name
        and variable-name conventions.
        """
        for key_base in (entry.documented_name, entry.variable_name):
            code_dict = self.get_code_dict(key_base + "_codes")
            if code_dict:
                return code_dict
        return {}

    def _code_description_for_value(self, entry: registry_map_entry, value: int | float | str) -> str | None:
        code_dict = self.get_entry_code_dict(entry)
        if not code_dict:
            return None

        lookup_keys = [str(value)]
        try:
            lookup_keys.insert(0, str(int(float(value))))
        except (TypeError, ValueError):
            pass

        for key in lookup_keys:
            if key in code_dict:
                return code_dict[key]
        return None

    def load__json(self, file: str = "", settings_dir: str = "") -> None:
        """
        JSON file serves as both protocol config and code mapping file,
        separated by key naming convention. Settings keys go to self.settings;
        _codes-suffixed keys stay in self.codes only.
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

        for key, value in self.codes.items():
            if not key.endswith("_codes") and isinstance(value, str):
                self.settings[key] = value

    def load_registry_overrides(self, override_path: str, keys: list[str]) -> dict[str, dict[str, Any]]:
        """Load overrides into a multidimensional dictionary keyed by each specified key."""
        overrides = {key: {} for key in keys}

        with open(override_path, newline="", encoding="latin-1") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                for key in keys:
                    if key in row:
                        row[key] = row[key].strip().lower().replace(" ", "_")
                        key_value = row[key]
                        if key_value:
                            overrides[key][key_value] = row
        return overrides

    def load__registry(self, path: str, registry_type: Registry_Type = Registry_Type.INPUT) -> list[registry_map_entry]:
        registry_map: list[registry_map_entry] = []

        register_regex = re.compile(
            r"(?P<register>\d{1,5}|0x[0-9A-Fa-f]{1,4})"
            r"(?:\.b(?P<bit_start>\d{1,2})(?:-(?P<bit_end>\d{1,2}))?)?"
            r"(?:\.(?P<byte>\d{1,2}))?"
        )

        read_interval_regex = re.compile(r"(?P<value>[\.\d]+)(?P<unit>[xs]|ms)")
        data_type_regex = re.compile(r"(?P<datatype>\w+)\.(?P<length>\d+)")
        range_regex = re.compile(r"(?P<reverse>r|)(?P<start>(?:0?x[\da-z]+|[\d]+))[\-~](?P<end>(?:0?x[\da-z]+|[\d]+))")
        ascii_value_regex = re.compile(r"(?P<regex>^\[.+\]$)")
        list_regex = re.compile(r"\s*(?:(?P<range_start>(?:0?x[\da-z]+|[\d]+))-(?P<range_end>(?:0?x[\da-z]+|[\d]+))|(?P<element>[^,\s][^,]*?))\s*(?:,|$)")

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
            if first_row.count(";") > first_row.count(","):
                return ";"
            else:
                return ","

        def process_row(row) -> None:
            unit_multiplier: float = 1
            unit_symbol: str = ""
            read_interval: int = 0
            adjustments: dict[str, Any] = self.parse_adjustments(row.get("adjustments", ""))

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

            variable_name = row["variable name"] if row["variable name"] else row["documented name"]
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

                matches = data_type_regex.search(row["data type"])
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
                    data_type = data_type_parsed

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
                note = row["note"]
            else:
                note = ""

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
                    register = strtoint_safe(
                        reg_match.group("register"),
                        context="register address"
                    )

                    bit_start_str = reg_match.group("bit_start")
                    bit_end_str = reg_match.group("bit_end")

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
                range_match = range_regex.search(row["register"])
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
            first_row = next(csvfile).lower().replace("_", " ")
            if first_row.count(";") < first_row.count(","):
                delimeter = ","

            first_row = re.sub(r"\s+" + re.escape(delimeter) + "|" + re.escape(delimeter) + r"\s+", delimeter, first_row)
            csvfile = itertools.chain([first_row], csvfile)

            reader = csv.DictReader(csvfile, delimiter=delimeter)

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
            _l_candidates = [
                (registry_map[i].documented_name, registry_map[i+1].documented_name)
                for i in range(len(registry_map) - 1)
                if registry_map[i].documented_name.endswith('_l')
            ]
            self._log.warning(
                f"[DEBUG-MERGE] path={path} "
                f"_l_candidates (index, index+1)={_l_candidates}"
            )

            for index in reversed(range(len(registry_map) - 1)):
                item = registry_map[index]
                next_item = registry_map[index + 1]
                if (
                    item.documented_name.endswith("_l")
                    and next_item.documented_name == item.documented_name[:-2] + "_h"
                ):
                    # _l row is the surviving combined entry
                    combined_item = item

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

                    self._log.warning(
                        f"[DEBUG-MERGE] MERGED: '{combined_item.variable_name}' "
                        f"reg={combined_item.register} "
                        f"dtype={combined_item.data_type} "
                        f"unit_mod={combined_item.unit_mod} "
                        f"adj={combined_item.adjustments}"
                    )
                    del registry_map[index + 1]

            # Apply variable mask (allowlist)
            if self.variable_mask:
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() not in self.variable_mask
                        and item.variable_name.strip().lower() not in self.variable_mask
                    ):
                        del registry_map[index]

            # Apply variable screen (denylist)
            if self.variable_screen:
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() in self.variable_screen
                        or item.variable_name.strip().lower() in self.variable_screen
                    ):
                        del registry_map[index]

            self._add_code_description_entries(registry_map)

            return registry_map

    def _add_code_description_entries(self, registry_map: list[registry_map_entry]) -> None:
        """
        Add synthetic _desc entries for any entry that has a code mapping but
        doesn't already have a description entry. This allows code descriptions
        to be automatically included in the output without needing to be manually
        added to the registry map CSV.
        """
        existing_names = {entry.variable_name for entry in registry_map}
        additions: list[registry_map_entry] = []

        for entry in registry_map:
            if entry.description_source:
                continue
            if not self.get_entry_code_dict(entry):
                continue

            desc_name = f"{entry.variable_name}_desc"
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
                    note=f"Decoded description for {entry.variable_name}",
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
        """
        Read optimization: calculate which register ranges to read so the Modbus
        driver can combine multiple registers into a single request (function
        0x03/0x04), reducing round-trips.
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
            end = start + max_batch_size

            window_min = None
            window_max = None

            for register in registry_map:
                if start <= register.register < end:
                    if register.write_mode in (WriteMode.READDISABLED, WriteMode.WRITEONLY):
                        continue

                    if init or register.next_read_timestamp < timestamp_ms:
                        if not init:
                            register.next_read_timestamp = timestamp_ms + register.read_interval

                        register_end = register.register + self.entry_word_count(register) - 1

                        if window_min is None or register.register < window_min:
                            window_min = register.register
                        if window_max is None or register_end > window_max:
                            window_max = register_end

            if window_min is not None and window_max is not None:
                ranges.append((window_min, window_max - window_min + 1))

        return ranges

    @staticmethod
    def find_protocol_file(file: str, base_dir: str = "") -> Optional[str]:
        """
        Searches for a protocol file by name across known locations.
        Static so it can be called from get_transport_type without a full instance.
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
            item_end_register = item.register + self.entry_word_count(item) - 1
            if item_end_register > size:
                size = item_end_register

        self.registry_map_size[registry_type] = size
        self._log.debug(f"load_registry_map: {registry_type.name} - loaded {len(self.registry_map[registry_type])} entries, max_register={size}")
        self.registry_map_ranges[registry_type] = self.calculate_registry_ranges(self.registry_map[registry_type], self.registry_map_size[registry_type], init=True)

    def process_register_bytes(self, registry: Mapping[int, bytes | tuple[bytes, float]], entry: registry_map_entry) -> int | float | str | None:
        """Process a bytes-oriented registry entry into a typed value.

        Endian contract for the bytes transport path:
          The transport delivers raw wire bytes. For single-register (16-bit) types
          the Modbus spec defines big-endian byte order on the wire, so the bytes
          buffer is always interpreted as big-endian regardless of Register_Endian.
          Register_Endian:little only affects multi-register (32/64-bit) types where
          it controls word ordering: the low word arrives at the lower register address
          so the words must be reversed before big-endian interpretation.
        """

        raw: bytes | tuple[bytes, float] = registry[entry.register]

        if isinstance(raw, tuple):
            register: bytes = raw[0]
        else:
            register = raw

        # word_order controls multi-register word assembly only.
        # Single-register reads always use big-endian byte interpretation
        # because the transport delivers Modbus wire bytes unchanged.
        word_order: Literal['little', 'big'] = self.get_entry_byteorder(entry)

        if entry.register_byte > 0:
            register = register[entry.register_byte:]

        if entry.data_type_size > 0:
            register = register[:entry.data_type_size]

        # Ensure register is plain bytes so that slice concatenation
        # (used for word-order reversal below) is always valid.
        # memoryview slices do not support + concatenation.
        register = bytes(register)

        # Default fallback: single 16-bit read.
        # Swap bytes for little-endian single-register entries.
        _fb = register[:2]
        if word_order == "little" and len(_fb) == 2:
            _fb = bytes([_fb[1], _fb[0]])
        value: int | float | str = int.from_bytes(_fb, byteorder="big", signed=False)

        if entry.data_type == Data_Type.UINT:
            # Multi-word: word_order controls whether low or high word came first.
            # Reverse the byte buffer when little-endian (low word at lower address).
            raw_bytes = register[:4]
            if word_order == "little":
                # Swap word order: bytes [W0_hi, W0_lo, W1_hi, W1_lo]
                # become [W1_hi, W1_lo, W0_hi, W0_lo]
                raw_bytes = register[2:4] + register[0:2]
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        elif entry.data_type == Data_Type.INT:
            raw_bytes = register[:4]
            if word_order == "little":
                raw_bytes = register[2:4] + register[0:2]
            value = int.from_bytes(raw_bytes, byteorder="big", signed=True)
        elif entry.data_type == Data_Type.UINT64:
            raw_bytes = register[:8]
            if word_order == "little":
                # Reverse 4 words of 2 bytes each
                raw_bytes = register[6:8] + register[4:6] + register[2:4] + register[0:2]
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        elif entry.data_type == Data_Type.ACC32:
            raw_bytes = register[:4]
            if word_order == "little":
                raw_bytes = register[2:4] + register[0:2]
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        elif entry.data_type == Data_Type.FLOAT32:
            raw_bytes = register[:4]
            if word_order == "little":
                raw_bytes = register[2:4] + register[0:2]
            value = struct.unpack(">f", raw_bytes)[0]
        elif entry.data_type == Data_Type.FLOAT64:
            raw_bytes = register[:8]
            if word_order == "little":
                raw_bytes = register[6:8] + register[4:6] + register[2:4] + register[0:2]
            value = struct.unpack(">d", raw_bytes)[0]
        elif entry.data_type == Data_Type.USHORT:
            # Single register. Swap bytes for little-endian entries.
            raw_bytes = register[:2]
            if word_order == "little":
                raw_bytes = bytes([raw_bytes[1], raw_bytes[0]])
            value = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        elif entry.data_type == Data_Type.SHORT:
            # Single register. Wire bytes are Modbus big-endian.
            # For Register_Endian:little the hardware sends bytes swapped,
            # so reverse them before sign-extending.
            raw_bytes = register[:2]
            _WATCH3 = {'tinner','tradiator1','tradiator2','tbat',
                       'maxcelltemp_bms','mincelltemp_bms','batcurrent_bms'}
            if entry.variable_name in _WATCH3:
                self._log.warning(
                    f"[DEBUG-BYTES-SHORT] {entry.variable_name} "
                    f"wire={raw_bytes.hex()} word_order={word_order}"
                )
            if word_order == "little":
                raw_bytes = bytes([raw_bytes[1], raw_bytes[0]])
            value = int.from_bytes(raw_bytes, byteorder="big", signed=True)
            if entry.variable_name in _WATCH3:
                self._log.warning(
                    f"[DEBUG-BYTES-SHORT] {entry.variable_name} "
                    f"after_swap={raw_bytes.hex()} value={value}"
                )
        elif entry.data_type in (Data_Type._16BIT_FLAGS, Data_Type._8BIT_FLAGS, Data_Type._32BIT_FLAGS):
            # Single or double-register flags. Always big-endian wire bytes per Modbus spec.
            val: int = int.from_bytes(register, byteorder="big", signed=False)
            start_bit: int = 0
            end_bit: int = 16
            flag_size: int = Data_Type.getSize(entry.data_type)

            if isinstance(value, (int, float)) and entry.register_bit >= 0:
                start_bit = entry.register_bit

            end_bit = flag_size + start_bit

            if entry.documented_name + "_codes" in self.codes:
                code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")
                flags: list[str] = []
                flag_indexes: list[str] = []

                if code_dict:
                    # use the integer value decoded with the correct byte_order
                    # rather than indexing raw bytes directly. The previous approach
                    # used register_bytes[byte] which assumes big-endian physical
                    # byte layout and gives wrong results for little-endian entries.
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
                    if (val >> i) & 1:
                        flags.append("1")
                    else:
                        flags.append("0")
                value = "".join(flags)

        elif entry.data_type.value > 400:  # signed-magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1
            bit_index = entry.register_bit
            register_int: int = int.from_bytes(register, byteorder="big")
            if (register_int >> bit_index) & 1:
                sign_extension: int = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> (bit_index + 1)) | sign_extension
            else:
                value = (register_int >> (bit_index + 1)) & bit_mask

        elif entry.data_type.value > 300:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1
            bit_index = entry.register_bit
            register_int: int = int.from_bytes(register, byteorder="big")
            if (register_int >> (bit_index + bit_size - 1)) & 1:
                sign_extension = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> bit_index) | sign_extension
            else:
                value = (register_int >> bit_index) & bit_mask

        elif entry.data_type == Data_Type.BYTE:
            # Single register — always big-endian wire bytes.
            value = int.from_bytes(register[:1], byteorder="big", signed=False)

        elif entry.data_type.value > 200:  # unsigned bit types
            bit_size: int = Data_Type.getSize(entry.data_type)
            bit_mask: int = (1 << bit_size) - 1
            bit_index: int = entry.register_bit
            if isinstance(register, bytes):
                register_int = int.from_bytes(register, byteorder="big")
                value = (register_int >> bit_index) & bit_mask

        elif entry.data_type == Data_Type.HEX:
            register_bytes: bytes = bytes(register)
            value = register_bytes.hex()

        elif entry.data_type == Data_Type.ASCII:
            value = self._decode_text_bytes(bytes(register))
        elif entry.data_type in (Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            value = self._decode_text_bytes(bytes(register))

        value = self.apply_adjustments(value, entry, "post_decode")

        return value

    def _extract_bits(self, raw_value: int, entry: registry_map_entry) -> int:
        """
        Extract bits from raw_value using entry.register_bit and register_bit_end.
        Supports single bit (b7) and multi-bit range (b4-7).
        """
        start: int = entry.register_bit
        end: int = entry.register_bit_end

        if start < 0:
            return raw_value

        width: int = end - start + 1
        mask: int = (1 << width) - 1

        return (raw_value >> start) & mask

    def _decoded_value_already_honors_bit_offset(self, entry: registry_map_entry) -> bool:
        """
        Returns True for data-type decoders that already consume
        register_bit/register_bit_end during decoding. Running _extract_bits()
        again would double-shift the value.
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
        if entry.concatenate and entry.concatenate_registers:
            return len(entry.concatenate_registers)

        word_counts = {
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
        byte_order: Literal['little', 'big'],
    ) -> bytes | None:
        """
        Assemble multiple 16-bit Modbus registers into a contiguous byte string.

        Endian contract (per EG4 hardware spec):
          big (default): high word at lower register address, bytes within each
                         word in big-endian order — standard Modbus.
          little:        low word at lower register address AND bytes within each
                         word are also reversed (LE-LE).  Used only for registers
                         explicitly annotated with Register_Endian:little in the
                         CSV adjustments column.

        NOTE: the Modbus transport always delivers each 16-bit register as a
        correctly oriented integer — byte swapping within a word is therefore a
        register-map-level concern, not a transport concern.
        """
        words: list[int] = []
        for offset in range(word_count):
            register_num = start_register + offset
            if register_num not in registry:
                return None
            words.append(registry[register_num] & 0xFFFF)

        if byte_order == "little":
            # Reverse word order: low word was at the lower register address,
            # so after reversal the high word is first in the byte string,
            # matching big-endian struct unpacking used by callers.
            words.reverse()

        # Bytes within each word follow the same byte_order.
        # For big (default): standard network order — no swap needed.
        # For little: bytes within each word are also reversed per hardware spec.
        return b"".join(
            word.to_bytes(2, byteorder=byte_order, signed=False) for word in words
        )

    def _swap_bytes_16(self, val: int) -> int:
        """Swap the high and low bytes of a 16-bit integer."""
        return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)

    def _decode_text_bytes(self, raw_bytes: bytes) -> str:
        return raw_bytes.decode("utf-8", errors="replace").replace("\x00", "").strip()

    def parse_adjustments(self, raw_adjustments: str | None) -> dict[str, Any]:
        """
        Parse the CSV adjustments field.
        Canonical form is a JSON object. Simple shorthand such as "Offset:-50"
        is also accepted.
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

    def get_adjustment(self, entry: registry_map_entry, name: str) -> Any | None:
        target: str = name.lower()
        for key, value in entry.adjustments.items():
            if key.lower() == target:
                return value
        return None

    def get_entry_byteorder(self, entry: registry_map_entry) -> Literal['little', 'big']:
        """
        Return the effective byte order for this entry.
        Defaults to the protocol-level byteorder; can be overridden per-entry
        via Register_Endian in the CSV adjustments column.
        """
        byte_order: Literal['little', 'big'] = self.byteorder
        if entry.adjustments:
            endian = self.get_adjustment(entry, "Register_Endian")
            if endian is not None:
                endian_str = str(endian).strip().lower()
                if endian_str in ("little", "le"):
                    _WATCH = {'tinner','tradiator1','tradiator2','tbat',
                              'maxcelltemp_bms','mincelltemp_bms','batcurrent_bms',
                              'epv1_all','epv2_all','echg_all','erec_all'}
                    if entry.variable_name in _WATCH:
                        self._log.warning(
                            f"[DEBUG-BYTEORDER] {entry.variable_name} adj={entry.adjustments} "
                            f"→ byte_order=little"
                        )
                    return "little"
                if endian_str in ("big", "be"):
                    return "big"
                self._log.warning(f"Unsupported Register_Endian '{endian}' for {entry.variable_name}")
        else:
            _WATCH2 = {'tinner','tradiator1','tradiator2','tbat',
                       'maxcelltemp_bms','mincelltemp_bms','batcurrent_bms'}
            if entry.variable_name in _WATCH2:
                self._log.warning(
                    f"[DEBUG-BYTEORDER] {entry.variable_name} adjustments=EMPTY "
                    f"→ byte_order=big (annotation missing!)"
                )
        return byte_order

    def apply_adjustments(
        self,
        value: int | float | str,
        entry: registry_map_entry,
        stage: Literal["byteorder", "post_decode", "context"],
        context: Mapping[str, int | float | str] | None = None,
    ) -> int | float | str:
        """
        Central adjustment dispatcher for CSV-driven post-processing.

        Stages:
          byteorder  — determine byte order before raw bytes are decoded.
                       Only relevant when entry has a Register_Endian adjustment.
          post_decode — apply unit_mod scaling and any numeric adjustments
                        (Offset, High_Low) after the raw value has been decoded.
                        unit_mod is ALWAYS applied here regardless of whether
                        other adjustments are present.
          context    — apply conditional transforms that depend on sibling
                       metric values (e.g. direction-aware sign flipping).
        """

        # ------------------------------------------------------------------ #
        # byteorder stage                                                      #
        # Early-exit is correct here: if there are no adjustments there is    #
        # nothing to do. byte order comes from get_entry_byteorder() which    #
        # already handles the default, so this stage is only reached when the  #
        # caller explicitly needs to resolve Register_Endian.                  #
        # ------------------------------------------------------------------ #
        if stage == "byteorder":
            if not entry.adjustments:
                return value
            endian = self.get_adjustment(entry, "Register_Endian")
            if endian is None:
                return value
            endian_str: str = str(endian).strip().lower()
            if endian_str in ("little", "le"):
                return "little"
            if endian_str in ("big", "be"):
                return "big"
            self._log.warning(f"Unsupported Register_Endian adjustment '{endian}' for {entry.variable_name}")
            return value

        # ------------------------------------------------------------------ #
        # post_decode stage                                                    #
        #                                                                      #
        # previously the function returned early when entry.adjustments   #
        # was empty, which silently skipped unit_mod scaling for the majority  #
        # of registers (those with no explicit adjustments in the CSV).        #
        # unit_mod must always be applied here; it is independent of the       #
        # adjustments dict.                                                    #
        # ------------------------------------------------------------------ #
        if stage == "post_decode":
            if not isinstance(value, (int, float)):
                return value

            adjusted: int | float = value

            # High_Low encodes the scaling formula directly (e.g. x/1000),
            # so unit_mod must NOT also be applied — the formula is the
            # complete transform.
            high_low = self.get_adjustment(entry, "High_Low") if entry.adjustments else None
            if high_low is not None:
                adjusted = self.apply_range_formula(float(adjusted), str(high_low))
            else:
                # Apply unit_mod unconditionally — this is the primary scaling
                # path for the vast majority of registers.
                if entry.unit_mod != 1.0:
                    adjusted = adjusted * entry.unit_mod

                # Apply Offset after scaling if present
                if entry.adjustments:
                    offset = self.get_adjustment(entry, "Offset")
                    if offset is not None:
                        try:
                            adjusted = adjusted + float(offset)
                        except (TypeError, ValueError):
                            self._log.warning(f"Unsupported Offset adjustment '{offset}' for {entry.variable_name}")

            # Collapse float to int when the value is whole (e.g. 52.0 → 52)
            if isinstance(adjusted, float) and adjusted.is_integer():
                return int(adjusted)
            return adjusted

        # ------------------------------------------------------------------ #
        # context stage                                                        #
        # Early-exit is correct here: context transforms are opt-in and only  #
        # apply when an explicit Context adjustment is defined.                #
        # ------------------------------------------------------------------ #
        if stage == "context":
            if not entry.adjustments:
                return value
            if context is None or not isinstance(value, (int, float)):
                return value

            context_adjustment = self.get_adjustment(entry, "Context")
            if not isinstance(context_adjustment, dict):
                return value

            key: str = str(context_adjustment.get("key", "")).strip()
            if not key or key not in context:
                self._log.debug(f"Context adjustment for {entry.variable_name} waiting for key '{key}'")
                return value

            cases = context_adjustment.get("cases", {})
            formula = ""
            context_value = context[key]
            if isinstance(cases, dict):
                formula = str(cases.get(str(context_value), cases.get(context_value, "")))
            if not formula:
                formula = str(context_adjustment.get("default", ""))
            if not formula:
                return value

            expression = formula.replace("x", str(float(value)))
            try:
                adjusted_context = self.safe_eval_expression(expression)
                if isinstance(adjusted_context, float) and adjusted_context.is_integer():
                    return int(adjusted_context)
                else:
                    return adjusted_context
            except Exception as e:
                self._log.warning(f"Failed context adjustment '{formula}' for {entry.variable_name}: {e}")
                return value

        return value  # unreachable but satisfies type checker

    def process_register_ushort(self, registry: Mapping[int, int], entry: registry_map_entry) -> int | float | str | None:
        """Process a ushort (integer-per-register) registry entry into a typed value."""

        byte_order: Literal['little', 'big'] = self.get_entry_byteorder(entry)

        if entry.data_type == Data_Type.UINT:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.UINT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, byte_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.ACC32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)

        elif entry.data_type == Data_Type.FLOAT32:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return None
            value = struct.unpack(">f", register_bytes)[0]

        elif entry.data_type == Data_Type.FLOAT64:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 4, byte_order)
            if register_bytes is None:
                return None
            value = struct.unpack(">d", register_bytes)[0]

        elif entry.data_type == Data_Type.SHORT:
            # FIX: byte_order was fetched above but never used for SHORT.
            # For Register_Endian:little entries (e.g. batcurrent_bms,
            # maxcelltemp_bms, mincelltemp_bms) the two bytes within the
            # 16-bit register must be swapped before sign-extending.
            raw = registry[entry.register] & 0xFFFF
            _WATCH4 = {'tinner','tradiator1','tradiator2','tbat',
                       'maxcelltemp_bms','mincelltemp_bms','batcurrent_bms'}
            if entry.variable_name in _WATCH4:
                self._log.warning(
                    f"[DEBUG-USHORT-SHORT] {entry.variable_name} "
                    f"raw=0x{raw:04X} byte_order={byte_order}"
                )
            if byte_order == "little":
                raw = self._swap_bytes_16(raw)
            if raw & (1 << 15):
                value = raw - (1 << 16)
            else:
                value = raw
            if entry.variable_name in _WATCH4:
                self._log.warning(
                    f"[DEBUG-USHORT-SHORT] {entry.variable_name} "
                    f"after_swap=0x{raw:04X} value={value}"
                )

        elif entry.data_type == Data_Type.INT:
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return None
            value = int.from_bytes(register_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type._8BIT:
            # FIX: byte_order ignored previously. Swap bytes before extracting
            # the target byte so high/low byte selection is correct for
            # little-endian entries.
            raw = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                raw = self._swap_bytes_16(raw)
            start_bit = entry.register_bit if entry.register_bit >= 0 else 0
            value = (raw >> start_bit) & 0xFF

        elif entry.data_type in (Data_Type._8BIT_FLAGS, Data_Type._16BIT_FLAGS, Data_Type._32BIT_FLAGS):
            bit_size: int = Data_Type.getSize(entry.data_type)
            total_registers = max(1, bit_size // 16)
            if total_registers > 1:
                # use _register_words_to_bytes so word order and byte order
                # within each word are both handled correctly for little-endian
                # entries (e.g. _32BIT_FLAGS with Register_Endian:little).
                # Previously this always assembled words in big-endian order.
                flag_bytes = self._register_words_to_bytes(registry, entry.register, total_registers, byte_order)
                if flag_bytes is None:
                    return None
                val = int.from_bytes(flag_bytes, byteorder="big", signed=False)
            else:
                # Single register: apply byte swap for little-endian if needed
                val = registry[entry.register] & 0xFFFF
                if byte_order == "little":
                    val = self._swap_bytes_16(val)

            start_bit: int = entry.register_bit if entry.register_bit >= 0 else 0
            end_bit: int = start_bit + bit_size

            code_dict = self.get_code_dict(entry.documented_name + "_codes")

            flags = []
            for i in range(start_bit, end_bit):
                if (val >> i) & 1:
                    if code_dict:
                        flag_index = "b" + str(i)
                        if flag_index in code_dict:
                            flags.append(code_dict[flag_index])
                    else:
                        flags.append("1")
                elif not code_dict:
                    flags.append("0")

            value = ",".join(flags) if code_dict else "".join(flags)

        elif entry.data_type.value > 400:  # signed-magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            # FIX: swap bytes before bit extraction for little-endian entries
            register_int = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                register_int = self._swap_bytes_16(register_int)
            sign_bit = (register_int >> bit_index) & 1
            magnitude = (register_int >> (bit_index + 1)) & ((1 << (bit_size - 1)) - 1)
            value = -magnitude if sign_bit else magnitude

        elif entry.data_type.value > 300:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            # FIX: swap bytes before bit extraction for little-endian entries
            register_int = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                register_int = self._swap_bytes_16(register_int)
            raw_bits = (register_int >> bit_index) & ((1 << bit_size) - 1)
            sign_mask = 1 << (bit_size - 1)
            if raw_bits & sign_mask:
                value = raw_bits - (1 << bit_size)
            else:
                value = raw_bits

        elif entry.data_type.value > 200:  # unsigned bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            # FIX: swap bytes before bit extraction for little-endian entries
            register_int = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                register_int = self._swap_bytes_16(register_int)
            value = (register_int >> bit_index) & ((1 << bit_size) - 1)

        elif entry.data_type == Data_Type.BYTE:
            # Extract the low byte of the register.
            # For little-endian entries swap first so "low byte" is the correct
            # physical byte per the hardware spec.
            raw = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                raw = self._swap_bytes_16(raw)
            value = raw & 0xFF

        elif entry.data_type == Data_Type.HEX:
            raw = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                raw = self._swap_bytes_16(raw)
            value = raw.to_bytes(2, byteorder="big").hex()

        elif entry.data_type == Data_Type.ASCII:
            raw_bytes = self._register_words_to_bytes(registry, entry.register, 1, byte_order)
            if raw_bytes is None:
                return None
            value = self._decode_text_bytes(raw_bytes)

        elif entry.data_type in (Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32):
            word_count = self.entry_word_count(entry)
            raw_bytes = self._register_words_to_bytes(registry, entry.register, word_count, byte_order)
            if raw_bytes is None:
                return None
            if entry.data_type_size > 0:
                raw_bytes = raw_bytes[:entry.data_type_size]
            value = self._decode_text_bytes(raw_bytes)

        else:
            # USHORT fallback
            # FIX: byte_order was ignored in the original fallback branch.
            # Swap bytes for little-endian entries before returning.
            raw = registry[entry.register] & 0xFFFF
            if byte_order == "little":
                raw = self._swap_bytes_16(raw)
            value = raw

        value = self.apply_adjustments(value, entry, "post_decode")

        # Collapse whole floats to int (e.g. 52.0 → 52) after scaling
        if isinstance(value, float) and value.is_integer():
            value = int(value)

        return value

    def process_registery(
        self,
        registry: Mapping[int, int | bytes | tuple[bytes, float]],
        registry_map: list[registry_map_entry],
    ) -> dict[str, int | float | str]:
        """Process registry into appropriately typed and named values."""

        concatenate_registry: dict[int, int | float | str] = {}
        info: dict[str, int | float | str] = {}

        for entry in registry_map:
            if entry.description_source:
                continue
            if entry.register not in registry:
                continue

            raw = registry[entry.register]

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
                        concatenated_value = concatenated_value.replace("\x00", " ").strip()

                    info[entry.variable_name] = concatenated_value
            else:
                info[entry.variable_name] = value

        for entry in registry_map:
            if entry.variable_name in info:
                info[entry.variable_name] = self.apply_adjustments(info[entry.variable_name], entry, "context", info)

        entries_by_name = {entry.variable_name: entry for entry in registry_map}
        for entry in registry_map:
            if not entry.description_source:
                continue
            source_name = entry.description_source
            if source_name not in info:
                continue
            source_entry = entries_by_name.get(source_name)
            if source_entry is None:
                continue
            description = self._code_description_for_value(source_entry, info[source_name])
            if description is not None:
                info[entry.variable_name] = description

        return info

    def validate_registry_entry(self, entry: registry_map_entry, val: str | int | float) -> int:
        """
        Validate one registry value against the entry's configured constraints.
        Returns 1 when valid, 0 otherwise.
        """
        code_dict: dict[str, str] = self.get_entry_code_dict(entry)
        if code_dict:
            lookup_keys = [str(val)]
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

    def safe_eval_expression(self, expression: str) -> int | float:
        """
        Safely evaluate arithmetic expressions using AST parsing.
        Supports: + - * / // % **
        Does NOT allow function calls, attribute access, imports, variables,
        comprehensions, lambdas, or arbitrary execution.
        """

        def _safe_eval(node: ast.AST) -> int | float:
            if isinstance(node, ast.Expression):
                return _safe_eval(node.body)

            elif isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                msg = f"Unsupported constant: {type(node.value)}"
                raise ValueError(msg)

            elif isinstance(node, ast.BinOp):
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

            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return -_safe_eval(node.operand)

            msg = f"Unsupported AST node: {type(node).__name__}"
            raise TypeError(msg)

        expression = re.sub(r"\s+", "", expression)
        tree: ast.Expression = ast.parse(expression, mode="eval")
        result: int | float = _safe_eval(tree)

        if isinstance(result, float) and result.is_integer():
            return int(result)

        return result

    def evaluate_expressions(self, expression: str, variables: dict[str, str | float | int]) -> list[str]:
        """
        Resolve dynamic register expressions and return a list of evaluated strings.
        Supports variable substitution ([name]), range expansion ([x~y]), and
        arithmetic evaluation ([expr]).
        """

        def evaluate_variables(expr: str) -> str:
            var_pattern = re.compile(r"\[([^\[\]]+)\]")

            def replace_vars(match: re.Match[str]) -> str:
                var_name = match.group(1)
                if var_name in variables:
                    return str(variables[var_name])
                return match.group(0)

            return var_pattern.sub(replace_vars, expr)

        def evaluate_ranges(expr: str) -> list[str]:
            range_pattern: re.Pattern[str] = re.compile(r"\[.*?(?P<start>\d+)\s*~\s*(?P<end>\d+).*?\]")
            match: re.Match[str] | None = range_pattern.search(expr)

            if not match:
                return [expr]

            range_start = int(match.group("start"))
            range_end = int(match.group("end"))

            if range_start > range_end:
                range_start, range_end = range_end, range_start

            results: list[str] = []
            for i in range(range_start, range_end + 1):
                replaced: str = expr[:match.start()] + str(i) + expr[match.end():]
                results.extend(evaluate_ranges(replaced))

            return results

        def evaluate_math(expr: str) -> str:
            math_pattern: re.Pattern[str] = re.compile(r"\[(?P<maths>[0-9\+\-\*\/%\(\)\.\s]+)\]")

            def replace_maths(match: re.Match[str]) -> str:
                try:
                    maths: str | Any = match.group("maths")
                    result: int | float = self.safe_eval_expression(maths)
                    return str(result)
                except Exception:
                    return match.group(0)

            return math_pattern.sub(replace_maths, expr)

        substituted = evaluate_variables(expression)
        expanded: list[str] = evaluate_ranges(substituted)
        return [evaluate_math(r) for r in expanded]

    def apply_range_formula(self, raw_value: float, logic: str) -> float:
        """Apply conditional range formulas based on raw_value."""
        cleaned_logic: str = logic.replace(" ", "")

        pattern = r"x\?\s*[\(\[]\s*([\-0-9.]+)\s*,\s*([\-0-9.]+)\s*[\)\]]\s*->\s*(.+?)(?=x\?|$)"
        logic_blocks = re.findall(pattern, cleaned_logic)

        for lower, upper, formula in logic_blocks:
            lower_f = float(lower)
            upper_f = float(upper)

            if lower_f < raw_value <= upper_f:
                expression = formula.replace("x", str(raw_value))
                try:
                    evaluated_expression: int | float = self.safe_eval_expression(expression)
                    self._log.debug(f"Successful range formula evaluation {evaluated_expression} for value {raw_value}")
                    return float(evaluated_expression)
                except Exception as e:
                    self._log.warning(f"Failed range formula evaluation {expression} for value {raw_value}: {e}")
                    return raw_value

        return raw_value

    def resolve_dynamic_registry_entries(self, live_values: dict[str, int | float | str]) -> None:
        """
        Resolve deferred dynamic register expressions using already-discovered
        live register values. Only resolves entries whose dependency variables
        are present in live_values.
        """
        if not self.dynamic_registry_rows:
            return

        resolved_count = 0

        for row in self.dynamic_registry_rows.copy():
            try:
                resolved_registers = self.evaluate_expressions(row["register"], live_values)

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
        """Reset the next_read_timestamp values for all registry entries."""
        for registry_type in Registry_Type:
            if registry_type in self.registry_map:
                for entry in self.registry_map[registry_type]:
                    entry.next_read_timestamp = 0.0
        self._log.debug(f"Reset timestamps for all registry entries in protocol {self.protocol}")
