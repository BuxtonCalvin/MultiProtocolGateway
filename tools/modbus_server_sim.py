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

from modbus_tk import hooks, modbus_tcp
from modbus_tk.defines import HOLDING_REGISTERS, READ_INPUT_REGISTERS


def on_write_request(request):
    print(f"Write request: {request}")


server = modbus_tcp.TcpServer(address="0.0.0.0", port=5020)  # noqa: S104
slave = server.add_slave(1)

#load registries
input_save_path = "input_registry.json"
holding_save_path = "holding_registry.json"

#load previous scan if enabled and exists
with open(input_save_path, "r") as file:
    input_registry = json.load(file)

with open(holding_save_path, "r") as file:
    holding_registry = json.load(file)

# Convert keys to integers
input_registry = {int(key): value for key, value in input_registry.items()}
holding_registry = {int(key): value for key, value in holding_registry.items()}

slave.add_block('INPUT', READ_INPUT_REGISTERS, 0, max(input_registry.keys()) +1 )
slave.add_block('HOLDING', HOLDING_REGISTERS,  0, max(holding_registry.keys()) +1)

for address, value in input_registry.items():
    slave.set_values('INPUT', address, [value])

for address, value in holding_registry.items():
    slave.set_values('HOLDING', address, [value])

server.start()
print("Modbus server is running on port 5020...")

hooks.install_hook("modbus.Server.before_handle_request", on_write_request)

try:
    while True:
        pass
except KeyboardInterrupt:
    print("Stopping server...")
    server.stop()
