# scraper for Modbus UDP devices, inheriting from modbus_base and implementing UDP-specific client setup and register access logic.
from threading import Lock
from typing import TYPE_CHECKING, cast

from pymodbus.client.base import ModbusBaseClient
from pymodbus.client.udp import ModbusUdpClient

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base

if TYPE_CHECKING:
    from threading import Lock

class modbus_udp(modbus_base):

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        self.host = settings.get("host", fallback="")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=502)

        client_str = self.host+"-udp-"+str(self.port)
        #check if client is already initialized
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return
        self.timeout: int = settings.getint("timeout", fallback=7)
        self.retries: int = settings.getint("retries", fallback=3)
        self.client = cast(ModbusBaseClient, ModbusUdpClient(host=self.host, port=self.port, timeout=self.timeout, retries=self.retries))

        #add to clients
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start, count=1, registry_type: Registry_Type = Registry_Type.INPUT,  **kwargs):
        # read_registers method to handle retries and prevent "fire and forget" failures
        if self.client is None:
            self._log.error("read_registers called before client was initialized")
            return None

        kwargs = self._get_correct_device_arg(kwargs)
        port_lock: Lock = self._get_port_lock()

        with port_lock:
        # Try the operation up to 'retries' times
            for attempt in range(self.retries):
                try:
                    if registry_type == Registry_Type.INPUT:
                        response = self.client.read_input_registers(start, count=count, **kwargs)
                    elif registry_type == Registry_Type.HOLDING:
                        response = self.client.read_holding_registers(start, count=count, **kwargs)
                    response = None
                    # Check if we actually received a valid response packet back
                    # Pymodbus returns an Exception object (not raises it) on failure
                    if response is not None and not response.isError():
                        return response

                    self._log.warning(f"Modbus UDP Attempt {attempt + 1} failed: {response}")

                except Exception as e:
                    self._log.error(f"Network error on attempt {attempt + 1}: {e}")

            # If the loop finishes without returning, all retries failed
            self._log.error(f"Failed to read {registry_type} after {self.retries} attempts.")
            return None

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

    def connect(self) -> None:
        if self.client is None:
            self._log.error(f"Cannot connect '{self.transport_name}' — client not initialized")
            self.connected = False
            return
        self.connected = bool(self.client.connect())
        self._log.info(f"Modbus udp connected: {self.connected} for {self.transport_name} on port {self.port}")
        super().connect()
