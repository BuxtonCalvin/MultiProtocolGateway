# scraper for Modbus RTU devices over RS-232/RS-485 serial, inheriting from modbus_base and implementing
# RTU-specific client setup and register access logic.
import inspect
from typing import Any, cast

from pymodbus.client import ModbusSerialClient
from pymodbus.client.base import ModbusBaseClient

from classes.protocol_settings import Registry_Type
from defs.common import (
    TransportSettings,
    find_usb_serial_port,
    get_usb_serial_port_info,
    strtoint_safe,
)

from .modbus_base import modbus_base


class modbus_rtu(modbus_base):
    addresses : list[int] = []
    client = cast(ModbusBaseClient, ModbusSerialClient)
    pymodbus_slave_arg: str = "device_id"

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        self.port = settings.get("port", fallback="/dev/ttyUSB0")
        if not self.port:
            raise ValueError("Port is not set")

        self.port = find_usb_serial_port(self.port)
        if not self.port:
            raise ValueError("Port is not valid / not found")

        print("Serial Port : " + self.port + " = ", get_usb_serial_port_info(self.port)) #print for config convenience

        if "baud" in self._protocol.settings:
            self.baudrate = strtoint_safe(self._protocol.settings["baud"])
        #todo better baud/baudrate alias handling
        self.baudrate = settings.getint("baudrate", fallback = 9600)

        address : int = settings.getint("address", 0)
        self.addresses = [address]
        inspection_params = inspect.signature(ModbusSerialClient.read_holding_registers).parameters

        # pymodbus compatibility; unit was renamed to slave, and then to device_id
        if "slave" in inspection_params:
            self.pymodbus_slave_arg = "slave"
        elif "unit" in inspection_params:
            self.pymodbus_slave_arg = "unit"
        elif "device_id" in inspection_params:
            self.pymodbus_slave_arg = "device_id"
        else:
            self._log.debug(f"Unknown Pymodbus version: {inspection_params}")

        client_str: str = self.port+"("+str(self.baudrate)+")"
        # Thread-safe client access
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        self._log.debug(f"Creating new client with baud rate: {self.baudrate}")

        # Get the signature of the __init__ method
        init_signature = inspect.signature(ModbusSerialClient.__init__)

        client_args = {
            "port": self.port,
            "baudrate": int(self.baudrate),
            "stopbits": 1,
            "parity": "N",
            "bytesize": 8,
            "timeout": 2
        }

        # Legacy support (for very old pymodbus versions) and should really be removed.
        # Add legacy 'method' only if the current version supports it
        if "method" in init_signature.parameters:
            client_args["method"] = "rtu"

        # Unpacking bypasses the strict signature check for 'method'
        self.client = cast(ModbusBaseClient, ModbusSerialClient(**client_args))

        #add to clients (thread-safe)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client # type: ignore

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        kwargs = self._get_correct_device_arg(kwargs)
        port_lock = self._get_port_lock()

        with port_lock:
            if registry_type == Registry_Type.INPUT:
                return self.client.read_input_registers(start, count=count, **kwargs)
            elif registry_type == Registry_Type.HOLDING:
                return self.client.read_holding_registers(start, count=count, **kwargs)
            else:
                self._log.warning(
                    f"read_registers: unsupported registry_type '{registry_type.name}' for TCP transport — returning None")
                return None

    def write_register(self, register : int, value : int, **kwargs):
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_register called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register

    def connect(self) -> None:
        if self.client is None:
            self._log.error(f"Cannot connect '{self.transport_name}' — client not initialized")
            self.connected = False
            return
        self.connected = cast(bool, self.client.connect())
        self._log.info(f"Modbus rtu connected: {self.connected} for {self.transport_name} on port {self.port}")
        super().connect()





