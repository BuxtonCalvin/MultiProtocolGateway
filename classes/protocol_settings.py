import ast
import csv
import itertools
import json
import logging
import os
import re
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
    _16BIT_FLAGS = 7
    _8BIT_FLAGS = 8
    _32BIT_FLAGS = 9


    ASCII = 84
    ''' 2 characters '''
    HEX = 85
    ''' HEXADECIMAL STRING '''

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
    #signed bits
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

    #signed magnitude  bits
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
    def fromString(cls, name : str) -> "Data_Type":
        name = name.strip().upper()
        if name[0].isdigit():
            name = "_"+name

        #common alternative names
        alias : dict[str,str] = {
            "UINT8" : "BYTE",
            "INT16" : "SHORT",
            "UINT16" : "USHORT",
            "UINT32" : "UINT",
            "INT32" : "INT"
        }

        if name in alias:
            name = alias[name]

        return getattr(cls, name)

    @classmethod
    def getSize(cls, data_type : "Data_Type") -> int:
        sizes = {
                    Data_Type.BYTE : 8,
                    Data_Type.USHORT : 16,
                    Data_Type.UINT : 32,
                    Data_Type.SHORT : 16,
                    Data_Type.INT : 32,
                    Data_Type._8BIT_FLAGS : 8,
                    Data_Type._16BIT_FLAGS : 16,
                    Data_Type._32BIT_FLAGS : 32
                 }

        if data_type in sizes:
            return sizes[data_type]

        if data_type.value > 400:  #signed magnitude bits
            return data_type.value-400

        if data_type.value > 300:  #signed bits
            return data_type.value-300

        if data_type.value > 200: #unsigned bits
            return data_type.value-200

        return -1 #should never happen

class WriteMode(Enum):
    READ = 0x00
    ''' READ ONLY '''
    READDISABLED = 0x01
    ''' DO NOT READ OR WRITE'''
    WRITE = 0x02
    ''' READ AND WRITE '''

    #todo, write only
    WRITEONLY = 0x03
    ''' WRITE ONLY'''

    @classmethod
    def fromString(cls, name: str) -> "WriteMode":
        name = name.strip().upper()

        # Map the input strings to the ACTUAL name of the Enum member
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

        # Get the member name from alias, defaulting to "READ"
        member_name = alias.get(name, "READ")

        # Return the actual Enum member using bracket notation
        return cls[member_name]

class Registry_Type(Enum):
    ZERO = 0x00
    ''' for protocols that don't have a command / registry type '''

    HOLDING = 0x03
    INPUT = 0x04

@dataclass
class registry_map_entry:
    registry_type : Registry_Type
    register : int
    register_bit : int
    register_bit_end: int
    register_byte : int
    ''' byte offset for canbus etc... '''

    variable_name : str
    documented_name : str
    note : str
    unit : str
    unit_mod : float
    concatenate : bool
    concatenate_registers : list[int]

    values : list
    value_regex : str = ""

    value_min : int = 0
    ''' min of value range for protocol analyzing'''
    value_max : int = 65535
    ''' max of value range for protocol analyzing'''

    ''' if value needs to be concatenated with other registers'''
    data_type : Data_Type = Data_Type.USHORT
    data_type_size : int = -1
    ''' for non-fixed size types like ASCII'''

    data_byteorder: Literal['little', 'big', ''] = ''
    ''' entry specific byte order little | big | '' '''

    read_command: bytes | None = None
    ''' for transports/protocols that require sending a command on top of "register" '''

    read_interval : float = 1000
    ''' how often to read register in ms'''

    next_read_timestamp : float = 0.0
    ''' unix timestamp in ms '''

    write_mode : WriteMode = WriteMode.READ
    ''' enable disable reading/writing '''

    has_enum_mapping : bool = False
    ''' indicates if this field has enum mappings that should be treated as strings '''

    hardware_offset : int = 0
    '''
    A fixed integer added to the decoded value after unit_mod scaling.
    Captures firmware bias-encoding conventions such as the EG4-LL temperature
    threshold registers, which store (actual_degC + 50) as an unsigned byte so
    that sub-zero thresholds remain positive in the raw register.
    Set via "offset:<N>" in the CSV values field, e.g. "offset:-50".
    Applied as: final_value = (raw * unit_mod) + hardware_offset
    '''

    def __str__(self):
        return self.variable_name

    def __eq__(self, other):
        return (    isinstance(other, registry_map_entry)
                    and self.register == other.register
                    and self.register_bit == other.register_bit
                    and self.register_bit_end == other.register_bit_end
                    and self.registry_type == other.registry_type
                    and self.register_byte == other.register_byte)

    def __hash__(self):
        # Hash based on tuple of object attributes
        return hash((self.variable_name, self.register_bit, self.register_byte, self.registry_type))


