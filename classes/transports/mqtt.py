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
import contextlib
import csv
import json
import random
import sys
import threading
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.packettypes
from paho.mqtt.client import (
    MQTT_ERR_NO_CONN,
    MQTT_ERR_SUCCESS,
    ConnectFlags,
    DisconnectFlags,
    MQTTMessage,
    MQTTMessageInfo,
)
from paho.mqtt.client import (
    Client as MQTTClient,
)
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from defs.common import TransportSettings, strtobool

from ..ha_metadata import HA_SWITCH_DEVICE_CLASSES, lookup_ha_unit
from ..protocol_settings import Data_Type, Registry_Type, WriteMode, registry_map_entry
from .transport_base import transport_base

if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict


class MqttHealthSnapshot(TypedDict):
    """
    Read-only snapshot of this bridge's live connection/reconnect/write-topic
    state, for the device page's "Bridge Health" panel (see
    mqtt.get_health_snapshot). Deliberately not BridgeHealthSnapshot (shared
    by the InfluxDB bridges) — MQTT publishes are fire-and-forget per metric
    with no local backlog/batch to report, and this panel tracks reconnect
    state and discovery/write-topic counts instead.
    """
    connected: bool
    reconnecting: bool
    reconnect_delay: int | None
    reconnect_attempts: int | None
    write_topic_count: int
    known_device_count: int
    discovery_enabled: bool
    discovery_entity_types: str
    discovery_entity_count: int
    json_mode: bool
    base_topic: str


class HADiscoveryPayload(TypedDict):
    """Home Assistant MQTT discovery config payload for one entity
    (see mqtt.mqtt_discovery) — published as-is via json.dumps().

    Only the keys common to every platform are required; the rest depend on
    which platform (sensor / binary_sensor / number / select / switch) the
    entity is published as."""
    availability: list[dict[str, str]]
    availability_mode: str
    device: dict[str, str]
    name: str
    unique_id: str
    state_topic: NotRequired[str]
    value_template: NotRequired[str]
    unit_of_measurement: NotRequired[str]
    device_class: NotRequired[str]
    state_class: NotRequired[str]
    entity_category: NotRequired[str]
    expire_after: NotRequired[int]
    command_topic: NotRequired[str]
    optimistic: NotRequired[bool]
    min: NotRequired[int | float]
    max: NotRequired[int | float]
    step: NotRequired[int | float]
    mode: NotRequired[str]
    options: NotRequired[list[str]]
    payload_on: NotRequired[str]
    payload_off: NotRequired[str]
    state_on: NotRequired[str]
    state_off: NotRequired[str]


@dataclass
class _DiscoveryPlan:
    """What mqtt_discovery decided to do for one registry entry."""
    component: str | None
    """HA platform actually published (None = publish nothing for this entry)."""
    payload: HADiscoveryPayload | None
    stale_components: tuple[str, ...]
    """Other platforms this entry may have been published under by an earlier
    version/setting; their retained config is cleared so HA does not keep a
    duplicate or orphaned entity."""


# ---------------------------------------------------------------------------
# Home Assistant metadata helpers (pure functions — no broker access)
# ---------------------------------------------------------------------------

_HA_STATE_CLASSES: frozenset[str] = frozenset({"measurement", "total", "total_increasing"})
_HA_ENTITY_CATEGORIES: frozenset[str] = frozenset({"config", "diagnostic"})
_HA_CONTROL_COMPONENTS: frozenset[str] = frozenset({"number", "select", "switch"})

_HA_NUMERIC_DATA_TYPES: frozenset[Data_Type] = frozenset({
    Data_Type.BYTE, Data_Type.USHORT, Data_Type.UINT, Data_Type.SHORT,
    Data_Type.INT, Data_Type.UINT64, Data_Type.FLOAT32, Data_Type.FLOAT64,
    Data_Type.ACC32,
})
_DATA_TYPE_1BIT: int = 201

def is_numeric_entry(entry: registry_map_entry, has_code_map: bool) -> bool:
    """True when the published value is a plain number.

    Enum-mapped, flag, string and ASCII/HEX entries publish text, so they must
    not be given a unit or state_class (HA would reject non-numeric states).
    """
    if has_code_map:
        return False
    return entry.data_type in _HA_NUMERIC_DATA_TYPES or 200 < entry.data_type.value < 500


def _csv_choice(csv_value: str, inferred: str) -> str:
    """Resolve a CSV override: blank = inferred, ``none`` / ``-`` = suppress."""
    value: str = (csv_value or "").strip().lower()
    if not value:
        return inferred
    if value in ("none", "-"):
        return ""
    return value


def _ha_number(value: float) -> int | float:
    """Tidy a float for JSON: drop float noise, and the ``.0`` on whole numbers."""
    rounded: float = round(value, 6)
    return int(rounded) if float(rounded).is_integer() else rounded


def _friendly_name(clean_name: str) -> str:
    """``grid_charge_enable`` -> ``Grid charge enable`` (HA display name only)."""
    text: str = clean_name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def number_bounds(entry: registry_map_entry) -> tuple[int | float, int | float, int | float]:
    """``(min, max, step)`` for a writable numeric entry, in engineering units.

    The CSV ``values`` range is a raw register range, so it is scaled by
    ``unit_mod`` (the write path divides by it again before writing). Home
    Assistant's own defaults (1..100) would be wrong for nearly every register,
    so bounds are always sent explicitly. Narrow the ``values`` column in the
    protocol CSV to tighten the slider/box limits.
    """
    low: float = entry.value_min
    high: float = entry.value_max
    if 200 < entry.data_type.value < 300:  # unsigned N-bit field inside a register
        high = min(high, (1 << Data_Type.getSize(entry.data_type)) - 1)
    scaled: tuple[float, float] = (low * entry.unit_mod, high * entry.unit_mod)
    step: float = max(abs(entry.unit_mod) or 1.0, 0.001)  # HA rejects steps below 0.001
    return _ha_number(min(scaled)), _ha_number(max(scaled)), _ha_number(step)


