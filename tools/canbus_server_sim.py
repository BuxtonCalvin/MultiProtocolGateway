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
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

import can

VCAN_IFACE : str = 'vcan0'
VCAN_BUSTYPE : str = 'socketcan'
vcan_messages = []


def load_candump_file(filepath):
    os.chdir(Path(__file__).resolve().parent)

    messages = []

    with open(filepath, 'r') as f:
        for line in f:
            line: str = line.strip()
            if not line or '#' not in line:
                continue

            try:
                can_data: str = line.split(' ')[-1]

                can_id_str, data_str = can_data.split('#')
                can_id = int(can_id_str, 16)
                data: bytes = bytes.fromhex(data_str)

                msg = can.Message(
                    arbitration_id=can_id,
                    data=data,
                    is_extended_id=False
                )
                messages.append(msg)
            except Exception as e:
                print(f"Failed to parse line '{line}': {e}")

    return messages


def emulate_device() -> NoReturn:
    bus: can.BusABC = can.interface.Bus(channel=VCAN_IFACE, interface=VCAN_BUSTYPE, bitrate=500000)

    while True:
        for msg in vcan_messages:
            try:
                bus.send(msg)
                print(f"Sent message: {msg}")
            except can.CanError:
                print("Message NOT sent")
            time.sleep(1)  # Send message every 1 second

def setup_vcan(interface=VCAN_IFACE) -> bool:
    # Safely skip Linux network setup if running on Windows
    if sys.platform == "win32":
        print("Windows detected. Using software virtual CAN bus fallback.")
        return False

    try:
        # Absolute paths for Linux systems to satisfy the linter
        subprocess.run(['/usr/bin/sudo', '/sbin/modprobe', 'vcan'], check=True)
        subprocess.run(['/usr/bin/sudo', '/sbin/ip', 'link', 'add', 'dev', interface, 'type', 'vcan'], check=True)
        subprocess.run(['/usr/bin/sudo', '/sbin/ip', 'link', 'set', 'up', interface], check=True)

        print(f"Virtual CAN interface {interface} is ready.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to set up {interface}: {e}")

    return False


def cleanup_vcan(interface=VCAN_IFACE) -> bool | None:
    # Safely skip Linux network setup if running on Windows
    if sys.platform == "win32":
        print("Windows detected. Using software virtual CAN bus fallback.")
        return False
    try:
        subprocess.run(['sudo', 'ip', 'link', 'delete', interface], check=True)
        print(f"Removed {interface}")
    except subprocess.CalledProcessError as e:
        print(f"Error removing {interface}: {e}")
        return False

    return True

# Register cleanup to run at program exit
atexit.register(cleanup_vcan)

# Optional: Handle Ctrl+C gracefully
signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))


if __name__ == "__main__":

    filename = "candump.log"
    if len(sys.argv) > 1:
        filename: str = sys.argv[1]
    else:
        print("Usage: python canbus_server_sim.py <candump_file>")
        print("Using default 'candump.log' file.")

    if not setup_vcan():
        VCAN_BUSTYPE = 'virtual'

    vcan_messages = load_candump_file(filename)
    emulate_device()

