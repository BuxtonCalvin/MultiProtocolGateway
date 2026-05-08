# scraper for Modbus RTU devices over RS-232/RS-485 serial, inheriting from modbus_base and implementing
# RTU-specific client setup and register access logic.

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

    def write_register(self, register : int, value : int, **kwargs):
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_register called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)

        # Use port-specific lock for thread-safe access
        port_lock: Lock = self._get_port_lock()
        with port_lock:
            self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register

    def connect(self) -> bool:
        if self.client is None:
            self._log.error(f"Cannot connect '{self.transport_name}' — client not initialized")
            self.connected = False
            return False

        try:
            # pymodbus connect() usually returns True/False
            self.connected = bool(self.client.connect())

            if self.connected:
                self._log.info(f"Modbus RTU connected: {self.connected} for {self.transport_name} on port {self.port}")
                super().connect()
            else:
                self._log.error(f"Failed to establish RTU connection to {self.port}")

            return self.connected  # noqa: TRY300
        except Exception as e:
            self._log.error(f"Exception during connection: {e}")
            self.connected = False
            return False




