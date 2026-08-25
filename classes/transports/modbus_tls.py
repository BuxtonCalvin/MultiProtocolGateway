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

import ssl

# scraper for Modbus TLS devices, inheriting from modbus_base and implementing TLS-specific client setup and register access logic.
from pathlib import Path

from pymodbus.client import ModbusTlsClient
from pymodbus.client.base import ModbusBaseSyncClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse, ModbusPDU

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base


class modbus_tls(modbus_base):


    transport_type: str = "scraper"
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
        self.timeout: int = settings.getint("timeout", fallback=7)
        self.retries: int = settings.getint("retries", fallback=3)

        cert_path: Path = config_dir / self.certfile
        key_path: Path = config_dir / self.keyfile

        # Thread-safe client caching logic
        client_str: str = f"{self.host}-tls-{self.port}"
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        if not (cert_path.is_file() and key_path.is_file()):
            self._log.error(f"TLS Files missing at: {cert_path.absolute()} or {key_path.absolute()}")

            msg = "SSL cert or key not found. Ensure they are on the host in the config folder."
            raise FileNotFoundError(msg)

        # 1. Create a standard TLS client SSL context
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        # 2. Configure certificate validation and trust settings
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True

        # 3. Load your certificates natively
        ssl_context.load_verify_locations(cafile=str(cert_path))
        ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        # 4. Initialize and cache the client.
        # NOTE: pymodbus 3.14.0's ModbusTlsClient constructor does not accept
        # a separate server_hostname/hostname override — only sslctx. That
        # means self.hostname (settings key "hostname", falling back to
        # self.host) currently has no effect: certificate/SNI validation is
        # always performed against `host` itself. If your config relies on
        # connecting via an IP while validating against a different
        # hostname, that override is not currently honored — flag this back
        # if you need it addressed.
        self.client = ModbusTlsClient(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            retries=self.retries,
            sslctx=ssl_context,
        )

        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, device_id: int | None = None) -> ModbusPDU | None:
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        sync_client: ModbusBaseSyncClient = self.client
        resolved_device_id: int = self._get_device_id(device_id)
        result: ModbusPDU | None = None
        # no need for a lock here since the client handles its own internal locking and
        # we don't have any shared state to protect in this method.  If we were to add retries or other
        # logic that re-enters this method, we would need to add a lock to prevent concurrent access to the client.
        try:
            if registry_type == Registry_Type.INPUT:
                result = sync_client.read_input_registers(start, count=count, device_id=resolved_device_id)
            elif registry_type == Registry_Type.HOLDING:
                result = sync_client.read_holding_registers(start, count=count, device_id=resolved_device_id)
            elif registry_type == Registry_Type.COIL:
                result = sync_client.read_coils(start, count=count, device_id=resolved_device_id)
            elif registry_type == Registry_Type.DISCRETE:
                result = sync_client.read_discrete_inputs(start, count=count, device_id=resolved_device_id)
            else:
                self._log.warning(f"read_registers: unsupported registry_type '{registry_type.name}' for TLS transport — returning None")
                return None

        except ConnectionException:
            self._log.error(f"Connection lost to {self.transport_name} at {self.host}:{self.port}")
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

        if isinstance(result, ExceptionResponse):
            self._log.error("Modbus Error: Result is None")
            return None

        # Use hasattr to safely check for the error attribute/method
        is_error: bool = result.isError() if hasattr(result, "isError") else bool(getattr(result, "is_error", False))

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
                self._log.debug(f"Failed to close TLS connection to {self.host}:{self.port}")

            # pymodbus connect() usually returns True/False
            self.connected = bool(self.client.connect())
            if self.connected:
                self._log.info(f"Modbus TLS connected: {self.connected} for {self.transport_name}")
                super().connect()
            else:
                self._log.error(f"Failed to establish TLS connection to {self.host}:{self.port}")
        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False

        return self.connected
