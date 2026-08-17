# Description: scraper for Modbus UDP devices, inheriting from modbus_base and implementing UDP-specific client setup and register access logic.
# File: modbus_udp.py
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

# scraper for Modbus UDP devices, inheriting from modbus_base and implementing UDP-specific client setup and register access logic.
from threading import Lock
from typing import TYPE_CHECKING

from pymodbus.client.base import ModbusBaseSyncClient
from pymodbus.client.udp import ModbusUdpClient
from pymodbus.pdu import ExceptionResponse, ModbusPDU

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base

if TYPE_CHECKING:
    from threading import Lock

class modbus_udp(modbus_base):


    transport_type: str = "scraper"
    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        self.host = settings.get("host", fallback="")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=502)

        client_str: str = self.host+"-udp-"+str(self.port)
        #check if client is already initialized
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return
        self.timeout: int = settings.getint("timeout", fallback=7)
        self.retries: int = settings.getint("retries", fallback=3)

        # Concrete pymodbus client classes (ModbusUdpClient etc.) are already
        # ModbusBaseSyncClient subtypes, so no cast is needed for assignment
        # to self.client (declared as Optional[ModbusBaseSyncClient] on the
        # base class). The local sync_client alias in read_registers below
        # gives static analysis the concrete Generic[ModbusPDU] binding for
        # response types.
        self.client = ModbusUdpClient(host=self.host, port=self.port, timeout=self.timeout, retries=self.retries)

        #add to clients
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, device_id: int | None = None) -> ModbusPDU | None:
        # read_registers method to handle retries and prevent "fire and forget" failures
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        sync_client: ModbusBaseSyncClient = self.client
        resolved_device_id: int = self._get_device_id(device_id)
        port_lock: Lock = self._get_port_lock()

        with port_lock:
        # Try the operation up to 'retries' times
            for attempt in range(self.retries):
                try:
                    if registry_type == Registry_Type.INPUT:
                        response: ModbusPDU = sync_client.read_input_registers(start, count=count, device_id=resolved_device_id)
                    elif registry_type == Registry_Type.HOLDING:
                        response = sync_client.read_holding_registers(start, count=count, device_id=resolved_device_id)
                    elif registry_type == Registry_Type.COIL:
                        response = sync_client.read_coils(start, count=count, device_id=resolved_device_id)
                    elif registry_type == Registry_Type.DISCRETE:
                        response = sync_client.read_discrete_inputs(start, count=count, device_id=resolved_device_id)
                    else:
                        self._log.warning(
                            f"read_registers: unsupported registry_type '{registry_type.name}' for UDP transport — returning None"
                        )
                        return None

                    # pymodbus v3+: sync clients return ModbusPDU directly.
                    # Error responses are ExceptionResponse instances, not flagged via isError().
                    if not isinstance(response, ExceptionResponse):
                        return response

                    self._log.warning(f"Modbus UDP Attempt {attempt + 1} failed: {response}")

                except Exception as e:
                    self._log.error(f"Network error on attempt {attempt + 1}: {e}")

            # If the loop finishes without returning, all retries failed
            self._log.error(f"Failed to read {registry_type} after {self.retries} attempts.")
            return None

    def connect(self) -> bool:
        if self.client is None:
            self._log.error(f"Cannot connect '{self.transport_name}' — client not initialized")
            self.connected = False
            return False

        try:
            # pymodbus connect() registers the UDP endpoint
            self.connected = bool(self.client.connect())

            if self.connected:
                self._log.info(f"Modbus UDP configured: {self.connected} for {self.transport_name} on port {self.port}")
                super().connect()
            else:
                self._log.error(f"Failed to configure UDP transport for {self.transport_name} on port {self.port}")

        except Exception as e:
            self._log.error(f"Exception during UDP configuration: {e}")
            self.connected = False

        return self.connected
