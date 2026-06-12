# Description: scraper for Modbus RTU devices over RS-232/RS-485 serial, inheriting from modbus_base and implementing RTU-specific client setup and register access logic.
# File: modbus_rtu.py
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


# scraper for Modbus RTU devices over RS-232/RS-485 serial, inheriting from modbus_base and implementing
# RTU-specific client setup and register access logic.

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from pymodbus.client import ModbusSerialClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from classes.protocol_settings import Registry_Type
from defs.common import (
    TransportSettings,
    find_usb_serial_port,
    get_usb_serial_port_info,
)

from .modbus_base import modbus_base

if TYPE_CHECKING:
    from threading import Lock

class modbus_rtu(modbus_base):


    transport_type: str = "scraper"
    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        self.client: ModbusBaseClient | None = None

        self.port = settings.get("port", fallback="/dev/ttyUSB0")
        if not self.port:
            raise ValueError("Port is not set")

        self.port = find_usb_serial_port(self.port)
        if not self.port:
            raise ValueError("Port is not valid / not found")

        self._log.info(f"Serial Port: {self.port} = {get_usb_serial_port_info(self.port)}")

        self.baudrate = settings.getint("baudrate", fallback=9600)

        address : int = settings.getint("address", 0)
        self.addresses: list[int] = [address]

        client_str: str = f"{self.port}({self.baudrate})"
        # Thread-safe client access
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        self._log.debug(f"Creating new client with baud rate: {self.baudrate}")


        client_args = {
            "port": self.port,
            "baudrate": int(self.baudrate),
            "stopbits": settings.getint("stopbits", fallback=1),
            "parity": settings.get("parity", fallback="N"),
            "bytesize": settings.getint("bytesize", fallback=8),
            "timeout": settings.getfloat("timeout", fallback=2.0),
        }

        self.client = cast(ModbusBaseClient, ModbusSerialClient(**client_args))

        #add to clients (thread-safe)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        kwargs = self._get_correct_device_arg(kwargs)
        port_lock: Lock = self._get_port_lock()
        result: Any = None
        with port_lock:
            try:
                if registry_type == Registry_Type.INPUT:
                    result = self.client.read_input_registers(start, count=count, **kwargs)
                elif registry_type == Registry_Type.HOLDING:
                    result = self.client.read_holding_registers(start, count=count, **kwargs)
                elif registry_type == Registry_Type.COIL:
                    result = self.client.read_coils(start, count=count, **kwargs)
                elif registry_type == Registry_Type.DISCRETE:
                    result = self.client.read_discrete_inputs(start, count=count, **kwargs)
                else:
                    self._log.warning(
                        f"read_registers: unsupported registry_type '{registry_type.name}' for RTU transport — returning None")
                    return None

                if isinstance(result, ModbusIOException): # pymodbus 3.0+ returns ModbusIOException objects instead of raising them
                    print(f"Modbus IO Exception returned as object: {result}")
                    return None
            except ConnectionException:
                self._log.error(f"Connection lost to {self.transport_name} at {self.port}")
                self._log.error(f"❌ [COMMUNICATION LOST] --- Name: {self.transport_name} ---")
                self.connected = False
                return None
            except ModbusIOException as e:
                print(f"Modbus IO Exception caught: {e}")
                return None
            except ModbusException as e:
                self._log.error(f"General Modbus error on {self.transport_name}: {e}")
                return None
            except Exception as e:
                self._log.error(f"Unexpected error during read: {e}")
                return None

    def connect(self) -> bool:
        if self.client is None:
            self._log.error(f"Cannot connect {self.transport_name} — client not initialized")
            self.connected = False
            return False

        try:
            # pymodbus connect() returns a boolean-like value
            self.connected = bool(self.client.connect())

            if self.connected:
                self._log.info(f"Modbus RTU connected: {self.connected} for {self.transport_name} on port {self.port}")
                super().connect()
            else:
                self._log.error(f"Failed to establish RTU connection to {self.port}")

        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False

        return self.connected





