# Description: simulate a modbus server (TCP or RTU-over-serial) for testing mpg
# File: modbus_server_sim.py
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

''' simulate a modbus server (TCP or RTU-over-serial) for testing mpg '''
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import serial  # type: ignore[import-untyped]
from modbus_tk import hooks, modbus_rtu, modbus_tcp  # type: ignore[import-untyped]
from modbus_tk.defines import (  # type: ignore[import-untyped]
    HOLDING_REGISTERS,  # type: ignore[import-untyped]
    READ_INPUT_REGISTERS,  # type: ignore[import-untyped]
)

# A sensible default serial device name per platform. On Windows this is a
# COM port (e.g. exposed by a USB-to-RS485/RS232 adapter's driver, visible
# in Device Manager); on Linux/macOS it's a /dev entry.
DEFAULT_SERIAL_PORT: str = "COM3" if sys.platform == "win32" else "/dev/ttyUSB0"


def on_write_request(request: Any) -> None:
    print(f"Write request: {request}")


def load_registry(path: str) -> dict[int, int]:
    with open(path, "r") as file:
        raw_registry: dict[str, int] = json.load(file)
    return {int(key): value for key, value in raw_registry.items()}


def build_slave(server: Any, slave_id: int, input_registry: dict[int, int], holding_registry: dict[int, int]) -> None:
    slave = server.add_slave(slave_id)  # type: ignore[import-untyped]

    if not input_registry or not holding_registry:
        raise ValueError("input_registry and holding_registry must each contain at least one entry")

    slave.add_block('INPUT', READ_INPUT_REGISTERS, 0, max(input_registry.keys()) + 1)  # type: ignore[import-untyped]
    slave.add_block('HOLDING', HOLDING_REGISTERS, 0, max(holding_registry.keys()) + 1)  # type: ignore[import-untyped]

    for address, value in input_registry.items():
        slave.set_values('INPUT', address, [value])  # type: ignore[import-untyped]

    for address, value in holding_registry.items():
        slave.set_values('HOLDING', address, [value])  # type: ignore[import-untyped]


def build_tcp_server(host: str, port: int) -> Any:
    return modbus_tcp.TcpServer(address=host, port=port)  # type: ignore[import-untyped]  # noqa: S104


def build_rtu_server(
    port: str,
    baudrate: int,
    bytesize: int,
    parity: str,
    stopbits: int,
) -> Any:
    ser = serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
    )
    return modbus_rtu.RtuServer(ser)  # type: ignore[import-untyped]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["tcp", "rtu"], default="tcp",
        help="Transport to simulate over: 'tcp' (default) or 'rtu' (serial, e.g. USB-RS485)."
    )
    parser.add_argument("--slave-id", type=int, default=1, help="Modbus slave/unit id (default: 1).")
    parser.add_argument("--input-registry", default="input_registry.json", help="Path to input registry JSON.")
    parser.add_argument("--holding-registry", default="holding_registry.json", help="Path to holding registry JSON.")

    tcp_group = parser.add_argument_group("tcp mode")
    tcp_group.add_argument("--host", default="0.0.0.0", help="TCP bind address (default: 0.0.0.0).")  # noqa: S104
    tcp_group.add_argument("--tcp-port", type=int, default=5020, help="TCP port (default: 5020).")

    rtu_group = parser.add_argument_group("rtu mode")
    rtu_group.add_argument(
        "--serial-port", default=DEFAULT_SERIAL_PORT,
        help=f"Serial device, e.g. COM3 (Windows) or /dev/ttyUSB0 (Linux). Default: {DEFAULT_SERIAL_PORT}"
    )
    rtu_group.add_argument("--baudrate", type=int, default=9600, help="Serial baud rate (default: 9600).")
    rtu_group.add_argument("--bytesize", type=int, default=8, choices=[5, 6, 7, 8], help="Data bits (default: 8).")
    rtu_group.add_argument("--parity", default="N", choices=["N", "E", "O"], help="Parity (default: N).")
    rtu_group.add_argument("--stopbits", type=int, default=1, choices=[1, 2], help="Stop bits (default: 1).")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    input_registry = load_registry(args.input_registry)
    holding_registry = load_registry(args.holding_registry)

    if args.mode == "tcp":
        server = build_tcp_server(args.host, args.tcp_port)
    else:
        try:
            server = build_rtu_server(
                args.serial_port, args.baudrate, args.bytesize, args.parity, args.stopbits
            )
        except serial.SerialException as e:
            print(f"Could not open serial port {args.serial_port!r}: {e}")
            if sys.platform == "win32":
                print("Check Device Manager > Ports (COM & LPT) for the correct COM port number,")
                print("and that no other program (e.g. a terminal emulator) has it open.")
            else:
                print(f"Check that {args.serial_port} exists and you have permission to access it")
                print("(on Linux you may need to be in the 'dialout' group).")
            sys.exit(1)

    build_slave(server, args.slave_id, input_registry, holding_registry)

    server.start()  # type: ignore[import-untyped]
    if args.mode == "tcp":
        print(f"Modbus TCP server is running on {args.host}:{args.tcp_port}...")
    else:
        print(
            f"Modbus RTU server is running on {args.serial_port} "
            f"({args.baudrate} {args.bytesize}{args.parity}{args.stopbits})..."
        )

    hooks.install_hook("modbus.Server.before_handle_request", on_write_request)  # type: ignore[import-untyped]

    try:
        while True:
            # A bare `pass` here busy-waits and pins a CPU core at 100%, and on
            # Windows it also delays delivery of the Ctrl+C KeyboardInterrupt
            # since signals are only checked between bytecode instructions.
            # A short sleep fixes both, cross-platform.
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping server...")
        server.stop()  # type: ignore[import-untyped]


if __name__ == "__main__":
    main()