class mqtt(transport_base):

    transport_type = "bridge"
    ''' for future; this will hold mqtt transport'''
    host : str
    port : int = 1883
    base_topic : str = "home/device"
    error_topic : str = "/error"
    discovery_topic : str = "homeassistant"
    discovery_enabled : bool = False
    discovery_entity_types : str = "auto"
    """ "auto" = native HA platforms (sensor, binary_sensor, number, select, switch);
    "sensor" = legacy behavior, every entity published as a read-only sensor. """
    discovery_expire_after : int = 0
    """ seconds; >0 adds expire_after to sensor/binary_sensor entities. 0 = off. """
    json : bool = False
    reconnect_delay : int = 7
    """ seconds """

    reconnect_attempts : int = 21

    holding_register_prefix : str = ""
    input_register_prefix : str = ""
    coil_register_prefix : str = ""
    discrete_register_prefix : str = ""

    client : MQTTClient | None = None
    mqtt_properties : Properties | None = None

    # Seconds between publishing a discovery message and replaying cached
    # state after Home Assistant announces it is online, so HA has created the
    # entities (and subscribed to their state topics) before values arrive.
    _HA_REPUBLISH_SETTLE : float = 2.0
    # Ignore repeated HA "online" announcements inside this window.
    _HA_REPUBLISH_DEBOUNCE : float = 5.0
    # Deliberate throttle between discovery messages for broker reliability on large maps.
    _DISCOVERY_THROTTLE : float = 0.07

    def __init__(self, settings: TransportSettings) -> None:
        self.host = settings.get("host", fallback="")
        if not self.host:
            raise ValueError("Host is not set")

        self.port = settings.getint("port", fallback=self.port)
        self.base_topic = settings.get("base_topic", fallback=self.base_topic).rstrip("/")
        # Was .rstrip("/") only — the default "/error" has a leading slash
        # too, which needs stripping for this to compose cleanly as a plain
        # topic segment below (base_topic/error_topic, no accidental "//").
        self.error_topic = settings.get("error_topic", fallback=self.error_topic).strip("/")
        self.discovery_topic = settings.get("discovery_topic", fallback=self.discovery_topic)
        self.discovery_enabled = strtobool(settings.get("discovery_enabled", self.discovery_enabled))

        self.discovery_entity_types = settings.get(
            "discovery_entity_types", fallback=self.discovery_entity_types
        ).strip().lower()
        if self.discovery_entity_types not in ("auto", "sensor"):
            warnings.warn(
                f"Unknown discovery_entity_types '{self.discovery_entity_types}' — using 'auto'",
                RuntimeWarning,
            )
            self.discovery_entity_types = "auto"

        self.discovery_expire_after = max(
            0, settings.getint("discovery_expire_after", fallback=self.discovery_expire_after)
        )
        self.json = strtobool(settings.get("json", self.json))
        self.reconnect_delay = settings.getint("reconnect_delay", fallback=7)

        if self.reconnect_delay < 1:  # minimum 1 second
            self.reconnect_delay = 1

        self.reconnect_attempts = settings.getint("reconnect_attempts", fallback=21)
        if self.reconnect_attempts < 0:  # minimum 0
            self.reconnect_attempts = 0

        self.holding_register_prefix = settings.get("holding_register_prefix", fallback="")
        self.input_register_prefix = settings.get("input_register_prefix", fallback="")
        self.coil_register_prefix = settings.get("coil_register_prefix", fallback="")
        self.discrete_register_prefix = settings.get("discrete_register_prefix", fallback="")

        self._registry_type_prefix: dict[Registry_Type, str] = {
            Registry_Type.HOLDING: self.holding_register_prefix,
            Registry_Type.INPUT: self.input_register_prefix,
            Registry_Type.COIL: self.coil_register_prefix,
            Registry_Type.DISCRETE: self.discrete_register_prefix,
        }

        # Instance-level state — never class-level to avoid shared-dict bugs across instances
        self._first_connection: bool = True
        self._reconnect_thread: threading.Thread | None = None
        self._write_topics: dict[str, registry_map_entry] = {}
        # Populated in write_data() the first time each device's telemetry
        # is published; consumed by exit_handler() to mark every actually-
        # seen device offline on clean shutdown (see exit_handler's docstring
        # for why this replaced a single hardcoded topic).
        self._known_device_identifiers: set[str] = set()
        # variable_name -> Registry_Type, per bridged scraper transport_name.
        # Built in init_bridge, consumed by write_data() to resolve which
        # per-registry-type prefix (if any) applies to a given metric.
        self._registry_type_by_name: dict[str, dict[str, Registry_Type]] = {}
        # Home Assistant support state. All instance-level (see the note above
        # about shared-dict bugs).
        # transport_name -> scraper transport, remembered so discovery can be
        # re-announced when Home Assistant restarts (see _handle_ha_status).
        self._discovery_transports: dict[str, transport_base] = {}
        # transport_name -> number of entities last published for it.
        self._discovery_counts: dict[str, int] = {}
        # Last published payload per topic, replayed after an HA restart so
        # entities do not sit at "unknown" until their next scrape.
        self._last_state: dict[str, str] = {}
        self._last_json_state: dict[str, dict[str, int | float | str]] = {}
        self._discovery_lock: threading.RLock = threading.RLock()
        self._last_ha_republish: float | None = None

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

        # Bridge-level connectivity status, distinct from the existing
        # per-device `.../availability` topics (see write_data()). This one
        # answers "is the MQTT bridge's own broker connection up" and is
        # backed by a real Last Will and Testament, so an ungraceful crash
        # (killed process, power loss, segfault) is reflected automatically
        # by the broker — no periodic republish or clean-exit handler
        # required for correctness. It deliberately does NOT try to be a
        # per-device signal: at __init__ time (and even at connect() time,
        # which must happen before any device is known — see connect())
        # nothing here yet knows which scraper transport(s), if any, will
        # end up bridged to this instance via init_bridge(), and a single
        # paho client only supports one Last Will. Per-device data
        # freshness is a different question from broker connectivity and
        # keeps using the periodic-republish mechanism it always has.
        self._bridge_status_topic: str = f"{self.base_topic}/bridge_status"
        self.client.will_set(
            self._bridge_status_topic,
            payload="offline",
            qos=1,
            retain=True,
        )

        self.mqtt_properties = Properties(paho.mqtt.packettypes.PacketTypes.PUBLISH)
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

        base_delay: int = self.reconnect_delay
        max_delay: int = 600  # 10 minutes
        attempt: int = 0

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
            #
            # getattr rather than direct attribute access: tests in this
            # codebase commonly construct via mqtt.__new__(mqtt), bypassing
            # __init__ entirely.
            for device_identifier in getattr(self, "_known_device_identifiers", set[str]()):
                self.client.publish(
                    f"{self.base_topic}/{device_identifier}/availability",
                    "offline",
                )
            # update the bridge-level connectivity status topic too, so a crash or kill is reflected in the broker
            bridge_status_topic: str | None = getattr(self, "_bridge_status_topic", None)
            if bridge_status_topic:
                self.client.publish(bridge_status_topic, "offline", qos=1, retain=True)
            # Give the final publish a moment to flush before the loop stops
            time.sleep(0.5)
            self.client.loop_stop()
            self.client.disconnect()

    # ------------------------------------------------------------------
    # Paho callbacks
    # ------------------------------------------------------------------

    def on_disconnect(
        self,
        client: MQTTClient,
        userdata: object,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None
        ) -> None:
        self.connected = False
        self._start_reconnect_thread()

    def on_connect(
        self,
        client: MQTTClient,
        userdata: object,
        flags: ConnectFlags,
        reason_code: ReasonCode ,
        properties: Properties | None,
        ) -> None:
        """Called when the client receives a CONNACK response from the server."""
        self._log.info("Connected with result code %s", str(reason_code))
        self.connected = True

        bridge_status_topic: str | None = getattr(self, "_bridge_status_topic", None)
        if self.client is not None and bridge_status_topic:
            self.client.publish(
                bridge_status_topic, "online", qos=1, retain=True
            )

        # Re-subscribe to all write topics so they survive a reconnect
        self._resubscribe_write_topics()
        self._subscribe_ha_status()

    # ------------------------------------------------------------------
    # Topic construction — the single source of truth for topic shapes, shared
    # by telemetry publishing, write-topic subscription and HA discovery so
    # the three can never disagree.
    # ------------------------------------------------------------------

    def _telemetry_topic(
        self, from_transport: transport_base, name: str, registry_type: Registry_Type | None
    ) -> str:
        """Flat telemetry topic for one metric (always lower-case)."""
        registry_type_prefix: dict[Registry_Type, str] = getattr(self, "_registry_type_prefix", {})
        prefix: str = registry_type_prefix.get(registry_type, "") if registry_type else ""
        parts: list[str] = [self.base_topic, from_transport.device_identifier]
        if prefix:
            parts.append(prefix)
        parts.append(name)
        return "/".join(parts).lower()

    def _write_topic(
        self, from_transport: transport_base, var_name: str, registry_type: Registry_Type
    ) -> str:
        """Command topic subscribed for one writable metric (case preserved)."""
        registry_type_prefix: dict[Registry_Type, str] = getattr(self, "_registry_type_prefix", {})
        prefix: str = registry_type_prefix.get(registry_type, "")
        parts: list[str] = [self.base_topic, from_transport.device_identifier]
        if prefix:
            parts.append(prefix)
        parts.append(var_name)
        return "/".join(parts) + "/write"

    # ------------------------------------------------------------------
    # Home Assistant status (birth message) handling
    # ------------------------------------------------------------------

    def _ha_status_topic(self) -> str:
        """Topic Home Assistant publishes "online"/"offline" to (its MQTT birth/will)."""
        return f"{self.discovery_topic}/status"

    def _subscribe_ha_status(self) -> None:
        if self.client is None or not getattr(self, "discovery_enabled", False):
            return
        self.client.subscribe(self._ha_status_topic())

    def _handle_ha_status(self, payload: str) -> None:
        """React to Home Assistant announcing it is online.

        Retained discovery configs normally survive an HA restart, but state
        topics are not retained, so entities would show "unknown" until each
        metric is next published (slowly-read registers can take minutes).
        Re-announce discovery and replay the last known values instead.
        """
        if payload.strip().lower() != "online":
            return
        now: float = time.monotonic()
        last: float | None = getattr(self, "_last_ha_republish", None)
        if last is not None and now - last < self._HA_REPUBLISH_DEBOUNCE:
            return
        self._last_ha_republish = now
        # Never block paho's network thread: discovery is throttled and slow.
        threading.Thread(
            target=self._republish_for_ha,
            name=f"mqtt-ha-republish-{getattr(self, 'transport_name', 'mqtt')}",
            daemon=True,
        ).start()

    def _republish_for_ha(self) -> None:
        transports: list[transport_base] = list(getattr(self, "_discovery_transports", {}).values())
        self._log.info(
            "Home Assistant is online — re-announcing discovery for %d device(s).", len(transports)
        )
        try:
            for from_transport in transports:
                self.mqtt_discovery(from_transport)
            time.sleep(self._HA_REPUBLISH_SETTLE)
            self._replay_last_state()
        except Exception as exc:
            self._log.error("Failed to republish for Home Assistant: %s", exc)

    def _replay_last_state(self) -> None:
        """Re-publish the most recent value of every metric (see _handle_ha_status)."""
        if self.client is None:
            return
        for device_identifier in list(getattr(self, "_known_device_identifiers", set[str]())):
            self.client.publish(
                f"{self.base_topic}/{device_identifier}/availability", "online", qos=0, retain=True
            )
        for topic, payload in list(getattr(self, "_last_state", {}).items()):
            self.client.publish(topic, payload)
        for topic, blob in list(getattr(self, "_last_json_state", {}).items()):
            self.client.publish(topic, json.dumps(blob, indent=4))

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

    def _load_writable_allowlist(self, from_transport: transport_base) -> set[str]:
        """
        Load documented-name allowlist from this transport's device-scoped writable CSV.

        If no writable file exists, return an empty set
        (no write topics allowed).
        """
        if from_transport.protocolSettings is None:
            return set()

        # device_name, not protocol_name — write-enable selections are
        # per-device (DeviceProtocolSelection.device_name), not per-protocol.
        # Two transports sharing the same protocol_version (e.g. two 18KPV
        # inverters) can have different write-enabled registers — only one
        # of them might actually be wired up for remote control — and a
        # protocol-scoped file couldn't represent that: every transport on
        # that protocol would share the same file and therefore the same
        # write-enabled set.
        device_name: str = from_transport.transport_name.removeprefix("transport.")
        protocol_name: str = from_transport.protocolSettings.protocol
        allowlist: set[str] = set()

        # Single combined file per device, not per protocol — see the comment above about why this is device-scoped.
        writable_file: str = f"{device_name}.writable.csv"
        writable_path: str | None = (
            from_transport.protocolSettings.find_protocol_file(
                writable_file,
                "config",
            )
        )

        if writable_path:
            try:
                with open(Path(writable_path), newline="", encoding="utf-8") as f:
                    reader: csv.DictReader[str] = csv.DictReader(f)

                    for row in reader:
                        name: str = (
                            (row.get("documented name") or "")
                            .strip()
                            .lower()
                            .replace(" ", "_")
                        )

                        if name:
                            allowlist.add(name)

            except Exception as exc:
                self._log.warning(
                    "Unable to read writable allowlist '%s': %s",
                    writable_path,
                    exc,
                )

        if not allowlist:
            self._log.warning(
                "No writable allowlist found for device '%s' (protocol '%s'; "
                "expected %s in the config directory); MQTT write topics "
                "disabled until write selections are made and committed.",
                device_name,
                protocol_name,
                writable_file,
            )
            return set()

        self._log.info(
            "Loaded %d entries from the '%s' writable allowlist",
            len(allowlist),
            writable_path,
        )

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
        if not hasattr(self, "_known_device_identifiers"):
            self._known_device_identifiers = set()
        self._known_device_identifiers.add(from_transport.device_identifier)
        # Publish availability every loop — required because HA doesn't disconnect
        # cleanly on restart (HA bug), so we can't rely on LWT alone for this
        # per-device signal (see _bridge_status_topic in __init__ for the
        # connectivity-level signal that *is* LWT-backed).
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
        # update the bridge status to reflect that the bridge is online, so a crash or kill is reflected in the broker
        bridge_status_topic: str | None = getattr(self, "_bridge_status_topic", None)
        if bridge_status_topic:
            self.client.publish(bridge_status_topic, "online", qos=1, retain=True)

        if self.json:
            json_object: str = json.dumps(data, indent=4)
            json_topic: str = self.base_topic + "/" + from_transport.device_identifier
            self.client.publish(
                json_topic,
                json_object,
                0,
                properties=self.mqtt_properties,
            )
            # Merge rather than replace: some scrapers publish one key at a time.
            self._cache_json_state(json_topic, data)
        else:
            # Optional per-registry-type topic segment (see
            # _registry_type_prefix / _registry_type_by_name in __init__ and
            # init_bridge) — empty/unset by default, which reproduces the
            # exact flat topic shape this always had. getattr rather than
            # direct attribute access since these are read-only here and
            # tests in this codebase commonly construct via
            # mqtt.__new__(mqtt), bypassing __init__ entirely.
            all_names_by_type: dict[str, dict[str, Registry_Type]] = getattr(self, "_registry_type_by_name", {})
            names_by_type: dict[str, Registry_Type] = all_names_by_type.get(from_transport.transport_name, {})
            val: int | float | str
            for entry, val in data.items():
                if isinstance(val, float) and self.max_precision >= 0:
                    val = round(val, self.max_precision)
                registry_type: Registry_Type | None = names_by_type.get(entry)
                topic: str = self._telemetry_topic(from_transport, entry, registry_type)
                self.client.publish(topic, str(val))
                self._cache_state(topic, str(val))

    def _cache_state(self, topic: str, payload: str) -> None:
        """Remember the last value published to ``topic`` (see _replay_last_state)."""
        if not hasattr(self, "_last_state"):
            self._last_state = {}
        self._last_state[topic] = payload

    def _cache_json_state(self, topic: str, data: dict[str, int | float | str]) -> None:
        if not hasattr(self, "_last_json_state"):
            self._last_json_state = {}
        self._last_json_state.setdefault(topic, {}).update(data)

    def _publish_error(self, context: str, message: str) -> None:
        """
        Scheduling path: N/A — error reporting, independent of read_mode.

        Publish a structured error report to error_topic, if connected.

        Best-effort only: the publish itself is wrapped so a failure while *reporting* an error
        can't cascade into a second, noisier failure, and this is a no-op
        entirely when disconnected (there's nowhere to publish to, and the
        disconnected case is already covered by _bridge_status_topic's LWT).
        """
        if self.client is None or not self.connected:
            return
        payload: str = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": context,
            "message": message,
        })
        try:
            self.client.publish(f"{self.base_topic}/{self.error_topic}", payload, qos=0, retain=False)
        except Exception as exc:
            self._log.debug(f"Failed to publish to error_topic (non-fatal): {exc}")

    def client_on_message(self, client: MQTTClient, userdata: object, msg: MQTTMessage) -> None:
        """Callback for PUBLISH messages received from the broker."""
        self._log.info("MQTT MSG: " + msg.topic + " " + str(msg.payload.decode("utf-8")))

        if getattr(self, "discovery_enabled", False) and msg.topic == self._ha_status_topic():
            self._handle_ha_status(msg.payload.decode("utf-8", errors="replace"))
            return

        if msg.topic in self._write_topics:
            entry: registry_map_entry = self._write_topics[msg.topic]
            # Distinct from the generic "MQTT MSG" line above — this one is
            # tagged by variable_name specifically so it lines up with the
            # eventual device-confirmation log from
            # modbus_base._check_write_response (also tagged by
            # variable_name), letting a specific command be traced end to
            # end with a single grep for its variable name, from receipt
            # here through to whether the device actually acknowledged it.
            self._log.info(
                "Write command received: variable='%s' topic='%s' payload='%s'",
                entry.variable_name,
                msg.topic,
                msg.payload.decode("utf-8"),
            )
            try:
                self._emit_message(entry, msg.payload.decode("utf-8"))
            except Exception as exc:
                self._log.error(f"Failed to process write command on '{msg.topic}': {exc}")
                self._publish_error(
                    "write_command",
                    f"Failed to process write on '{msg.topic}': {exc}",
                )
        else:
            # Broker only delivers messages for topics we've subscribed to
            # (no wildcard subscription exists in this class), so this
            # should be unreachable in practice — but if it ever fires, it
            # means a topic-string mismatch (trailing slash, case, a stale
            # entry after init_bridge reset _write_topics) is silently
            # eating a write. Better to log it than have it vanish again.
            self._log.warning(
                "MQTT message on '%s' received but not in _write_topics — "
                "write ignored. This shouldn't happen without a wildcard "
                "subscription; check for a topic-string mismatch.",
                msg.topic,
            )

    # ------------------------------------------------------------------
    # Bridge initialization
    # ------------------------------------------------------------------

    def init_bridge(self, from_transport: transport_base) -> None:
        if self.client is None or from_transport.protocolSettings is None:
            return

        # Build the variable_name -> Registry_Type lookup used by
        # write_data() for optional per-registry-type telemetry prefixes
        # (see _registry_type_prefix in __init__). Done for every bridged
        # transport, not just write-enabled ones — prefixing telemetry
        # topics is unrelated to whether this transport can be written to.

        if not hasattr(self, "_registry_type_by_name"):
            self._registry_type_by_name = {}
        registry_type_by_name: dict[str, Registry_Type] = {}
        for reg_type in (
            Registry_Type.HOLDING,
            Registry_Type.INPUT,
            Registry_Type.COIL,
            Registry_Type.DISCRETE,
        ):
            for entry in from_transport.protocolSettings.get_registry_map(reg_type):
                registry_type_by_name[entry.variable_name.lower().replace(" ", "_")] = reg_type
        self._registry_type_by_name[from_transport.transport_name] = registry_type_by_name

        if from_transport.write_enabled:
            # Reset only THIS device's topics so a second call (e.g. after reconnect)
            # is clean. Clearing the whole dict would silently drop the write
            # topics of every other write-enabled device bridged to this
            # instance — their subscriptions stay live on the broker, but
            # incoming commands would then be ignored as "not in _write_topics".
            device_prefix: str = f"{self.base_topic}/{from_transport.device_identifier}/"
            self._write_topics = {
                topic: entry
                for topic, entry in self._write_topics.items()
                if not topic.startswith(device_prefix)
            }
            write_allowlist: set[str] = self._load_writable_allowlist(from_transport)
            if not write_allowlist:
                self._log.info(
                    "No writable allowlist found for '%s'; MQTT write topics disabled until write selections are committed.",
                    from_transport.transport_name,
                )

            # Subscribe to holding and coil register write topics.
            #
            # Topic shape: {base_topic}/{device_identifier}/{prefix}/{var_name}/write
            # (prefix segment only present if holding_register_prefix /
            # coil_register_prefix is configured — see _registry_type_prefix
            # in __init__) — i.e. exactly the read/telemetry topic for that
            # variable (published in write_data(), below) with /write
            # appended.

            registry_types: list[Registry_Type] = [Registry_Type.HOLDING, Registry_Type.COIL]
            excluded_by_allowlist: list[str] = []

            for reg_type in registry_types:
                for entry in from_transport.protocolSettings.get_registry_map(reg_type):
                    is_protocol_writable: bool = entry.write_mode in (WriteMode.WRITE, WriteMode.WRITEONLY)
                    entry_name: str = entry.documented_name.strip().lower().replace(" ", "_")

                    if is_protocol_writable and entry_name in write_allowlist:
                        var_name: str = entry.variable_name.lower().replace(" ", "_")
                        # Built by the shared helper (which reads the per-registry-type
                        # prefix via getattr — tests commonly construct via
                        # mqtt.__new__(mqtt), bypassing __init__) so HA discovery
                        # advertises exactly the topic subscribed here.
                        topic: str = self._write_topic(from_transport, var_name, reg_type)

                        existing_entry: registry_map_entry | None = self._write_topics.get(topic)
                        if existing_entry is not None and existing_entry is not entry:
                            self._log.warning(
                                "'%s': write topic '%s' already maps to a different "
                                "register (variable name collision between holding "
                                "and coil entries) — keeping the first one seen, "
                                "'%s' registered second is being ignored for writes.",
                                from_transport.transport_name,
                                topic,
                                entry.variable_name,
                            )
                            continue

                        self._write_topics[topic] = entry
                        self.client.subscribe(topic)
                    elif is_protocol_writable:
                        # Protocol-level write_mode says this entry is writable,
                        # but it's missing from the writable CSV's allowlist, so
                        # no write topic gets subscribed for it at all
                        excluded_by_allowlist.append(entry.variable_name)

            if excluded_by_allowlist:
                self._log.warning(
                    "'%s': %d variable(s) are protocol-writable but excluded from "
                    "MQTT write topics because they're missing from the "
                    "writable CSV allowlist (check the 'documented name' column) — "
                    "no write topic was subscribed for: %s",
                    from_transport.transport_name,
                    len(excluded_by_allowlist),
                    sorted(excluded_by_allowlist),
                )

            self._log.info(
                "MQTT write topic allowlist for '%s': %d topic(s)",
                from_transport.transport_name,
                len(self._write_topics),
            )

        if self.discovery_enabled:
            if not hasattr(self, "_discovery_transports"):
                self._discovery_transports = {}
            self._discovery_transports[from_transport.transport_name] = from_transport
            self.mqtt_discovery(from_transport)

    # ------------------------------------------------------------------
    # Bridge info pane
    # ------------------------------------------------------------------

    def get_health_snapshot(self) -> MqttHealthSnapshot:
        """
        Read-only snapshot of this bridge's live connection/reconnect/
        write-topic state, for the device page's "Bridge Health" panel.
        Pulls together state that's otherwise scattered across connection
        management, write-topic tracking, and Home Assistant discovery
        config — nothing here is a fresh query, just this instance's own
        attributes.

        Unlike the InfluxDB/TimescaleDB bridges, there's deliberately no
        backlog/batch figure here: MQTT publishes are fire-and-forget per
        metric (see write_data()) with no local queue held back on a
        failed publish, so there's nothing to report there. There's also
        no Storage Overview panel for this bridge at all (see
        services/bridge_service module docstring) — MQTT brokers generally
        don't persist historical data, so there's no measurement/table/
        row-count concept to introspect the way InfluxDB/TimescaleDB have.

        `connected` is included for completeness but isn't necessarily
        rendered by the panel — the device page already shows connection
        status in its own status badge.

        Uses getattr with fallbacks throughout, matching this class's own
        established convention (tests here commonly construct via
        mqtt.__new__(mqtt), bypassing __init__ entirely — see e.g.
        exit_handler / write_data / init_bridge above).
        """
        reconnect_thread: threading.Thread | None = getattr(self, "_reconnect_thread", None)
        reconnecting: bool = bool(reconnect_thread is not None and reconnect_thread.is_alive())

        write_topics: dict[str, registry_map_entry] = getattr(self, "_write_topics", dict[str, registry_map_entry]())
        known_device_identifiers: set[str] = getattr(self, "_known_device_identifiers", set[str]())

        return {
            "connected": getattr(self, "connected", False),
            "reconnecting": reconnecting,
            "reconnect_delay": getattr(self, "reconnect_delay", None),
            "reconnect_attempts": getattr(self, "reconnect_attempts", None),
            "write_topic_count": len(write_topics),
            "known_device_count": len(known_device_identifiers),
            "discovery_enabled": getattr(self, "discovery_enabled", False),
            "discovery_entity_types": getattr(self, "discovery_entity_types", "auto"),
            "discovery_entity_count": sum(getattr(self, "_discovery_counts", {}).values()),
            "json_mode": getattr(self, "json", False),
            "base_topic": getattr(self, "base_topic", ""),
        }

    # ------------------------------------------------------------------
    # Home Assistant discovery
    # ------------------------------------------------------------------

    def _discovery_guard(self) -> contextlib.AbstractContextManager[object]:
        """Serialize discovery runs (startup vs. a re-announce after HA restarts)."""
        lock: threading.RLock | None = getattr(self, "_discovery_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def _discovery_config_topic(self, component: str, serial: str, name: str) -> str:
        # The "HN-" segment and the "sensor" topic shape are kept exactly as in
        # earlier releases so existing retained configs are overwritten in place.
        return f"{self.discovery_topic}/{component}/HN-{serial}/{name.replace(' ', '_')}/config"

    def mqtt_discovery(self, from_transport: transport_base) -> None:
        """Publish Home Assistant MQTT discovery configs for one bridged device.

        Each registry entry becomes the most appropriate HA platform
        (``discovery_entity_types = auto``) or a plain read-only sensor
        (``sensor``, the pre-existing behavior):

        - read-only numeric / text values      -> ``sensor`` (with device_class,
          state_class and a normalized unit where they can be inferred safely)
        - read-only coils and discrete inputs  -> ``binary_sensor``
        - writable entries that passed BOTH write gates (protocol write_mode and
          the per-device writable allowlist) -> ``number`` / ``select`` / ``switch``
          wired to the existing ``/write`` topic

        Entries that are merely protocol-writable but not enabled for this device
        stay read-only sensors: discovery never advertises a control the write
        gates would not accept.
        """
        with self._discovery_guard():
            self._log.info("Publishing HA Discovery Topics...")
            client: MQTTClient | None = self.client
            if client is None:
                return

            serial: str = from_transport.device_serial_number
            availability_topic: str = (
                self.base_topic + "/" + from_transport.device_identifier + "/availability"
            )
            # Available only while BOTH signals are online: the per-device data
            # freshness topic and the LWT-backed bridge connectivity topic. A
            # crashed gateway therefore marks every entity unavailable instead of
            # leaving the last value looking current.
            availability: list[dict[str, str]] = [
                {"topic": availability_topic},
                {"topic": getattr(self, "_bridge_status_topic", f"{self.base_topic}/bridge_status")},
            ]
            device: dict[str, str] = {
                "manufacturer": from_transport.device_manufacturer,
                "model": from_transport.device_model,
                "identifiers": "MPG_" + from_transport.device_model + "_" + serial,
                "name": from_transport.device_name,
            }

            registry_map: dict[Registry_Type, list[registry_map_entry]] = {}
            if from_transport.protocolSettings is not None:
                registry_map = from_transport.protocolSettings.registry_map

            entries: list[tuple[Registry_Type, registry_map_entry]] = [
                (reg_type, entry) for reg_type, group in registry_map.items() for entry in group
            ]
            length: int = len(entries)
            published: Counter[str] = Counter()
            seen_names: set[str] = set()
            published_availability: bool = False

            for count, (reg_type, item) in enumerate(entries, start=1):
                if item.concatenate and item.register != item.concatenate_registers[0]:
                    continue  # skip all except the first register to avoid duplicates

                if item.write_mode == WriteMode.READDISABLED:
                    continue

                clean_name: str = item.variable_name.lower().replace(" ", "_").strip()
                if not clean_name:
                    continue

                if clean_name in seen_names:
                    # Same variable name in two registry types: they would share one
                    # unique_id (and telemetry topic), so only the first is published.
                    self._log.debug(f"#Skipping duplicate discovery name \"{clean_name}\" ({reg_type.name})")
                    continue
                seen_names.add(clean_name)

                self._log.debug(f"#Publishing Topic {count} of {length} \"{clean_name}\"")

                plan: _DiscoveryPlan = self._plan_discovery_entity(
                    from_transport, reg_type, item, clean_name, device, availability
                )

                # Clear retained configs this entity may have under another platform
                # (e.g. the old read-only sensor now replaced by a number).
                for stale in plan.stale_components:
                    client.publish(
                        self._discovery_config_topic(stale, serial, clean_name), "", qos=1, retain=True
                    )

                if plan.component is None or plan.payload is None:
                    continue

                client.publish(
                    self._discovery_config_topic(plan.component, serial, clean_name),
                    json.dumps(plan.payload),
                    qos=1,
                    retain=True,
                )
                published[plan.component] += 1

                # Legacy (sensor mode): a write-only entry has no readable value, so
                # its state topic is seeded with a literal marker.
                if item.write_mode == WriteMode.WRITEONLY and plan.component == "sensor":
                    state_topic: str | None = plan.payload.get("state_topic")
                    if state_topic:
                        client.publish(state_topic, "WRITEONLY")

                published_availability = True
                time.sleep(self._DISCOVERY_THROTTLE)

            # Only publish availability if at least one topic was processed,
            # guarding against KeyError on an empty registry map
            if published_availability:
                client.publish(availability_topic, "online", qos=0, retain=True)

            if not hasattr(self, "_discovery_counts"):
                self._discovery_counts = {}
            self._discovery_counts[from_transport.transport_name] = sum(published.values())

            breakdown: str = ", ".join(f"{n} {name}" for name, n in sorted(published.items())) or "none"
            self._log.info(f"Published HA discovery for {sum(published.values())} entities ({breakdown})")

    def _plan_discovery_entity(
        self,
        from_transport: transport_base,
        reg_type: Registry_Type,
        entry: registry_map_entry,
        clean_name: str,
        device: dict[str, str],
        availability: list[dict[str, str]],
    ) -> _DiscoveryPlan:
        """Decide which HA platform ``entry`` becomes and build its config payload."""
        code_dict: dict[str, str] = {}
        if from_transport.protocolSettings is not None:
            code_dict = from_transport.protocolSettings.get_entry_code_dict(entry)
        numeric: bool = is_numeric_entry(entry, bool(code_dict))
        write_only: bool = entry.write_mode == WriteMode.WRITEONLY
        auto: bool = getattr(self, "discovery_entity_types", "auto") == "auto"

        # A live write target = protocol-writable AND subscribed by init_bridge, i.e.
        # it already passed the per-device writable allowlist (the second write gate).
        control_topic: str | None = None
        if reg_type in (Registry_Type.HOLDING, Registry_Type.COIL) and entry.write_mode in (
            WriteMode.WRITE,
            WriteMode.WRITEONLY,
        ):
            candidate: str = self._write_topic(from_transport, clean_name, reg_type)
            existing: registry_map_entry | None = getattr(self, "_write_topics", {}).get(candidate)
            if existing is not None and (existing is entry or existing == entry):
                control_topic = candidate

        native: str  # the platform this entry would be in "auto" mode
        if control_topic is not None:
            if reg_type == Registry_Type.COIL or (
                entry.data_type.value == _DATA_TYPE_1BIT and not code_dict
            ):
                native = "switch"
            elif code_dict:
                native = "select"
            elif numeric:
                native = "number"
            else:
                native = "sensor"  # e.g. a writable string/flag register: read-only here
        elif reg_type in (Registry_Type.COIL, Registry_Type.DISCRETE):
            native = "binary_sensor"
        else:
            native = "sensor"

        component: str | None = native if auto else "sensor"
        if auto and write_only and native not in _HA_CONTROL_COMPONENTS:
            component = None  # nothing to read and nothing to write: no entity at all

        stale: tuple[str, ...] = tuple(sorted({"sensor", native} - {component or ""}))
        if component is None:
            return _DiscoveryPlan(None, None, stale)

        is_control: bool = component in _HA_CONTROL_COMPONENTS
        has_state: bool = not (is_control and write_only)

        payload: HADiscoveryPayload = {
            "availability": availability,
            "availability_mode": "all",
            "device": device,
            "name": _friendly_name(clean_name),
            "unique_id": "MPG_" + from_transport.device_serial_number + "_" + clean_name,
        }

        # ---- state ----
        if has_state:
            if self.json:
                payload["state_topic"] = self.base_topic + "/" + from_transport.device_identifier
                expr: str = f"value_json['{clean_name}']"
            else:
                payload["state_topic"] = self._telemetry_topic(from_transport, clean_name, reg_type)
                expr = "value"
            if component in ("binary_sensor", "switch"):
                # Normalize 1/0, True/False, on/off to the single "1"/"0" pair below.
                payload["value_template"] = (
                    "{{ '1' if (" + expr + " | string | lower) in ['1', 'true', 'on'] else '0' }}"
                )
            elif self.json:
                payload["value_template"] = "{{ " + expr + " }}"

        # ---- platform-specific fields ----
        device_class: str = ""
        if component in ("sensor", "number"):
            unit, device_class, state_class = lookup_ha_unit(entry.unit) if numeric else ("", "", "")
            if unit:
                payload["unit_of_measurement"] = unit
            if component == "sensor":
                state_class = _csv_choice(entry.ha_state_class, state_class)
                if state_class in _HA_STATE_CLASSES and numeric:
                    payload["state_class"] = state_class
                elif state_class:
                    self._log.warning(
                        "'%s': ignoring ha state class '%s' (must be one of %s, on a numeric entry)",
                        clean_name, state_class, sorted(_HA_STATE_CLASSES),
                    )
        device_class = _csv_choice(entry.ha_device_class, device_class)
        # A coil's platform depends on whether it is write-enabled (switch) or not
        # (binary_sensor), and each accepts a different set of classes. Home
        # Assistant rejects the whole entity on a mismatch, so drop the class
        # rather than lose the entity.
        if device_class and component == "switch" and device_class not in HA_SWITCH_DEVICE_CLASSES:
            self._log.warning(
                "'%s': ha device class '%s' is not valid on a switch (use outlet or switch) — ignored",
                clean_name, device_class,
            )
            device_class = ""
        elif device_class and component == "binary_sensor" and device_class in HA_SWITCH_DEVICE_CLASSES:
            self._log.warning(
                "'%s': ha device class '%s' only applies to a write-enabled switch — ignored",
                clean_name, device_class,
            )
            device_class = ""
        if device_class and component != "select":
            payload["device_class"] = device_class

        if component in ("sensor", "binary_sensor") and self.discovery_expire_after > 0:
            payload["expire_after"] = self.discovery_expire_after

        if component in ("binary_sensor", "switch"):
            payload["payload_on"] = "1"
            payload["payload_off"] = "0"
        if component == "switch":
            payload["state_on"] = "1"
            payload["state_off"] = "0"

        if is_control and control_topic is not None:
            payload["command_topic"] = control_topic
            if not has_state:
                payload["optimistic"] = True  # write-only: HA cannot read it back
            if component == "number":
                low, high, step = number_bounds(entry)
                payload["min"], payload["max"], payload["step"] = low, high, step
                payload["mode"] = "box"
            elif component == "select":
                payload["options"] = list(dict.fromkeys(str(v) for v in code_dict.values()))

        # ---- entity category ----
        category: str = entry.ha_entity_category
        if category:
            if category not in _HA_ENTITY_CATEGORIES:
                self._log.warning(
                    "'%s': ignoring ha entity category '%s' (use config or diagnostic)",
                    clean_name, category,
                )
            elif category == "config" and not is_control:
                self._log.debug("'%s': 'config' category only applies to controls — ignored", clean_name)
            else:
                payload["entity_category"] = category

        return _DiscoveryPlan(component, payload, stale)
