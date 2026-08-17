# Description: Implements common functionality for the MultiProtocolGateway application.
# File: common.py
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

import os
import re

# classes/transport_types.py
from typing import Optional, Protocol, runtime_checkable

from serial.tools import list_ports


@runtime_checkable
class TransportSettings(Protocol):
    """
    Interface contract for transport configuration sections.
    Implemented by CustomConfigParser's SectionProxy with extended
    list-option support. Used as the settings parameter type across
    transport_base, protocol_settings, and bridge transports.
    """

    @property
    def name(self) -> str: ...

    def get(self, option: str | list[str], fallback: object = None, **kwargs: object) -> str: ...

    def getint(self, option: str | list[str], fallback: object = None, **kwargs: object) -> int: ...

    def getfloat(self, option: str | list[str], fallback: object = None, **kwargs: object) -> float: ...

    def getboolean(self, option: str | list[str], fallback: object = None, **kwargs: object) -> bool: ...

    def __contains__(self, key: object) -> bool: ...

    def __getitem__(self, key: str) -> str: ...


def strtobool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val

    val = str(val).lower().strip()
    if val in ("y", "yes", "t", "true", "on", "1"):
        return True
    if val in ("n", "no", "f", "false", "off", "0", ""):
        return False
    msg: str = f"Invalid truth value: {val}"
    raise ValueError(msg)

def strtoint(val: str | int) -> int | str:
    if isinstance(val, int):
        return val

    # Standardize input
    s: str = str(val).lower().strip()
    if not s:
        return 0

    # Handle 'x' prefix by converting to standard '0x'
    if s.startswith('x'):
        s = '0x' + s[1:]

    try:
        # base=0 automatically detects hex (0x), octal (0o), or decimal
        # int(s, base=0) replaces all that manual padding and from_bytes logic. It handles 0x naturally.
        return int(s, base=0)
    except ValueError:
        # This catches strings with letters that aren't valid hex
        return s

def strtoint_safe(val: str | int, context: str = "") -> int:
    """
    Converts val to int, raising ValueError if conversion fails.
    Use when the caller guarantees val is a valid numeric string
    and a non-int result indicates malformed data.

    Args:
        val:     The value to convert.
        context: Optional description for the error message.
    """
    result: int | str = strtoint(val)
    if isinstance(result, int):
        return result
    msg = f"strtoint_safe: could not convert {repr(val)} to int ({context})" if context else ""
    raise ValueError(msg)

def get_usb_serial_port_info(port : str = "") -> str:

    # If port is a symlink
    if os.path.islink(port):
        port = os.path.realpath(port)

    for p in list_ports.comports(): #from serial.tools
        if str(p.device).upper() == port.upper():
            vid: str = hex(p.vid) if p.vid is not None else ""
            pid: str = hex(p.pid) if p.pid is not None else ""
            serial: str = str(p.serial_number) if p.serial_number is not None else ""
            location: str = str(p.location) if p.location is not None else ""
            return "["+vid+":"+pid+":"+serial+":"+location+"]"

    return ""

def find_usb_serial_port(port: str = "", vendor_id: str = "", product_id: str = "",
                         serial_number: str = "", location: str = "") -> Optional[str]:

    # 1. Handle direct paths/symlinks first
    if port and os.path.islink(port):
        port = os.path.realpath(port)
    if port and not port.startswith("["):
        return port

    # 2. Extract values from the pattern string if provided
    port = port.replace("None", "")
    pattern = r"\[(?P<vendor>[\da-zA-Z]*):?(?P<product>[\da-zA-Z]*):?(?P<serial>[\da-zA-Z]*):?(?P<location>[\d\-]*)\]"
    match: re.Match[str] | None = re.match(pattern, port)

    if match:
        # Use values from string if they exist, otherwise keep function arguments
        v_match: str = match.group("vendor")
        p_match: str = match.group("product")

        # Convert hex strings to int only if they exist in the pattern
        v_id: int | None = int(v_match, 16) if v_match else (int(vendor_id, 16) if vendor_id else None)
        p_id: int | None = int(p_match, 16) if p_match else (int(product_id, 16) if product_id else None)
        s_num: str = match.group("serial") or serial_number
        loc: str = match.group("location") or location

        # 3. Search based on the merged criteria
        for p in list_ports.comports():
            if ((not v_id or p.vid == v_id) and
                (not p_id or p.pid == p_id) and
                (not s_num or p.serial_number == s_num) and
                (not loc or p.location == loc)):
                return p.device
    else:
        if any([vendor_id, product_id, serial_number, location]):
            # 3. Simplified assignment that satisfies the type checker
            v_id = int(vendor_id, 16) if vendor_id else None
            p_id = int(product_id, 16) if product_id else None

            for p in list_ports.comports():
                if ((not v_id or p.vid == v_id) and
                    (not p_id or p.pid == p_id) and
                    (not serial_number or p.serial_number == serial_number) and
                    (not location or p.location == location)):
                    return p.device
    return None
