# Description: scraper for Modbus TCP devices, inheriting from modbus_base and implementing TCP-specific client setup and register access logic.
# File: modbus_tcp.py
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

# scraper for Modbus TCP devices, inheriting from modbus_base and implementing TCP-specific client setup and register access logic.
from typing import Any, cast

from packaging import version
from pymodbus import __version__ as pymodbus_version
from pymodbus.client import ModbusTcpClient
from pymodbus.client.base import ModbusBaseClient, ModbusBaseSyncClient
from pymodbus.exceptions import ConnectionException, ModbusException
from pymodbus.pdu import ExceptionResponse, ModbusPDU

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base


class modbus_tcp(modbus_base):

    transport_type: str = "scraper"
    pymodbus_slave_arg: str = "unit"  # default legacy device arg
    client = cast(ModbusBaseClient, ModbusTcpClient)

    def __init__(self, settings : TransportSettings ) -> None:
        super().__init__(settings)

        self.host = settings.get("host", "")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=502)

        try:
            self.curr_version: version.Version = version.parse(pymodbus_version)
        except Exception:
            self.curr_version: version.Version = version.parse("0.0.0")

        client_str: str = f"{self.host}-tcp-{self.port}"
        #check if client is already initialized
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                self._log.debug(f"Reusing cached client for '{client_str}' (id={id(self.client)})" )
                return

        timeout: int = settings.getint("timeout", fallback=7)
        retries: int = settings.getint("retries", fallback=3)
        self.client = cast(ModbusBaseClient, ModbusTcpClient(host=self.host, port=self.port, timeout=timeout, retries=retries))
        self._log.debug(f"Created new client for '{client_str}' (id={id(self.client)})")

        #add to clients (thread-safe)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        # Cast to ModbusBaseSyncClient (ModbusClientMixin[ModbusPDU]) so the type
        # checker resolves T -> ModbusPDU and types result as ModbusPDU, not
        # Awaitable[ModbusPDU] (which is the async specialization).
        sync_client: ModbusBaseSyncClient = cast(ModbusBaseSyncClient, self.client)
        call_kwargs: dict[str, Any] = self._get_correct_device_arg(kwargs)
        result: ModbusPDU | None = None

        # Proactively verify socket health before making the call
        if hasattr(self.client, 'connected') and not self.client.connected:
            self._log.warning(f"Socket closed before read for {self.transport_name}. Triggering reconnect.")
            self.connected = False
            return None

        try:
            if registry_type == Registry_Type.INPUT:
                result = sync_client.read_input_registers(start, count=count, **call_kwargs)
            elif registry_type == Registry_Type.HOLDING:
                result = sync_client.read_holding_registers(start, count=count, **call_kwargs)
            elif registry_type == Registry_Type.COIL:
                result = sync_client.read_coils(start, count=count, **call_kwargs)
            elif registry_type == Registry_Type.DISCRETE:
                result = sync_client.read_discrete_inputs(start, count=count, **call_kwargs)
            else:
                self._log.warning(f"read_registers: unsupported registry_type '{registry_type.name}' for TCP transport — returning None")
                return None

        except ConnectionException:
            self._log.error(f"Connection lost to {self.transport_name} at {self.host}:{self.port}")
            self._log.error(f"❌ [Communication Lost] --- Name: {self.transport_name} ---")
            self.connected = False
            return None
        except (BrokenPipeError, ConnectionResetError, ConnectionError) as e:
            # Explicitly catch the OS-level socket disconnection errors
            self._log.error(f"Socket pipe broken for {self.transport_name}: {e}")
            self._log.error(f"❌ [Communication Lost] --- Name: {self.transport_name} ---")

            # Safely tear down the dead client state internally
            try:
                self.client.close()
            except Exception:
                self._log.error(f"❌ [Could not close client] --- Name: {self.transport_name} ---")
                pass

            self.connected = False
            return None
        except ModbusException as e:
            self._log.error(f"General Modbus error on {self.transport_name}: {e}")
            return None
        except Exception as e:
            self._log.error(f"Unexpected error during read: {e}")
            return None

        if isinstance(result, ExceptionResponse):
            self._log.error("Modbus Error: Result is None")
            return None

        # Use hasattr to safely check for the error attribute/method
        is_error: bool | Any = result.isError() if hasattr(result, "isError") else getattr(result, "is_error", False)

        if is_error:
            self._log.error(f"Modbus Error: {result}")
            return None

        return result


    def connect(self) -> bool:
        if self.client is None:
            self._log.error(f"Cannot connect {self.transport_name} — client not initialized")
            self.connected = False
            return False

        try:
            # Ensure old socket hooks are entirely wiped out before reconnecting
            try:
                self.client.close()
            except Exception:
                self._log.error(f"Failed to close TCP connection to {self.host}:{self.port}")
                pass

            # pymodbus connect() usually returns True/False
            self.connected = bool(self.client.connect())

            if self.connected:
                self._log.info(f"Modbus TCP connected: {self.connected} for {self.transport_name}")
                super().connect()
            else:
                self._log.error(f"Failed to establish TCP connection to {self.host}:{self.port}")

        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False
            return False

        return self.connected
