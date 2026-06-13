# Description: bridge transport module for MQTT, implementing a publish-subscribe mechanism to relay data between the protocol scrapers and an MQTT broker, with support for Home Assistant discovery.
# File: mqtt.py
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

# bridge transport module for MQTT, implementing a publish-subscribe mechanism
# to relay data between the protocol scrapers and an MQTT broker, with support for Home Assistant discovery.
import atexit
import csv
import json
import random
import threading
import time
import warnings
from pathlib import Path

import paho.mqtt.packettypes
import paho.mqtt.properties
from paho.mqtt.client import MQTT_ERR_NO_CONN, MQTT_ERR_SUCCESS, MQTTMessageInfo
from paho.mqtt.client import Client as MQTTClient
from paho.mqtt.enums import CallbackAPIVersion

from defs.common import TransportSettings, strtobool

from ..protocol_settings import Registry_Type, WriteMode, registry_map_entry
from .transport_base import transport_base


class mqtt(transport_base):

    transport_type = "bridge"
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

    holding_register_prefix : str = ""
    input_register_prefix : str = ""

    client : MQTTClient | None = None
    mqtt_properties : paho.mqtt.properties.Properties | None = None

    def __init__(self, settings: TransportSettings) -> None:
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

        if not isinstance(self.reconnect_delay, int) or self.reconnect_delay < 1:  # minimum 1 second
            self.reconnect_delay = 1

        self.reconnect_attempts = settings.getint("reconnect_attempts", fallback=21)
        if not isinstance(self.reconnect_attempts, int) or self.reconnect_attempts < 0:  # minimum 0
            self.reconnect_attempts = 0

        self.holding_register_prefix = settings.get("holding_register_prefix", fallback="")
        self.input_register_prefix = settings.get("input_register_prefix", fallback="")

        # Instance-level state — never class-level to avoid shared-dict bugs across instances
        self._first_connection: bool = True
        self._reconnect_thread: threading.Thread | None = None
        self._write_topics: dict[str, registry_map_entry] = {}

        username: str = settings.get("username", fallback="")
        password: str = settings.get("password", fallback="")

        if not username:
            warnings.warn("MQTT Username is empty", RuntimeWarning)

        if not password:
            warnings.warn("MQTT Password is empty", RuntimeWarning)

        self.client = MQTTClient(CallbackAPIVersion.VERSION2)

        if username:
            self.client.username_pw_set(username=username, password=password)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.client_on_message
        self.client.on_disconnect = self.on_disconnect

        self.mqtt_properties = paho.mqtt.properties.Properties(paho.mqtt.packettypes.PacketTypes.PUBLISH)
        self.mqtt_properties.MessageExpiryInterval = 30  # in seconds

        super().__init__(settings)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._log.info("mqtt connect")
        if self._first_connection:
            self._first_connection = False
            if self.client is not None:
                self.client.connect(str(self.host), int(self.port), 60)
                self.client.loop_start()
                atexit.register(self.exit_handler)
                self._log.info("MQTT Client initialized and connection loop started.")
        else:
            self._start_reconnect_thread()

    def _start_reconnect_thread(self) -> None:
        """Spawn a background reconnect thread if one is not already running."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            self._log.debug("Reconnect thread already running — skipping duplicate spawn.")
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name=f"mqtt-reconnect-{self.transport_name}",
            daemon=True,
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """Background thread: exponential backoff reconnect until connected or exhausted.

        Uses client.reconnect() exclusively — the correct paho call after a drop.
        Resets cleanly on both success and exhaustion so future disconnects can
        spawn a fresh thread.
        """
        self._log.info("Disconnected from MQTT Broker — starting background reconnect.")

        base_delay = self.reconnect_delay
        max_delay = 600  # 10 minutes
        attempt = 0

        try:
            while not self.connected:
                attempt += 1
                delay: int = min(max_delay, base_delay * (2 ** (attempt - 1)))
                jitter: float = random.uniform(0, 1)  # noqa: S311
                current_wait: float = delay + jitter

                self._log.warning(f"Reconnect attempt {attempt} — waiting {current_wait:.2f}s...")
                time.sleep(current_wait)

                try:
                    if self.client is not None:
                        self.client.reconnect()

                    # Give the paho background loop time to process the CONNACK
                    time.sleep(2)

                    if self.connected:
                        self._log.info("Successfully reconnected!")
                        return

                except Exception as exc:
                    self._log.error(f"Reconnect attempt {attempt} failed: {exc}")
                    self._log.error(f"❌ [COMMUNICATION LOST] --- Host: {self.host} ---")

                if self.reconnect_attempts > 0 and attempt >= self.reconnect_attempts:
                    self._log.error(
                        f"Exhausted {self.reconnect_attempts} reconnect attempts — giving up. "
                        "A future disconnect will retry."
                    )
                    return
        finally:
            # Always clear the thread reference so a future on_disconnect can
            # spawn a fresh one, regardless of whether we succeeded or gave up.
            self._reconnect_thread = None

    def exit_handler(self) -> None:
        """Publish offline availability and cleanly shut down the paho loop on exit."""
        self._log.warning("MQTT Exiting...")
        if self.client is not None:
            self.client.publish(
                self.base_topic + "/" + self.device_identifier + "/availability",
                "offline",
            )
            # Give the final publish a moment to flush before the loop stops
            time.sleep(0.5)
            self.client.loop_stop()
            self.client.disconnect()

    # ------------------------------------------------------------------
    # Paho callbacks
    # ------------------------------------------------------------------

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self.connected = False
        self._start_reconnect_thread()

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        """Called when the client receives a CONNACK response from the server."""
        self._log.info("Connected with result code %s", str(reason_code))
        self.connected = True
        # Re-subscribe to all write topics so they survive a reconnect
        self._resubscribe_write_topics()

    # ------------------------------------------------------------------
    # Write-topic helpers
    # ------------------------------------------------------------------

    def _resubscribe_write_topics(self) -> None:
        """Re-subscribe to all registered write topics after a (re)connect.

        Paho does not automatically re-subscribe on reconnect when clean_session
        is True (the default), so we do it explicitly here from on_connect.
        """
        if not self._write_topics or self.client is None:
            return
        for topic in self._write_topics:
            self.client.subscribe(topic)
        self._log.info("Re-subscribed to %d write topic(s).", len(self._write_topics))

    def _load_override_write_allowlist(self, from_transport: transport_base) -> set[str]:
        """Load documented-name allowlist from the protocol's holding override CSV.

        If the file is missing/empty, return an empty set (no write topics allowed).
        """
        if from_transport.protocolSettings is None:
            return set()

        protocol_name: str = from_transport.protocolSettings.protocol
        override_file: str = f"{protocol_name}.holding_registry_map.override.csv"
        override_path: str | None = from_transport.protocolSettings.find_protocol_file(
            override_file, "config"
        )
        if not override_path:
            self._log.warning(
                "No override write allowlist found for '%s'; MQTT write topics disabled until write selections are made.",
                protocol_name,
            )
            return set()

        allowlist: set[str] = set()
        try:
            with open(Path(override_path), newline="", encoding="utf-8") as f:
                reader: csv.DictReader[str] = csv.DictReader(f)
                for row in reader:
                    name: str = (row.get("documented name") or "").strip().lower().replace(" ", "_")
                    if name:
                        allowlist.add(name)
        except Exception as exc:
            self._log.warning(f"Unable to read override write allowlist '{override_path}': {exc}")
            return set()
        self._log.info(f"Loaded {len(allowlist)} entries from override write allowlist '{override_path}'")
        return allowlist

    # ------------------------------------------------------------------
    # Data publishing
    # ------------------------------------------------------------------

    def write_data(self, data: dict[str, int | float | str], from_transport: transport_base) -> None:
        # Note: write_enabled is intentionally NOT checked here.
        # For bridge transports like MQTT, write_enabled has no meaning for the write_data method —
        # this method publishes scraper READ data to the broker, not commands to hardware.
        # Hardware write-back gating belongs in modbus_base.write_data and
        # modbus_tcp.write_register, where it guards FC06 Modbus write calls.
        # We use the property in init_bridge instead, where register overloads describe
        # which registers are allowed to be written to.
        if self.client is None:
            return

        # Sync connected state unconditionally so a background reconnect that
        # succeeded is reflected immediately, and a stale True is corrected.
        self.connected = self.client.is_connected()

        self._log.info(f"write data from [{from_transport.transport_name}] to mqtt transport {data}")
        # Publish availability every loop — required because HA doesn't disconnect
        # cleanly on restart (HA bug), so we can't rely on LWT alone.
        info: MQTTMessageInfo = self.client.publish(
            f"{self.base_topic}/{from_transport.device_identifier}/availability",
            "online",
            qos=0,
            retain=True,
        )
        if info.rc != MQTT_ERR_SUCCESS:
            self.connected = False
            if info.rc == MQTT_ERR_NO_CONN:
                self._log.error("MQTT Publish failed: No connection to broker.")
            return

        if self.json:
            json_object: str = json.dumps(data, indent=4)
            self.client.publish(
                self.base_topic + "/" + from_transport.device_identifier,
                json_object,
                0,
                properties=self.mqtt_properties,
            )
        else:
            for entry, val in data.items():
                if isinstance(val, float) and self.max_precision >= 0:
                    val = round(val, self.max_precision)
                self.client.publish(
                    str(self.base_topic + "/" + from_transport.device_identifier + "/" + entry).lower(),
                    str(val),
                )

    def client_on_message(self, client, userdata, msg) -> None:
        """Callback for PUBLISH messages received from the broker."""
        self._log.info("MQTT MSG: " + msg.topic + " " + str(msg.payload.decode("utf-8")))

        if msg.topic in self._write_topics:
            entry: registry_map_entry = self._write_topics[msg.topic]
            self._emit_message(entry, msg.payload.decode("utf-8"))

    # ------------------------------------------------------------------
    # Bridge initialization
    # ------------------------------------------------------------------

    def init_bridge(self, from_transport: transport_base) -> None:
        if self.client is None or from_transport.protocolSettings is None:
            return

        if from_transport.write_enabled:
            # Reset per-transport so a second call (e.g. after reconnect) is clean
            self._write_topics = {}
            write_allowlist: set[str] = self._load_override_write_allowlist(from_transport)
            if not write_allowlist:
                self._log.info(
                    "No holding override allowlist found for '%s'; MQTT write topics disabled until write selections are committed.",
                    from_transport.transport_name,
                )

            # Subscribe to holding-register write topics
            for entry in from_transport.protocolSettings.get_registry_map(Registry_Type.HOLDING):
                is_protocol_writable: bool = (
                    entry.write_mode == WriteMode.WRITE or entry.write_mode == WriteMode.WRITEONLY
                )
                entry_name: str = entry.documented_name.strip().lower().replace(" ", "_")
                if is_protocol_writable and entry_name in write_allowlist:
                    topic: str = (
                        self.base_topic
                        + "/" + from_transport.device_identifier
                        + "/write/"
                        + entry.variable_name.lower().replace(" ", "_")
                    )
                    self._write_topics[topic] = entry
                    self.client.subscribe(topic)

            # Subscribe to coil-register write topics
            for entry in from_transport.protocolSettings.get_registry_map(Registry_Type.COIL):
                is_protocol_writable = (
                    entry.write_mode == WriteMode.WRITE or entry.write_mode == WriteMode.WRITEONLY
                )
                entry_name = entry.documented_name.strip().lower().replace(" ", "_")
                if is_protocol_writable and entry_name in write_allowlist:
                    topic = (
                        self.base_topic
                        + "/" + from_transport.device_identifier
                        + "/write/"
                        + entry.variable_name.lower().replace(" ", "_")
                    )
                    self._write_topics[topic] = entry
                    self.client.subscribe(topic)

            self._log.info(
                "MQTT write topic allowlist for '%s': %d topic(s)",
                from_transport.transport_name,
                len(self._write_topics),
            )

        if self.discovery_enabled:
            self.mqtt_discovery(from_transport)

    # ------------------------------------------------------------------
    # Home Assistant discovery
    # ------------------------------------------------------------------

    def mqtt_discovery(self, from_transport: transport_base) -> None:
        self._log.info("Publishing HA Discovery Topics...")

        availability_topic: str = (
            self.base_topic + "/" + from_transport.device_identifier + "/availability"
        )

        device: dict = {
            "manufacturer": from_transport.device_manufacturer,
            "model": from_transport.device_model,
            "identifiers": "MPG_" + from_transport.device_model + "_" + from_transport.device_serial_number,
            "name": from_transport.device_name,
        }

        registry_map: list[registry_map_entry] = []
        if from_transport.protocolSettings is not None:
            for entries in from_transport.protocolSettings.registry_map.values():
                registry_map.extend(entries)

        length: int = len(registry_map)
        count: int = 0

        if self.client is None:
            return

        published_availability: bool = False

        for item in registry_map:
            count += 1

            if item.concatenate and item.register != item.concatenate_registers[0]:
                continue  # skip all except the first register to avoid duplicates

            if item.write_mode == WriteMode.READDISABLED:
                continue

            clean_name: str = item.variable_name.lower().replace(" ", "_").strip()
            if not clean_name:
                continue

            self._log.debug(f"#Publishing Topic {count} of {length} \"{clean_name}\"")

            writePrefix = ""
            if from_transport.write_enabled and (
                item.write_mode == WriteMode.WRITE or item.write_mode == WriteMode.WRITEONLY
            ):
                writePrefix = ""  # Home Assistant doesn't like write prefix

            disc_payload: dict = {
                "availability_topic": availability_topic,
                "device": device,
                "name": clean_name,
                "unique_id": "MPG_" + from_transport.device_serial_number + "_" + clean_name,
                "state_topic": (
                    self.base_topic + "/" + from_transport.device_identifier + writePrefix + "/" + clean_name
                ),
            }

            if item.unit:
                disc_payload["unit_of_measurement"] = item.unit

            discovery_topic: str = (
                self.discovery_topic
                + "/sensor/HN-" + from_transport.device_serial_number
                + writePrefix + "/"
                + disc_payload["name"].replace(" ", "_")
                + "/config"
            )

            self.client.publish(discovery_topic, json.dumps(disc_payload), qos=1, retain=True)

            # Send WO message to indicate topic is write-only
            if item.write_mode == WriteMode.WRITEONLY:
                self.client.publish(disc_payload["state_topic"], "WRITEONLY")

            published_availability = True
            time.sleep(0.07)  # deliberate throttle for broker reliability on large maps

        # Only publish availability if at least one topic was processed,
        # guarding against KeyError on an empty registry map
        if published_availability:
            self.client.publish(availability_topic, "online", qos=0, retain=True)

        self._log.info(f"Published HA {count}x Discovery Topics")
