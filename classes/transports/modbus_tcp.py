import inspect
from configparser import SectionProxy

from pymodbus.client import ModbusTcpClient

from classes.protocol_settings import Registry_Type, protocol_settings

from .modbus_base import modbus_base


class modbus_tcp(modbus_base):
    port : str = 502
    host : str = ""
    client : ModbusTcpClient
    pymodbus_slave_arg: str = "unit"  # default legacy device arg

    def __init__(self, settings : SectionProxy, protocolSettings : protocol_settings = None):
        super().__init__(settings, protocolSettings=protocolSettings)

        self.host = settings.get("host", "")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", self.port)

        client_str = self.host+"-tcp-"+str(self.port)
        #check if client is already initialized
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                super().__init__(settings, protocolSettings=protocolSettings)
                return

        self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=7, retries=3)

        #add to clients (thread-safe)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client


    def write_register(self, register : int, value : int, **kwargs):
        if not self.write_enabled:
            return
        kwargs = self._get_correct_device_arg(kwargs)

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register

    def read_registers(self, start, count=1, registry_type : Registry_Type = Registry_Type.INPUT, **kwargs):

        kwargs = self._get_correct_device_arg(kwargs)

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            if registry_type == Registry_Type.INPUT:
                return self.client.read_input_registers(start,count=count, **kwargs  )
            elif registry_type == Registry_Type.HOLDING:
                return self.client.read_holding_registers(start,count=count, **kwargs)

    def connect(self):
        self.connected = self.client.connect()
        super().connect()
