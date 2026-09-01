# Description: Provides a developer utility for working with MultiProtocolGateway data or simulations.
# File: canbus_server_sim.py
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

import atexit
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Final, NoReturn

import can

VCAN_IFACE: Final[str] = 'vcan0'
VCAN_BUSTYPE: Final[str] = 'socketcan'

# Windows has no SocketCAN/vcan support, so we fall back to python-can's
# udp_multicast backend instead. Unlike the plain 'virtual' bustype (which
# only works for buses that live in the same Python process), udp_multicast
# uses real UDP sockets, so a separate process (e.g. the actual gateway
# software under test) can join the same multicast group and receive the
# simulated frames, just like it would with vcan0 on Linux.
# Requires the optional 'msgpack' dependency: pip install msgpack
WIN_CHANNEL: Final[str] = '239.74.163.2'  # python-can's default IPv4 mcast group
WIN_BUSTYPE: Final[str] = 'udp_multicast'

vcan_messages: list[can.Message] = []
_vcan_ready: bool = False  # only True once setup_vcan() has actually created the interface

# Linux network interface names are limited to IFNAMSIZ-1 (15) characters and
# may not contain path separators, whitespace, or shell metacharacters.
_IFACE_NAME_RE: Final[re.Pattern[str]] = re.compile(r'^[A-Za-z0-9_.-]{1,15}$')


def _validate_interface_name(interface: str) -> str:
    """Ensure `interface` is safe to pass to subprocess before it reaches a command line."""
    if not _IFACE_NAME_RE.fullmatch(interface):
        msg: str = f"Refusing to use unsafe interface name: {interface!r}"
        raise ValueError(msg)
    return interface


def load_candump_file(filepath: str) -> list[can.Message]:
    os.chdir(Path(__file__).resolve().parent)

    messages: list[can.Message] = []

    with open(filepath, 'r') as f:
        for raw_line in f:
            stripped_line: str = raw_line.strip()
            if not stripped_line or '#' not in stripped_line:
                continue

            try:
                can_data: str = stripped_line.split(' ')[-1]

                can_id_str, data_str = can_data.split('#', 1)
                can_id: int = int(can_id_str, 16)
                data: bytes = bytes.fromhex(data_str)

                msg = can.Message(
                    arbitration_id=can_id,
                    data=data,
                    is_extended_id=False
                )
                messages.append(msg)
            except Exception as e:
                print(f"Failed to parse line '{stripped_line}': {e}")

    return messages


def emulate_device(bustype: str = VCAN_BUSTYPE, channel: str = VCAN_IFACE) -> NoReturn:
    bus: can.BusABC = can.interface.Bus(channel=channel, interface=bustype, bitrate=500000)

    while True:
        for msg in vcan_messages:
            try:
                bus.send(msg)
                print(f"Sent message: {msg}")
            except can.CanError:
                print("Message NOT sent")
            time.sleep(1)  # Send message every 1 second

def setup_vcan(interface: str = VCAN_IFACE) -> bool:
    global _vcan_ready

    # Safely skip Linux network setup if running on Windows
    if sys.platform == "win32":
        return False

    interface = _validate_interface_name(interface)

    try:
        # Absolute paths for Linux systems to satisfy the linter
        # interface is validated by _validate_interface_name() above, so this
        # is not attacker-controlled input reaching subprocess.
        subprocess.run(['/usr/bin/sudo', '/sbin/modprobe', 'vcan'], check=True)  # nosec B603
        subprocess.run(  # noqa: S603  # nosec B603
            ['/usr/bin/sudo', '/sbin/ip', 'link', 'add', 'dev', interface, 'type', 'vcan'], check=True
        )
        subprocess.run(  # noqa: S603  # nosec B603
            ['/usr/bin/sudo', '/sbin/ip', 'link', 'set', 'up', interface], check=True
        )

        print(f"Virtual CAN interface {interface} is ready.")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Failed to set up {interface}: {e}")
        return False
    else:
        _vcan_ready = True
        return True


def cleanup_vcan(interface: str = VCAN_IFACE) -> bool:
    global _vcan_ready

    # Nothing to tear down on Windows, or if setup never actually succeeded
    # (e.g. it failed, or we're on Windows and never touched a vcan interface).
    if sys.platform == "win32" or not _vcan_ready:
        return False

    interface = _validate_interface_name(interface)

    try:
        # Absolute paths for Linux systems to satisfy the linter
        # interface is validated by _validate_interface_name() above, so this
        # is not attacker-controlled input reaching subprocess.
        subprocess.run(  # noqa: S603  # nosec B603
            ['/usr/bin/sudo', '/sbin/ip', 'link', 'delete', interface], check=True
        )
        print(f"Removed {interface}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error removing {interface}: {e}")
        return False
    else:
        _vcan_ready = False
        return True


def setup_transport() -> tuple[str, str]:
    """Pick a (channel, bustype) pair appropriate for the current OS.

    Linux: real vcan/SocketCAN interface (falls back to an in-process
    'virtual' bus if vcan setup fails, e.g. no sudo/root).
    Windows: UDP-multicast virtual bus (no admin rights or kernel driver
    needed, and unlike 'virtual' it's visible to other processes).
    """
    if sys.platform == "win32":
        print(
            "Windows detected: SocketCAN/vcan isn't available, "
            f"using UDP-multicast virtual CAN bus ({WIN_CHANNEL})."
        )
        return WIN_CHANNEL, WIN_BUSTYPE

    if setup_vcan():
        return VCAN_IFACE, VCAN_BUSTYPE

    print("Falling back to an in-process virtual CAN bus (no external listeners).")
    return VCAN_IFACE, 'virtual'


# Register cleanup to run at program exit
atexit.register(cleanup_vcan)

# Optional: Handle Ctrl+C gracefully
signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))


if __name__ == "__main__":

    filename: str = "candump.log"
    if len(sys.argv) > 1:
        filename = sys.argv[1]
    else:
        print("Usage: python canbus_server_sim.py <candump_file>")
        print("Using default 'candump.log' file.")

    channel, bustype = setup_transport()

    vcan_messages = load_candump_file(filename)
    emulate_device(bustype, channel)
