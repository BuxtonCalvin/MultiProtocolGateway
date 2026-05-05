# bridge transport module for MQTT, implementing a publish-subscribe mechanism
# to relay data between the protocol scrapers and an MQTT broker, with support for Home Assistant discovery.
import atexit
import csv
import json
import random
import time
import warnings
from pathlib import Path

import paho.mqtt.packettypes
import paho.mqtt.properties
from paho.mqtt.client import MQTT_ERR_NO_CONN, MQTT_ERR_SUCCESS
from paho.mqtt.client import Client as MQTTClient
from paho.mqtt.enums import CallbackAPIVersion

from defs.common import TransportSettings, strtobool

from ..protocol_settings import Registry_Type, WriteMode, registry_map_entry
from .transport_base import transport_base


class mqtt(transport_base):
    ''' for future; this will hold mqtt transport'''
    host : str
    port : int = 1883
    base_topic : str = "home/device"
    error_topic : str = "/error"
    discovery_topic : str = "homeassistant"
    discovery_enabled : bool = False
    json : bool = False
    reconnect_delay : int = 7
    """ seconds """

    reconnect_attempts : int = 21
    #max_precision : int = - 1

    holding_register_prefix : str = ""
    input_register_prefix : str = ""

    client : MQTTClient | None = None
    mqtt_properties : paho.mqtt.properties.Properties | None = None

    __first_connection : bool = True
    __reconnecting : float = 0.0
    connected : bool = False

    def __init__(self, settings :TransportSettings) -> None:
        self.host = settings.get("host", fallback="")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=self.port)
        self.base_topic = settings.get("base_topic", fallback=self.base_topic).rstrip("/")
        self.error_topic = settings.get("error_topic", fallback=self.error_topic).rstrip("/")
        self.discovery_topic = settings.get("discovery_topic", fallback=self.discovery_topic)
        self.discovery_enabled = strtobool(settings.get("discovery_enabled", self.discovery_enabled))
        self.json = strtobool(settings.get("json", self.json))
        self.reconnect_delay = settings.getint("reconnect_delay", fallback=7)
        #self.max_precision = settings.getint('max_precision', fallback=self.max_precision)

        if not isinstance( self.reconnect_delay , int) or self.reconnect_delay < 1: #minimum 1 second
            self.reconnect_delay = 1

        self.reconnect_attempts = settings.getint("reconnect_attempts", fallback=21)
        if not isinstance( self.reconnect_attempts , int) or self.reconnect_attempts < 0: #minimum 0
            self.reconnect_attempts = 0

        self.holding_register_prefix = settings.get("holding_register_prefix", fallback="")
        self.input_register_prefix = settings.get("input_register_prefix", fallback="")

        username: str = settings.get("username", fallback="")
        password: str = settings.get("password", fallback="")

        if not username:
            warnings.warn("MQTT Username is empty", RuntimeWarning)


        if not password:
            warnings.warn("MQTT Password is empty", RuntimeWarning)

        try:
            self.client = MQTTClient(CallbackAPIVersion.VERSION2)
        except ImportError:
            # Fallback for paho-mqtt < 2.0.0
            self.client = MQTTClient()

        if username:
            self.client.username_pw_set(username=username, password=password)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.client_on_message
        self.client.on_disconnect = self.on_disconnect

        self.mqtt_properties = paho.mqtt.properties.Properties(paho.mqtt.packettypes.PacketTypes.PUBLISH)
        self.mqtt_properties.MessageExpiryInterval = 30  # in seconds

        super().__init__(settings)


    def connect(self) -> None:
        self._log.info("mqtt connect")
        if self.__first_connection:
            self.__first_connection = False
            if self.client is not None:
                self.client.connect(str(self.host), int(self.port), 60)
                self.client.loop_start()
                atexit.register(self.exit_handler)
                self._log.info("MQTT Client initialized and connection loop started.")
        else:
            self.mqtt_reconnect() #special reconnect function

    def exit_handler(self) -> None:
        '''on exit handler'''
        self._log.warning("MQTT Exiting...")
        if self.client is not None:
            self.client.publish( self.base_topic + "/" + self.device_identifier + "/availability","offline")
        return

    def mqtt_reconnect(self) -> None:
        # This function implements an exponential backoff strategy for reconnecting to the MQTT broker,
        # with added jitter to prevent thundering herd issues.  Can't quit here because other transports may still be working,
        # and we want to keep trying to reconnect in the background.

        self._log.info("Disconnected from MQTT Broker!")

        if self.__reconnecting != 0:
            return

        # Configuration for backoff
        base_delay = self.reconnect_delay      # Start with user configured delay
        max_delay = 600      # Max wait time (10 minutes)
        attempt = 0
        current_wait = 0

        while not self.connected:
            self.__reconnecting = time.time()
            attempt += 1

            try:
                # Calculate exponential backoff: (base * 2^attempt) + random jitter
                delay: int = min(max_delay, base_delay * (2 ** (attempt - 1)))
                jitter: float = random.uniform(0, 1)  # noqa: S311
                current_wait = delay + jitter

                self._log.warning(f"Attempting to reconnect ({attempt})...")

                if self.client is not None:
                    # Toggle connection methods
                    if random.randint(0, 1):  # noqa: S311
                        self.client.reconnect()
                    else:
                        self.client.loop_stop()
                        self.client.connect(str(self.host), int(self.port), 60)
                        self.client.loop_start()

                # Brief sleep to let the background loop process the connection
                time.sleep(2)

                if self.connected:
                    self._log.info("Successfully reconnected!")
                    self.__reconnecting = 0
                    return

            except Exception as e:
                self._log.error(f"Connection error: {e}")
                self._log.error(f"❌ [COMMUNICATION LOST] --- Host: {self.host} ---")

            # If not connected, wait with backoff
            self._log.warning(f"Waiting {current_wait:.2f}s before next retry...")
            time.sleep(current_wait)

    def on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        """ The callback for when the client receives a CONNACK response from the server. """
        self._log.info("Connected with result code %s\n", str(reason_code))
        self.connected = True

    __write_topics : dict[str, registry_map_entry] = {}

    def _load_override_write_allowlist(self, from_transport: transport_base) -> set[str]:
        """
        Load documented-name allowlist from the protocol's holding override CSV.
        If the file is missing/empty, return an empty set (no write topics allowed).
        """
        if from_transport.protocolSettings is None:
            return set()

        protocol_name: str = from_transport.protocolSettings.protocol
        override_file: str = f"{protocol_name}.holding_registry_map.override.csv"
        override_path: str | None = from_transport.protocolSettings.find_protocol_file(
            override_file, from_transport.protocolSettings.settings_dir
        )
        if not override_path:
            return set()

        allowlist: set[str] = set()
        try:
            with open(Path(override_path), newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name: str  = (row.get("documented name") or "").strip().lower().replace(" ", "_")
                    if name:
                        allowlist.add(name)
        except Exception as exc:
            self._log.warning(f"Unable to read override write allowlist '{override_path}': {exc}")
            return set()

        return allowlist

    def write_data(self, data: dict[str, int | float | str ], from_transport : transport_base) -> None:
        # Note: write_enabled is intentionally NOT checked here.
        # For bridge transports like MQTT, write_enabled has no meaning for the write_data method—
        # this method publishes scraper READ data to the broker, not commands to hardware.
        # Hardware write-back gating belongs in modbus_base.write_data and
        # modbus_tcp.write_register, where it guards FC06 Modbus write calls.
        # so we use the property in init_bridge instead where we use register overloads to describe
        # which registers are allowed to be written to.  Overloads can be easily created in the webserver UI.
        # if not self.write_enabled:
        #     return
        if self.client is not None:
            if self.connected:
                self.connected = self.client.is_connected()

            self._log.info(f"write data from [{from_transport.transport_name}] to mqtt transport")
            self._log.info(data)
            #have to send this every loop, because mqtt doesn't disconnect when HA restarts. HA bug.

            info = self.client.publish(f"{self.base_topic}/{from_transport.device_identifier}/availability","online",qos=0,retain=True)
            if info.rc != MQTT_ERR_SUCCESS:
                self.connected = False
                if info.rc == MQTT_ERR_NO_CONN:
                    self._log.error("MQTT Publish failed: No connection to broker.")

            if(self.json):
                # Serializing json
                json_object: str = json.dumps(data, indent=4)
                self.client.publish(self.base_topic+"/"+from_transport.device_identifier, json_object, 0, properties=self.mqtt_properties)
            else:
                for entry, val in data.items():
                    if isinstance(val, float) and self.max_precision >= 0: #apply max_precision on mqtt transport
                        val = round(val, self.max_precision)

                    self.client.publish(str(self.base_topic+"/"+from_transport.device_identifier+"/"+entry).lower(), str(val))

    def client_on_message(self, client, userdata, msg) -> None:
        """ The callback for when a PUBLISH message is received from the server. """
        self._log.info("MQTT MSG: " + msg.topic+" "+str(msg.payload.decode("utf-8")))

        #self.protocolSettings.validate_registry_entry
        if msg.topic in self.__write_topics:
            entry: registry_map_entry = self.__write_topics[msg.topic]

            self._emit_message(entry, msg.payload.decode("utf-8"))
            #self.write_variable(entry, value=str(msg.payload.decode('utf-8')))

    def init_bridge(self, from_transport : transport_base) -> None:

        if self.client is not None and from_transport.protocolSettings is not None:
            if from_transport.write_enabled:
                self.__write_topics = {}
                write_allowlist = self._load_override_write_allowlist(from_transport)
                if not write_allowlist:
                    self._log.info(
                        "No holding override allowlist found for '%s'; MQTT write topics disabled until write selections are committed.",
                        from_transport.transport_name,
                    )
                #subscribe to write topics
                for entry in from_transport.protocolSettings.get_registry_map(Registry_Type.HOLDING):
                    is_protocol_writable = entry.write_mode == WriteMode.WRITE or entry.write_mode == WriteMode.WRITEONLY
                    entry_name: str = entry.documented_name.strip().lower().replace(" ", "_")
                    if is_protocol_writable and entry_name in write_allowlist:
                        #__write_topics
                        topic : str = self.base_topic + "/"+ from_transport.device_identifier + "/write/" + entry.variable_name.lower().replace(" ", "_")
                        self.__write_topics[topic] = entry
                        self.client.subscribe(topic)
                self._log.info(
                    "MQTT write topic allowlist for '%s': %d topic(s)",
                    from_transport.transport_name,
                    len(self.__write_topics),
                )

            if self.discovery_enabled:
                self.mqtt_discovery(from_transport)

    def mqtt_discovery(self, from_transport : transport_base) -> None:
        self._log.info("Publishing HA Discovery Topics...")

        disc_payload = {}
        disc_payload["availability_topic"] = self.base_topic + "/" + from_transport.device_identifier + "/availability"

        device = {}
        device["manufacturer"] = from_transport.device_manufacturer
        device["model"] = from_transport.device_model
        device["identifiers"] = "MPG_" + from_transport.device_model + "_" + from_transport.device_serial_number
        device["name"] = from_transport.device_name

        registry_map : list[registry_map_entry] = []
        if from_transport.protocolSettings is not None:
            for entries in from_transport.protocolSettings.registry_map.values():
                registry_map.extend(entries)

        length: int = len(registry_map)
        count = 0
        if self.client is not None:
            for item in registry_map:
                count = count + 1

                if item.concatenate and item.register != item.concatenate_registers[0]:
                    continue #skip all except the first register so no duplicates

                if item.write_mode == WriteMode.READDISABLED: #disabled
                    continue


                clean_name = item.variable_name.lower().replace(" ", "_").strip()
                if not clean_name: #if name is empty, skip
                    continue

                if False:
                    if self.__input_register_prefix and item.registry_type == Registry_Type.INPUT:
                        clean_name = self.__input_register_prefix + clean_name

                    if self.__holding_register_prefix and item.registry_type == Registry_Type.HOLDING:
                        clean_name = self.__holding_register_prefix + clean_name


                #print(("#Publishing Topic "+str(count)+" of " + str(length) + ' "'+str(clean_name)+'"').ljust(100)+"#", end="\r", flush=True)
                self._log.debug("#Publishing Topic "+str(count)+" of " + str(length) + ' "'+ str(clean_name)+'"')


                #device['sw_version'] = bms_version
                disc_payload = {}
                disc_payload["availability_topic"] = self.base_topic + "/" + from_transport.device_identifier + "/availability"
                disc_payload["device"] = device
                disc_payload["name"] = clean_name
                disc_payload["unique_id"] = "MPG_" + from_transport.device_serial_number + "_"+clean_name

                writePrefix = ""
                if from_transport.write_enabled and ( item.write_mode == WriteMode.WRITE or item.write_mode == WriteMode.WRITEONLY ):
                    writePrefix = "" #home assistant doesn't like write prefix

                disc_payload["state_topic"] = self.base_topic + "/" +from_transport.device_identifier + writePrefix+ "/"+clean_name

                if item.unit:
                    disc_payload["unit_of_measurement"] = item.unit

                discovery_topic: str = self.discovery_topic+"/sensor/HN-" + from_transport.device_serial_number  + writePrefix + "/" + disc_payload["name"].replace(" ", "_") + "/config"

                self.client.publish(discovery_topic,
                                        json.dumps(disc_payload),qos=1, retain=True)

                #send WO message to indicate topic is write only
                if item.write_mode == WriteMode.WRITEONLY:
                    self.client.publish(disc_payload["state_topic"], "WRITEONLY")

                time.sleep(0.07) #slow down for better reliability

            self.client.publish(disc_payload["availability_topic"],"online",qos=0, retain=True)
            print()
            self._log.info("Published HA "+str(count)+"x Discovery Topics")