class protocol_settings:

    # only the JSON without loading registry CSVs
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

        settings = {k: v for k, v in data.items() if not k.endswith("_codes")}
        if "transport" in settings:
            return str(settings["transport"])
        if "reader" in settings:
            return str(settings["reader"])
        return "modbus_rtu"

    def __init__(self, protocol : str, transport_settings : Optional[TransportSettings] = None, settings_dir : str = "protocols") -> None:

        self.byteorder : Literal['little', 'big'] = "big"

        #apply log level to logger
        self._log_level = getattr(logging, logging.getLevelName(logging.getLogger().getEffectiveLevel()), logging.INFO)
        self._log : logging.Logger = logging.getLogger(__name__)
        self._log.setLevel(self._log_level)

        self.protocol: str = protocol
        self.transport: str = ""   # safe default — overwritten by load__json if present
        self.registry_map: dict[Registry_Type, list[registry_map_entry]] = {}
        self.registry_map_size: dict[Registry_Type, int] = {}
        self.registry_map_ranges: dict[Registry_Type, list[tuple[int, int]]] = {}
        self.codes: dict[str, str | dict[str, str]] = {}
        self.settings: dict[str, str] = {}
        self.variable_mask: list[str] = []
        self.variable_screen: list[str] = []
        self.settings_dir = settings_dir
        self.transport_settings: Optional[TransportSettings] = transport_settings

        raw_device: str | None = self.transport_settings.name if self.transport_settings else None

        if raw_device is not None and raw_device.startswith("transport."):
            device_name: str = raw_device.removeprefix("transport.")
        else:
            device_name: str = "unknown_device"
        # Default to standard file names using device name as suffix.
        mask_file: str = "variable_mask_" + device_name  + ".txt"
        screen_file: str = "variable_screen_" + device_name  + ".txt"

        if transport_settings is not None:
            mask_file = transport_settings.get("variable_mask", fallback=mask_file)
            screen_file = transport_settings.get("variable_screen", fallback=screen_file)

        # variable_mask:   if set, only metrics in this file are kept (allowlist)
        # variable_screen: if set, metrics in this file are removed (denylist)
        # if both are set, mask is applied first, then screen
        # files are searched relative to the protocol folder

        # Load filters using resolved filenames
        self.variable_mask = self._load_filter_file(mask_file)
        self.variable_screen = self._load_filter_file(screen_file)

        self.load__json() #load first, so priority to json codes

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
            self._log.debug( f"Filter file '{filename}' not found for protocol '{self.protocol}' —skipping." )
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

    def get_registry_map(self, registry_type : Registry_Type = Registry_Type.ZERO) -> list[registry_map_entry]:
        return self.registry_map[registry_type]

    def get_registry_ranges(self, registry_type : Registry_Type)  -> list[tuple[int, int]]:
        return self.registry_map_ranges[registry_type]


    def get_holding_registry_entry(self, name : str) -> registry_map_entry | None:
        ''' deprecated '''
        return self.get_registry_entry(name, registry_type=Registry_Type.HOLDING)

    def get_input_registry_entry(self, name : str) -> registry_map_entry | None:
        ''' deprecated '''
        return self.get_registry_entry(name, registry_type=Registry_Type.INPUT)

    def get_registry_entry( self, name: str, registry_type: Registry_Type ) -> Optional[registry_map_entry]:

        name = name.strip().lower().replace(" ", "_") #clean name
        for item in self.registry_map[registry_type]:
            if item.documented_name == name:
                return item

        return None

    def get_code_by_value(self, entry: registry_map_entry, value: str, fallback: str) -> str:
        if value is not None and entry is not None:
            value = value.strip().lower()
            for code, val in self.get_code_dict(entry.variable_name + "_codes").items():
                if value == val.lower():
                    return code
            return fallback

    def get_code_dict(self, key: str) -> dict[str, str]:
        """
        Returns the code mapping for a given key, or an empty dict if
        the key doesn't exist or is not a dict (i.e. is a plain setting).
        Used internally by process_register_* and validate_registry_entry.
        """
        value: str | dict[str, str] | None = self.codes.get(key)
        if isinstance(value, dict):
            return value
        return {}

    def load__json(self, file : str = "", settings_dir : str = "") -> None:
        if not settings_dir:
            settings_dir = self.settings_dir

        if not file:
            file = self.protocol + ".json"

        path: str | None = self.find_protocol_file(file, settings_dir)

        #if path does not exist; nothing to load. skip.
        if path is None:
            self._log.error(f"ERROR: '{file}' not found")
            return

        with open(path) as f:
            self.codes = json.loads(f.read())

        self.settings = {}

        # Iterate over the keys and add entries not ending with "_codes" to self.settings
        for key, value in self.codes.items():
            if not key.endswith("_codes") and isinstance(value, str):
                self.settings[key] = value

    def load_registry_overrides(self, override_path: str, keys : list[str]) -> dict[str, dict[str, Any]]:
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


    def load__registry(self, path: str, registry_type : Registry_Type = Registry_Type.INPUT) -> list[registry_map_entry]:
        registry_map : list[registry_map_entry] = []

        # register_regex = re.compile(r"(?P<register>(?:0?x[\da-z]+|[\d]+))\.(b(?P<bit>x?\d{1,2})|(?P<byte>x?\d{1,2}))")
        register_regex = re.compile(
            r"(?P<register>\d{1,5}|0x[0-9A-Fa-f]{1,4})"
            r"(?:\.b(?P<bit_start>\d{1,2})(?:-(?P<bit_end>\d{1,2}))?)?"
            r"(?:\.(?P<byte>\d{1,2}))?"
        )


        """The register field in the CSV can be written as:
            40001.b3 — register 40001, bit 3 → bit group matches 3, byte group is None
            40001.2 — register 40001, byte offset 2 → byte group matches 2, bit group is None
            40001 — plain register with no bit/byte qualifier → neither group matches
        """

        read_interval_regex = re.compile(r"(?P<value>[\.\d]+)(?P<unit>[xs]|ms)")


        data_type_regex = re.compile(r"(?P<datatype>\w+)\.(?P<length>\d+)")

        range_regex = re.compile(r"(?P<reverse>r|)(?P<start>(?:0?x[\da-z]+|[\d]+))[\-~](?P<end>(?:0?x[\da-z]+|[\d]+))")
        ascii_value_regex = re.compile(r"(?P<regex>^\[.+\]$)")
        list_regex = re.compile(r"\s*(?:(?P<range_start>(?:0?x[\da-z]+|[\d]+))-(?P<range_end>(?:0?x[\da-z]+|[\d]+))|(?P<element>[^,\s][^,]*?))\s*(?:,|$)")


        #load read_interval from transport settings, for #x per register read intervals
        transport_read_interval : int = 1000
        if self.transport_settings is not None:
            transport_read_interval = self.transport_settings.getint("read_interval", transport_read_interval)


        if not os.path.exists(path): #return empty if file doesn't exist.
            return registry_map


        overrides : dict[str, dict] | None = None
        override_keys: list[str] = ["documented name", "register"]
        overrided_keys = set()
        ''' list / set of keys that were used for overriding. to track unique entries'''

        #assuming path ends with .csv
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
            # Initialize variables to hold numeric and character parts
            unit_multiplier : float = 1
            unit_symbol : str = ""
            read_interval : int = 0
            ''' read interval in ms '''

             #clean up doc name, for extra parsing
            if row["variable name"].startswith("#"):  # filter out comments
                return

            row["documented name"] = row["documented name"].strip().lower().replace(" ", "_")

            #region read_interval

            if "read interval" in row:
                row["read interval"] = row["read interval"].lower() #ensure is all lower case
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


            #region overrides
            if overrides is not None:
                #apply overrides using documented name or register
                override_row = None
                # Check each key in order until a match is found
                for key in override_keys:
                    key_value = row.get(key)
                    if key_value and key_value in overrides[key]:
                        override_row = overrides[key][key_value]
                        overrided_keys.add(key_value)
                        break

                # Apply non-empty override values if an override row is found
                if override_row:
                    for field, override_value in override_row.items():
                        if override_value:  # Only replace if override value is non-empty
                            row[field] = override_value

            #endregion overrides

            #region unit

            #if or is in the unit; ignore unit
            if "or" in row["unit"].lower() or ":" in row["unit"].lower():
                unit_multiplier = 1
                unit_symbol = row["unit"]
            else:
                # Use regular expressions to extract numeric and character parts
                unit_matches: list[tuple[str, str]] = re.findall(r"(\-?[0-9.]+)|(.*?)$", row["unit"])

                # Iterate over the matches and assign them to appropriate variables
                for unit_match in unit_matches:
                    if unit_match[0]:  # If it matches a numeric part
                        unit_multiplier = float(unit_match[0])
                    elif unit_match[1]:  # If it matches a character part
                        unit_symbol = unit_match[1].strip()

            #convert to float
            try:
                unit_multiplier = float(unit_multiplier)
            except Exception:
                unit_multiplier = float(1)

            if unit_multiplier == 0:
                unit_multiplier = float(1)

            #endregion unit


            variable_name = row["variable name"] if row["variable name"] else row["documented name"]
            variable_name = variable_name.strip().lower().replace(" ", "_").replace("__", "_") #clean name

            if re.search(r"[^a-zA-Z0-9\_]", variable_name) :
                self._log.warning("Invalid Name : " + str(variable_name) + " reg: " + str(row["register"]) + " doc name: " + str(row["documented name"]) + " path: " + str(path))


            if not variable_name and not row["documented name"]: #skip empty entry / no name. todo add more invalidator checks.
                return

            #region data type
            data_type = Data_Type.USHORT
            data_type_len : int = -1
            data_byteorder: Literal['little', 'big', ''] = ''
            #optional row, only needed for non-default data types
            if "data type" in row and row["data type"]:
                data_type_str : str = ''

                matches = data_type_regex.search(row["data type"])
                if matches:
                    data_type_len = int(matches.group("length"))
                    data_type_str = matches.group("datatype")
                else:
                    data_type_str = row["data type"]

                #check if datatype specifies byteorder
                if data_type_str.upper().endswith("_LE"):
                    data_byteorder = "little"
                    data_type_str = data_type_str[:-3]
                elif data_type_str.upper().endswith("_BE"):
                    data_byteorder = "big"
                    data_type_str = data_type_str[:-3]


                data_type = Data_Type.fromString(data_type_str)

            # Some legacy registry maps duplicate the bit index in the "unit"
            # column for 1bit / nbit rows, e.g.:
            #   21.b11,...,11,1bit,...
            # That numeric token is not a real engineering scale factor and
            # must not become unit_mod, or values get multiplied by 11, 12, etc.
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

            #endregion data type

            #region values
            #get value range for protocol analyzer
            values : list = []
            value_min : int = 0
            value_max : int = 65535 #default - max value for ushort
            value_regex : str = ""
            value_is_json : bool = False

            #test if value is json.
            if "{" in row["values"]: #to try and stop non-json values from parsing. the json parser is buggy for validation
                try:
                    codes_json = json.loads(row["values"])
                    value_is_json = True

                    name = row["documented name"]+"_codes"
                    if name not in self.codes:
                        self.codes[name] = codes_json

                except ValueError:
                    value_is_json = False

            # Extract hardware_offset from values field before other parsing.
            # Syntax: "offset:<N>" where N is a signed integer, e.g. "offset:-50".
            # Captures firmware bias-encoding where the BMS adds a fixed constant
            # before storing a value (e.g. EG4-LL stores temp_degC + 50 so that
            # sub-zero thresholds remain non-negative in the raw unsigned register).
            # The token is stripped from the values string before range/list parsing
            # so it does not interfere with normal value_min/value_max extraction.
            hardware_offset: int = 0
            offset_match: re.Match[str] | None = re.search(r"offset:\s*(?P<offset>-?\d+)", row["values"], re.IGNORECASE)
            if offset_match:
                hardware_offset = int(offset_match.group("offset"))
                # Remove the token (and any surrounding comma/whitespace) so the
                # remainder of the values string parses normally.
                row["values"] = re.sub(r",?\s*offset:\s*-?\d+\s*,?", ",", row["values"], flags=re.IGNORECASE).strip(",").strip()

            if not value_is_json:
                if "," in row["values"]:
                    list_matches: Iterator[re.Match[str]] = list_regex.finditer(row["values"])

                    for list_match in list_matches:
                        # groupdict() returns dict[str, Any] — named groups that didn't
                        # participate return None, so extract with .get() to make
                        # None explicit and allow truthiness narrowing below
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

                    if data_type == Data_Type.ASCII: #might need to apply too hex values as well? or min-max works for hex?
                        #value_regex
                        val_match: re.Match[str] | None = ascii_value_regex.search(row["values"])
                        if val_match:
                            value_regex = val_match.group("regex")
                            unit_matched = True

                    if not unit_matched: #single value
                        values.append(row["values"])
            #endregion values

            #region register
            concatenate : bool = False
            concatenate_registers : list[int] = []

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
                    return   # skip this row — register address is un-parseable

                #print("register: " + str(register) + " bit : " + str(register_bit))
            else:
                range_match = range_regex.search(row["register"])
                if not range_match:
                    # Check if it's a dynamic expression
                    if "[" in row["register"]:
                        ### TODO resolved = self.evaluate_expressions(row["register"], live_register_values)
                        # use resolved addresses...
                        pass
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
                            for i in range(end, start-1, -1):
                                concatenate_registers.append(i)
                        else:
                            for i in range(start, end+1):
                                concatenate_registers.append(i)

            if concatenate_registers:
                r = range(len(concatenate_registers))
            else:
                r = range(1)

            #endregion register

            read_command = None
            if "read command" in row and row["read command"]:
                if row["read command"][0] == "x":
                    read_command: bytes | None = bytes.fromhex(row["read command"][1:])
                else:
                    read_command = row["read command"].encode("utf-8")

            writeMode : WriteMode = WriteMode.READ
            if "writable" in row:
                writeMode = WriteMode.fromString(row["writable"])

            if "write" in row:
                writeMode = WriteMode.fromString(row["write"])

            for i in r:
                item = registry_map_entry(
                                            registry_type = registry_type,
                                            register = register,
                                            register_bit = register_bit,
                                            register_bit_end = register_bit_end,
                                            register_byte = register_byte,
                                            variable_name = variable_name,
                                            documented_name = row["documented name"],
                                            unit = str(unit_symbol),
                                            unit_mod = unit_multiplier,
                                            data_type = data_type,
                                            data_type_size = data_type_len,
                                            data_byteorder = data_byteorder,
                                            note = note,
                                            concatenate = concatenate,
                                            concatenate_registers = concatenate_registers,
                                            values=values,
                                            value_min = value_min,
                                            value_max = value_max,
                                            value_regex = value_regex,
                                            read_command = read_command,
                                            read_interval = read_interval,
                                            write_mode = writeMode,
                                            has_enum_mapping = value_is_json,
                                            hardware_offset = hardware_offset,
                                        )
                registry_map.append(item)

                register = register + 1


        with open(path, newline="", encoding="latin-1") as csvfile:

            #clean column names before passing to csv dict reader

            delimeter = ";"
            first_row = next(csvfile).lower().replace("_", " ")
            if first_row.count(";") < first_row.count(","):
                delimeter = ","

            first_row = re.sub(r"\s+" + re.escape(delimeter) +"|" + re.escape(delimeter) +r"\s+", delimeter, first_row) #trim values

            csvfile = itertools.chain([first_row], csvfile) #add clean header to beginning of iterator

            # Create a CSV reader object
            reader = csv.DictReader(csvfile, delimiter=delimeter)

            # Iterate over each row in the CSV file
            for row in reader:
                process_row(row)

            if overrides is not None:
                # Add any unmatched overrides as new entries... probably need to add some better error handling to ensure entry isn't empty ect...
                for key in override_keys:
                    applied = False
                    for key_value, override_row in overrides[key].items():
                        # Check if both keys are unique before applying
                        if all(override_row.get(k) for k in override_keys):
                            if all(override_row.get(k) not in overrided_keys for k in override_keys):
                                self._log.info("Loading unique entry from overrides for both unique keys")
                                process_row(override_row)

                                # Mark both keys as applied
                                for k in override_keys:
                                    overrided_keys.add(override_row.get(k))

                                applied = True
                                break  # Exit inner loop after applying unique entry

                    if applied:
                        continue

            for index in reversed(range(len(registry_map))):
                item = registry_map[index]
                if index > 0:
                    #if high/low, its a double
                    if (
                        item.documented_name.endswith("_l")
                        and registry_map[index-1].documented_name.replace("_h", "_l") == item.documented_name
                        ):
                        combined_item = registry_map[index-1]

                        if not combined_item.data_type or combined_item.data_type  == Data_Type.USHORT:
                            if registry_map[index].data_type != Data_Type.USHORT:
                                combined_item.data_type = registry_map[index].data_type
                            else:
                                combined_item.data_type = Data_Type.UINT


                        if combined_item.documented_name == combined_item.variable_name:
                            combined_item.variable_name = combined_item.variable_name[:-2].strip()

                        combined_item.documented_name = combined_item.documented_name[:-2].strip()

                        if not combined_item.unit: #fix inconsistent documentation
                            combined_item.unit = registry_map[index].unit
                            combined_item.unit_mod = registry_map[index].unit_mod

                        del registry_map[index]

            #apply mask
            if self.variable_mask:
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() not in self.variable_mask
                        and item.variable_name.strip().lower() not in self.variable_mask
                        ):
                        del registry_map[index]

            # apply variable screen
            # logic also changes from and to or — an entry should be screened out if either its documented name or variable name
            # appears in the screen list, not only when both do.
            if self.variable_screen:
                for index in reversed(range(len(registry_map))):
                    item = registry_map[index]
                    if (
                        item.documented_name.strip().lower() in self.variable_screen
                        or item.variable_name.strip().lower() in self.variable_screen
                    ):
                        del registry_map[index]

            return registry_map

    def calculate_registry_ranges(self, registry_map : list[registry_map_entry], max_register : int, init : bool = False, timestamp: float = 0.0) -> list[tuple[int, int]]:

        ''' read optimization; calculate which ranges to read
        to allow the Modbus driver to combine multiple registers into a single Modbus read operation
        (function 0x03/0x04), reducing round-trips.

        1 Sort registry entries by starting register address
        2 Walk through them sequentially
        3 If the next register starts exactly where the previous one ends → merge
        4 If there is a gap → start a new range
        5 Produce a minimal set of (start_register, total_length) ranges'''

        max_batch_size = 45 #see manual; says max batch is 45

        if "batch_size" in self.settings:
            try:
                max_batch_size = int(self.settings["batch_size"])
            except ValueError:
                pass

        # Debug: log the parameters
        self._log.debug(f"calculate_registry_ranges: max_register={max_register}, max_batch_size={max_batch_size}, map_size={len(registry_map)}, init={init}")

        start: int = -max_batch_size
        ranges: list[tuple[int, int]] = []

        if timestamp > 0:
            timestamp_ms: float = timestamp*1000
        else:
            timestamp_ms = float(time.time() * 1000)

        while (start := start+max_batch_size) <= max_register:

            registers : list[int] = [] #use a list, im too lazy to write logic

            end: int = start+max_batch_size
            for register in registry_map:
                if register.register >= start and register.register < end:
                    if register.write_mode == WriteMode.READDISABLED: ##register is disabled; skip
                        continue
                    if register.write_mode == WriteMode.WRITEONLY: ##Write Only; skip
                        continue

                    #we are assuming calc registry ranges is being called EVERY READ.
                    if init: #add but do not update timestamp; can maybe rename init to no timestamp at this point
                        registers.append(register.register)
                    elif register.next_read_timestamp < timestamp_ms:
                        register.next_read_timestamp = timestamp_ms + register.read_interval
                        registers.append(register.register)

            if registers: #not empty
                ranges.append((min(registers), max(registers)-min(registers)+1)) ## APPENDING A TUPLE!

        return ranges

    @staticmethod
    def find_protocol_file(file: str, base_dir: str = "") -> Optional[str]:
        """
        Searches for a protocol file by name across known locations.
        Static so it can be called both from instance methods and from
        get_transport_type without needing a full instance.
        """
        base_path = Path(__file__).resolve().parent.parent / base_dir

        candidates: list[Path] = [
            base_path / file,
            base_path / file.split("_", 1)[0] / file,
        ]
        found: Path | None = next((p for p in candidates if p.exists()), None)
        if found:
            return str(found)
        # 4. Recursive search (last resort)
        try:
            # rglob returns Path objects; convert the first match to a string
            return str(next(base_path.rglob(file)))
        except StopIteration:
            return None


    def load_registry_map(self, registry_type : Registry_Type, file : str = "", settings_dir : str = ""):
        if not settings_dir:
            settings_dir = self.settings_dir

        if not file:
            if registry_type == Registry_Type.ZERO:
                file = self.protocol + ".registry_map.csv"
            else:
                file = self.protocol + "."+registry_type.name.lower()+"_registry_map.csv"

        path: str | None = self.find_protocol_file(file, settings_dir)

        #if path does not exist; nothing to load. skip.
        if not path:
            return

        self.registry_map[registry_type] = self.load__registry(path, registry_type)

        size : int = 0

        #get max register size
        for item in self.registry_map[registry_type]:
            if item.register > size:
                size = item.register

        self.registry_map_size[registry_type] = size
        self._log.debug(f"load_registry_map: {registry_type.name} - loaded {len(self.registry_map[registry_type])} entries, max_register={size}")
        self.registry_map_ranges[registry_type] = self.calculate_registry_ranges(self.registry_map[registry_type], self.registry_map_size[registry_type], init=True)

    # # None if no branch matched
    def process_register_bytes(self, registry: Mapping[int, bytes | tuple[bytes, float]], entry: registry_map_entry) -> int | float | str | None:
        ''' process bytes into data'''

        raw: bytes | tuple[bytes, float] = registry[entry.register]

        if isinstance(raw, tuple):
            register: bytes = raw[0]   # bytes — type checker narrows correctly now
        else:
            register = raw

        byte_order: Literal['little', 'big'] = self.byteorder
        if entry.data_byteorder: #allow map entry to override byteorder
            byte_order = entry.data_byteorder

        if entry.register_byte > 0:
            register = register[entry.register_byte:]

        if entry.data_type_size > 0:
            register = register[:entry.data_type_size]

        value: int | float | str = int.from_bytes(register[:2], byteorder=byte_order, signed=False)

        if entry.data_type == Data_Type.UINT:
            value = int.from_bytes(register[:4], byteorder=byte_order, signed=False)
        elif entry.data_type == Data_Type.INT:
            value = int.from_bytes(register[:4], byteorder=byte_order, signed=True)
        elif entry.data_type == Data_Type.USHORT:
            value = int.from_bytes(register[:2], byteorder=byte_order, signed=False)
        elif entry.data_type == Data_Type.SHORT:
            value = int.from_bytes(register[:2], byteorder=byte_order, signed=True)
        elif entry.data_type in (Data_Type._16BIT_FLAGS, Data_Type._8BIT_FLAGS, Data_Type._32BIT_FLAGS):
            val = int.from_bytes(register, byteorder=byte_order, signed=False)
            #16 bit flags
            start_bit : int = 0
            end_bit : int = 16 #default 16 bit
            flag_size : int = Data_Type.getSize(entry.data_type)

            if isinstance(value, (int, float)) and entry.register_bit >= 0: #handle custom bit offset
                start_bit = entry.register_bit

            #handle custom sizes, less than 1 register
            end_bit = flag_size + start_bit

            if entry.documented_name + "_codes" in self.codes:
                code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")
                flags: list[str] = []
                flag_indexes: list[str] = []

                if code_dict:
                    register_bytes: bytes = bytes(register)

                    for i in range(start_bit, end_bit):
                        byte: int = i // 8
                        bit: int = i % 8
                        val: int = register_bytes[byte]
                        if (val >> bit) & 1:
                            flag_index: str = "b" + str(i)
                            flag_indexes.append(flag_index)
                            if flag_index in code_dict:
                                flags.append(code_dict[flag_index])

                # check multibit flags
                multibit_flags: list[str] = [key for key in self.codes if "&" in key]

                if multibit_flags:
                    flag_indexes_set: set[str] = set(flag_indexes)
                    for multibit_flag in multibit_flags:
                        bits: list[str] = multibit_flag.split("&")
                        if all(bit in flag_indexes_set for bit in bits):
                            if multibit_flag in code_dict:          # guard before access
                                flags.append(code_dict[multibit_flag])  # dict[str, str]

                value = ",".join(flags)
            else:
                flags : list[str] = []
                for i in range(start_bit, end_bit):  # Iterate over each bit position (0 to 15)
                    # Check if the i-th bit is set
                    if (val >> i) & 1:
                        flags.append("1")
                    else:
                        flags.append("0")
                value = "".join(flags)


        elif entry.data_type.value > 400: #signed-magnitude bit types ( sign bit is the last bit instead of front )
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1  # Create a mask for extracting X bits
            bit_index = entry.register_bit

            # convert bytes to integer here for operations.
            register_int: int = int.from_bytes(register, byteorder=byte_order)

            # Check if the value is negative
            if (register_int >> bit_index) & 1:
                # If negative, extend the sign bit to fill out the value
                sign_extension: int = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> (bit_index + 1)) | sign_extension
            else:
                # If positive, simply extract the value using the bit mask
                value = (register_int >> bit_index) & bit_mask
        elif entry.data_type.value > 300: #signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_mask = (1 << bit_size) - 1  # Create a mask for extracting X bits
            bit_index = entry.register_bit

            register_int: int = int.from_bytes(register, byteorder=byte_order)

            # Check if the value is negative
            if (register_int >> (bit_index + bit_size - 1)) & 1:
                # If negative, extend the sign bit to fill out the value
                sign_extension = 0xFFFFFFFFFFFFFFFF << bit_size
                value = (register_int >> bit_index) | sign_extension
            else:
                # If positive, simply extract the value using the bit mask
                value = (register_int >> bit_index) & bit_mask

        elif entry.data_type == Data_Type.BYTE: #bit types
            value = int.from_bytes(register[:1], byteorder=byte_order, signed=False)

        elif entry.data_type.value > 200: #bit types
            bit_size: int = Data_Type.getSize(entry.data_type)
            bit_mask: int = (1 << bit_size) - 1  # Create a mask for extracting X bits
            bit_index: int = entry.register_bit

            if isinstance(register, bytes):
                register_int = int.from_bytes(register, byteorder=byte_order)
                value = (register_int >> bit_index) & bit_mask


        elif entry.data_type == Data_Type.HEX:
            register_bytes: bytes = bytes(register)
            value = register_bytes.hex() #convert bytes to hex

        elif entry.data_type == Data_Type.ASCII:
            try:
                register_bytes: bytes = bytes(register)
                value = register_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                self._log.error(f"UnicodeDecodeError decoding register "
                                f"{entry.register} for '{entry.variable_name}': {e}")
                value = ""    # safe fallback — empty string rather than leaving value unbound

        #apply unit mod
        if entry.unit_mod != float(1):
            value = int(value) * entry.unit_mod

        #apply codes
        if (entry.data_type != Data_Type._16BIT_FLAGS and entry.documented_name+"_codes" in self.codes):
            try:
                code_dict = self.get_code_dict(entry.documented_name + "_codes")
                if code_dict:
                    cleanval: str = str(int(value))
                    if cleanval in code_dict:
                        value = code_dict[cleanval]

            except Exception as e:
                self._log.debug( f"Exception '{e}' for conversion ")

                pass    # conversion failed — keep original value unchanged

        return value

    def _extract_bits(self, raw_value: int, entry: registry_map_entry) -> int:
        """
        Extract bits from raw_value using entry.register_bit and register_bit_end.
        Supports:
        b7        → single bit
        b4-7      → multi-bit range
        """
        start: int = entry.register_bit
        end: int = entry.register_bit_end

        # No bit spec → return original
        if start < 0:
            return raw_value

        width: int = end - start + 1
        mask: int = (1 << width) - 1

        return (raw_value >> start) & mask

    def _decoded_value_already_honors_bit_offset(self, entry: registry_map_entry) -> bool:
        """
        Some data-type decoders already consume register_bit/register_bit_end
        while decoding the raw Modbus register(s). Running _extract_bits() again
        would shift the already-decoded value a second time.

        Keep the generic post-decode bit slicing only for whole-word numeric
        types such as USHORT/SHORT/UINT/INT.
        """
        if entry.data_type in (
            Data_Type._8BIT,
            Data_Type._8BIT_FLAGS,
            Data_Type._16BIT_FLAGS,
            Data_Type._32BIT_FLAGS,
        ):
            return True

        return entry.data_type.value > 200

    def _register_words_to_bytes(
        self,
        registry: Mapping[int, int],
        start_register: int,
        word_count: int,
        byte_order: Literal['little', 'big'],
    ) -> bytes | None:
        words: list[int] = []
        for offset in range(word_count):
            register_num = start_register + offset
            if register_num not in registry:
                return None
            words.append(registry[register_num] & 0xFFFF)

        if byte_order == "little":
            words.reverse()

        return b"".join(word.to_bytes(2, byteorder="big", signed=False) for word in words)

    # None signals partial read — register+1 missing.  # None is legitimate here — caller must handle a None value.
    def process_register_ushort(self, registry: Mapping[int, int], entry: registry_map_entry) -> int | float | str | None:
        ''' process ushort type registry into data'''

        byte_order: Literal['little', 'big']  = self.byteorder
        if entry.data_byteorder:
            byte_order = entry.data_byteorder

        if entry.data_type == Data_Type.UINT: #read uint
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return
            value = int.from_bytes(register_bytes, byteorder="big", signed=False)
        elif entry.data_type == Data_Type.SHORT: #read signed short
            val = registry[entry.register]

            # Convert the combined unsigned value to a signed integer if necessary
            if val & (1 << 15):  # Check if the sign bit (bit 15) is set
                # Perform two's complement conversion to get the signed integer
                value = val - (1 << 16)
            else:
                value = val
            # As most inverters produce the correct value, the value needs to be flipped conditionally,
            # perhaps by a suffix/prefix in the data type in the CSV.
            #value = -value
        elif entry.data_type == Data_Type.INT: #read int
            register_bytes = self._register_words_to_bytes(registry, entry.register, 2, byte_order)
            if register_bytes is None:
                return
            value = int.from_bytes(register_bytes, byteorder="big", signed=True)

        elif entry.data_type == Data_Type._8BIT:
            val= registry[entry.register]

            # Use start_bit to decide if we want High Byte (8) or Low Byte (0)
            # Default to Low Byte (0) if not specified
            start_bit = entry.register_bit if entry.register_bit >= 0 else 0

            # Shift and mask to get 8 bits (0xFF = 255)
            # (25628 >> 0) & 255 = 28
            # (25628 >> 8) & 255 = 100
            value = (val >> start_bit) & 0xFF

        elif entry.data_type in [Data_Type._8BIT_FLAGS, Data_Type._16BIT_FLAGS, Data_Type._32BIT_FLAGS]:
            # Determine flag size in bits based on type
            bit_size: int = Data_Type.getSize(entry.data_type)
            total_registers = max(1, bit_size // 16)
            if total_registers > 1:
                needed_registers = [entry.register + offset for offset in range(total_registers)]
                if any(register_num not in registry for register_num in needed_registers):
                    return None
                val = 0
                for register_num in needed_registers:
                    val = (val << 16) | registry[register_num]
            else:
                val = registry[entry.register]

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
            register_int = registry[entry.register]
            sign_bit = (register_int >> bit_index) & 1
            magnitude = (register_int >> (bit_index + 1)) & ((1 << (bit_size - 1)) - 1)
            value = -magnitude if sign_bit else magnitude

        elif entry.data_type.value > 300:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            register_int = registry[entry.register]
            raw_bits = (register_int >> bit_index) & ((1 << bit_size) - 1)
            sign_mask = 1 << (bit_size - 1)
            if raw_bits & sign_mask:
                value = raw_bits - (1 << bit_size)
            else:
                value = raw_bits

        elif entry.data_type.value > 200:  # unsigned bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            register_int = registry[entry.register]
            value = (register_int >> bit_index) & ((1 << bit_size) - 1)

        elif entry.data_type == Data_Type.HEX:
                value = registry[entry.register].to_bytes((16 + 7) // 8, byteorder=byte_order) #convert to ushort to bytes
                value = value.hex() #convert bytes to hex
        elif entry.data_type == Data_Type.ASCII:
            # For ASCII data, use little-endian byte order to read characters in correct order
            value = registry[entry.register].to_bytes((16 + 7) // 8, byteorder="little") #convert to ushort to bytes
            try:
                value = value.decode("utf-8") #convert bytes to ascii
            except UnicodeDecodeError as e:
                self._log.error(f"UnicodeDecodeError decoding UShort register "
                                f"{entry.register} for '{entry.variable_name}': {e}")
                value = ""    # safe fallback — empty string rather than leaving value unbound

        else:  # default, Data_Type.USHORT
            value = registry[entry.register]  # keep as int

        # Apply scaling only to numeric values
        if isinstance(value, (int, float)) and entry.unit_mod != 1:
            value = value * entry.unit_mod

            # normalize float → int when appropriate
            if isinstance(value, float) and value.is_integer():
                value = int(value)

        # Apply hardware offset (bias-encoding correction) after scaling.
        # e.g. EG4-LL config temp registers store (actual_degC + 50);
        # hardware_offset = -50 corrects this back to actual_degC.
        if isinstance(value, (int, float)) and entry.hardware_offset != 0:
            value = value + entry.hardware_offset

        # Normalize numeric type AFTER scaling
        if isinstance(value, float) and value.is_integer():
            value = int(value)

        #move this to transport level
        #if  isinstance(value, float) and self.max_precision > -1:
        #   value = round(value, self.max_precision)

        if (entry.data_type != Data_Type._16BIT_FLAGS and entry.documented_name+"_codes" in self.codes):
            try:
                code_dict = self.get_code_dict(entry.documented_name + "_codes")
                if code_dict:
                    cleanval = str(int(value))

                    if cleanval in code_dict:
                        value = code_dict[cleanval]
            except Exception:
                #do nothing; try is for intval
                value = value

        return value

    # rename from map to registry_map — as it shadows builtin
    # This is where None should be caught and excluded before the dict is built.
    def process_registery(self, registry: Mapping[int, int | bytes | tuple[bytes, float]],
                      registry_map: list[registry_map_entry]) -> dict[str, int | float | str]:
        '''process registry into appropriate datatypes and names -- maybe add func for single entry later?'''

        concatenate_registry: dict[int, int | float | str] = {}
        info: dict[str, int | float | str] = {}

        for entry in registry_map:
            if entry.register not in registry:
                continue

            raw = registry[entry.register]   # int | bytes | tuple[bytes, float]

            if isinstance(raw, (bytes, tuple)):
                bytes_registry: Mapping[int, bytes | tuple[bytes, float]] = cast(Mapping[int, bytes | tuple[bytes, float]], registry)
                # Pass the registry — Mapping[int, bytes | tuple[bytes, float]]
                value: int | float | str | None = self.process_register_bytes(bytes_registry, entry)
            else:
                int_registry: Mapping[int, int] = cast(Mapping[int, int], registry)
                # Pass the registry — Mapping[int, int]
                value = self.process_register_ushort(int_registry, entry)

            # Filter None — partial reads are dropped before entering info dict
            if value is None:
                self._log.debug(f"Skipping '{entry.variable_name}' — partial read")
                continue

            # Add bit handling right after value is decoded, before it goes into info
            # force bit extraction if .bX is present
            if (
                isinstance(value, (int, float))
                and entry.register_bit >= 0
                and not self._decoded_value_already_honors_bit_offset(entry)
            ):
                value = self._extract_bits(int(value), entry)

            # # enable for verbose metric values
            # self._log.debug(
            #     f"{entry.variable_name}: raw={raw} extracted={value} "
            #     f"bits={entry.register_bit}-{entry.register_bit_end}"
            # )

            if entry.concatenate:
                concatenate_registry[entry.register] = value

                all_exist = True
                for key in entry.concatenate_registers:
                    if key not in concatenate_registry:
                        all_exist = False
                        break
                if all_exist:
                # if all(key in concatenate_registry for key in item.concatenate_registers):
                    concatenated_value = ""
                    for key in entry.concatenate_registers:
                        concatenated_value = concatenated_value + str(concatenate_registry[key])
                        del concatenate_registry[key]

                    #replace null characters with spaces and trim
                    if entry.data_type == Data_Type.ASCII:
                        concatenated_value = concatenated_value.replace("\x00", " ").strip()

                    info[entry.variable_name] = concatenated_value
            else:
                info[entry.variable_name] = value

        return info

    def validate_registry_entry(self, entry: registry_map_entry, val: str | int | float) -> int:
        """
        Validate one registry value against the entry's configured constraints.

        Returns:
            `1` when the value is considered valid, otherwise `0`.
        """
        code_dict: dict[str, str] = self.get_code_dict(entry.documented_name + "_codes")
        if code_dict:
            if val in code_dict:
                return 1
            return 0

        if entry.data_type == Data_Type.ASCII:
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


    def evaluate_expressions(self, expression: str, variables: dict[str, str | int]) -> list[str]:
        """
        Tries to resolve dynamic register address expressions containing variable
        references and range expansions.  Typically, battery data per a given register (battery x voltage etc.) is known
        only at startup and represents a register address where the actual location depends on how many battery cells
        and temperature sensors the device has — values that are only known after reading other related registers first.

        Args:
            expression: Register address expression from CSV, e.g.
                        "x4642.[1 + (([battery cells]*2) + ([temp sensors]*2))]"
            variables:  Dict of variable name -> current register value,
                        obtained from a first-pass register read.

        Returns:
            List of resolved register address strings ready for strtoint_safe.
        """

        def evaluate_variables(expr: str) -> str:
            """Substitute [variable name] references with their values."""
            var_pattern = re.compile(r"\[([^\[\]]+)\]")

            def replace_vars(match: re.Match[str]) -> str:
                var_name: str = match.group(1)
                if var_name in variables:
                    return str(variables[var_name])
                return match.group(0)   # leave unresolved references unchanged

            return var_pattern.sub(replace_vars, expr)

        def evaluate_ranges(expr: str) -> list[str]:
            """
            Recursively expand range expressions [start~end] into
            individual values. Handles multiple ranges via recursion.
            """
            range_pattern = re.compile(r"\[.*?(?P<start>\d+)\s*~\s*(?P<end>\d+).*?\]")
            match: re.Match[str] | None = range_pattern.search(expr)

            if not match:
                return [expr]

            range_start: int = int(match.group('start'))
            range_end: int = int(match.group('end'))

            if range_start > range_end:
                range_start, range_end = range_end, range_start

            results: list[str] = []
            for i in range(range_start, range_end + 1):
                replaced: str = (
                    expr[:match.start()] + str(i) + expr[match.end():]
                )
                results.extend(evaluate_ranges(replaced))

            return results

        def evaluate_expression(expr: str) -> str:
            """Evaluate arithmetic within brackets using safe AST evaluation."""
            var_pattern = re.compile(r"\[(?P<maths>.*?)\]")

            def _safe_eval(node: ast.AST) -> int | float:
                if isinstance(node, ast.Expression):
                    return _safe_eval(node.body)
                elif isinstance(node, ast.Constant):
                    if isinstance(node.value, (int, float)):
                        return node.value
                    msg1: str = f"Unsupported constant: {type(node.value)}"
                    raise ValueError(msg1)
                elif isinstance(node, ast.BinOp):
                    left = _safe_eval(node.left)
                    right = _safe_eval(node.right)
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
                        msg2: str = f"Unsupported operator: {op_type.__name__}"
                        raise ValueError(msg2)
                    return ops[op_type](left, right)
                elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                    return -_safe_eval(node.operand)
                msg3: str = f"Unsupported AST node: {type(node).__name__}"
                raise TypeError(msg3)

            def replace_maths(match: re.Match[str]) -> str:
                try:
                    maths: str = re.sub(r"\s", "", match.group("maths"))
                    tree: ast.Expression = ast.parse(maths, mode="eval")
                    result: int | float = _safe_eval(tree)
                    return str(
                        int(result) if isinstance(result, float)
                        and result.is_integer() else result
                    )
                except Exception:
                    return match.group(0)

            return var_pattern.sub(replace_maths, expr)

        # Pipeline: substitute variables → expand ranges → evaluate arithmetic
        substituted: str = evaluate_variables(expression)
        expanded: list[str] = evaluate_ranges(substituted)
        return [evaluate_expression(r) for r in expanded]

    def reset_register_timestamps(self) -> None:
        """Reset the next_read_timestamp values for all registry entries"""
        for registry_type in Registry_Type:
            if registry_type in self.registry_map:
                for entry in self.registry_map[registry_type]:
                    entry.next_read_timestamp = 0.0
        self._log.debug(f"Reset timestamps for all registry entries in protocol {self.protocol}")


#settings = protocol_settings('v0.14')
