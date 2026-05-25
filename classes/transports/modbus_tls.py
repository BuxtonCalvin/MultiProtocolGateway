# Description: scraper for Modbus TLS devices, inheriting from modbus_base and implementing TLS-specific client setup and register access logic.
# File: modbus_tls.py
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

# scraper for Modbus TLS devices, inheriting from modbus_base and implementing TLS-specific client setup and register access logic.
from pathlib import Path
from typing import Any, cast

from packaging import version
from pymodbus import __version__ as pymodbus_version
from pymodbus.client import ModbusTlsClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base


class modbus_tls(modbus_base):

    transport_type = "scraper"
    def __init__(self, settings: TransportSettings) -> None:
        super().__init__(settings)

        # Path(__file__) is the path to the current script (modbus_tls.py)
        # .parent.parent moves up 2 levels from 'transports/modbus_tls.py' to 'app_root/' ie MultiProtocolGateway
        app_root: Path = Path(__file__).resolve().parent.parent
        config_dir: Path = app_root / "config"

        self.host = settings.get("host", fallback="")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=502)
        self.certfile: str = settings.get("certfile", "")
        self.keyfile: str = settings.get("keyfile", "")
        self.hostname: str = settings.get("hostname", self.host)

        cert_path: Path = config_dir / self.certfile
        key_path: Path = config_dir / self.keyfile

        # 1. Thread-safe client caching logic
        client_str: str = f"{self.host}-tls-{self.port}"
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        # 2. Version detection
        # Falls back to a safe legacy mode if version cannot be parsed
        try:
            self.curr_version: version.Version = version.parse(pymodbus_version)
            is_modern = self.curr_version >= version.parse("3.7.0")

        except Exception:
            self.curr_version: version.Version = version.parse("0.0.0")
            is_modern: bool = hasattr(ModbusTlsClient, "generate_ssl")

        # 3. Construct version-specific arguments
        client_args = {
            "host": self.host,
            "port": self.port,
            "timeout": 7,
            "retries": 3,
        }
        if is_modern:
            # Pymodbus 3.7.0+
            if not (cert_path.is_file() and key_path.is_file()):
                self._log.error(f"TLS Files missing at: {cert_path.absolute()} or {key_path.absolute()}")

                msg = "SSL cert or key not found. Ensure they are on the host in the config folder."
                raise FileNotFoundError(msg)

            # 3. generate_ssl usually expects strings, so convert them back with str()
            client_args["sslctx"] = ModbusTlsClient.generate_ssl(
                certfile=str(cert_path),
                keyfile=str(key_path)
            )
            client_args["server_hostname"] = self.hostname # Renamed from 'hostname'
        else:
            # Legacy Pymodbus support
            client_args["certfile"] = self.certfile
            client_args["keyfile"] = self.keyfile
            client_args["hostname"] = self.hostname


        # 4. Initialize and cache the client
        self.client = cast(ModbusBaseClient, ModbusTlsClient(**client_args))

        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def write_register(self, register: int, value: int, **kwargs) -> None:
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_register called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)
        port_lock = self._get_port_lock()
        with port_lock:
            self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register


    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        kwargs = self._get_correct_device_arg(kwargs)
        result: Any = None
        # no need for a lock here since the client handles its own internal locking and
        # we don't have any shared state to protect in this method.  If we were to add retries or other
        # logic that re-enters this method, we would need to add a lock to prevent concurrent access to the client.
        try:
            if registry_type == Registry_Type.INPUT:
                result = self.client.read_input_registers(start, count=count, **kwargs)
            elif registry_type == Registry_Type.HOLDING:
                result = self.client.read_holding_registers(start, count=count, **kwargs)
            else:
                self._log.warning(f"read_registers: unsupported registry_type '{registry_type.name}' for TCP transport — returning None")
                return None

            if isinstance(result, ModbusIOException): # pymodbus 3.0+ returns ModbusIOException objects instead of raising them
                print(f"Modbus IO Exception returned as object: {result}")
                return None

        except ConnectionException:
            self._log.error(f"Connection lost to {self.transport_name} at {self.host}:{self.port}")
            self._log.error(f"❌ [COMMUNICATION LOST] --- Name: {self.transport_name} ---")
            self.connected = False
            self._needs_reconnection = True
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
            # pymodbus connect() usually returns True/False
            self.connected = bool(self.client.connect())

            if self.connected:
                self._log.info(f"Modbus TLS connected: {self.connected} for {self.transport_name}")
                super().connect()
            else:
                self._log.error(f"Failed to establish TLS connection to {self.host}:{self.port}")

            return self.connected  # noqa: TRY300
        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False
            return False
