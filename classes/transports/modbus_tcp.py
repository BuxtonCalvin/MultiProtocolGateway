# scraper for Modbus TCP devices, inheriting from modbus_base and implementing TCP-specific client setup and register access logic.
from typing import Any, cast

from packaging import version
from pymodbus import __version__ as pymodbus_version
from pymodbus.client import ModbusTcpClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base


class modbus_tcp(modbus_base):
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

    def write_register(self, register: int, value: int, **kwargs) -> None:
        if not self.write_enabled:  # guard for checking inverter scraper flag to allow write back to the inverter.
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

        # This only runs if the try block succeeded without exceptions
        if result is None or result.isError():
            self._log.error(f"Modbus Error: {result}")
            return None

        return result

    def connect(self) -> bool: # Changed return type to bool
        if self.client is None:
            self._log.error(f"Cannot connect {self.transport_name} — client not initialized")
            self.connected = False
            return False

        try:
            # pymodbus connect() usually returns True/False
            self.connected = cast(bool, self.client.connect())

            if self.connected:
                self._log.info(f"Modbus TCP connected: {self.connected} for {self.transport_name}")
                super().connect()
            else:
                self._log.error(f"Failed to establish TCP connection to {self.host}:{self.port}")

            return self.connected  # noqa: TRY300
        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False
            return False
