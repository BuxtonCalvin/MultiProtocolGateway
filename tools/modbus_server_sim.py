# Description: simulate modbus tcp server for testing mpg
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

''' simulate modbus tcp server for testing mpg '''
from __future__ import annotations

import json
from typing import Any

from modbus_tk import hooks, modbus_tcp  # type: ignore[import-untyped]
from modbus_tk.defines import (  # type: ignore[import-untyped]
    HOLDING_REGISTERS,  # type: ignore[import-untyped]
    READ_INPUT_REGISTERS,  # type: ignore[import-untyped]
)


def on_write_request(request: Any) -> None:
    print(f"Write request: {request}")


server = modbus_tcp.TcpServer(address="0.0.0.0", port=5020)  # type: ignore[import-untyped]  # noqa: S104
slave = server.add_slave(1) # type: ignore[import-untyped]

#load registries
input_save_path: str = "input_registry.json"
holding_save_path: str = "holding_registry.json"

#load previous scan if enabled and exists
with open(input_save_path, "r") as file:
    raw_input_registry: dict[str, int] = json.load(file)

with open(holding_save_path, "r") as file:
    raw_holding_registry: dict[str, int] = json.load(file)

# Convert keys to integers
input_registry: dict[int, int] = {int(key): value for key, value in raw_input_registry.items()}
holding_registry: dict[int, int] = {int(key): value for key, value in raw_holding_registry.items()}

if not input_registry or not holding_registry:
    raise ValueError("input_registry and holding_registry must each contain at least one entry")

slave.add_block('INPUT', READ_INPUT_REGISTERS, 0, max(input_registry.keys()) + 1) # type: ignore[import-untyped]
slave.add_block('HOLDING', HOLDING_REGISTERS, 0, max(holding_registry.keys()) + 1) # type: ignore[import-untyped]

for address, value in input_registry.items():
    slave.set_values('INPUT', address, [value]) # type: ignore[import-untyped]

for address, value in holding_registry.items():
    slave.set_values('HOLDING', address, [value]) # type: ignore[import-untyped]

server.start() # type: ignore[import-untyped]
print("Modbus server is running on port 5020...")

hooks.install_hook("modbus.Server.before_handle_request", on_write_request) # type: ignore[import-untyped]

try:
    while True:
        pass
except KeyboardInterrupt:
    print("Stopping server...")
    server.stop() # type: ignore[import-untyped]
