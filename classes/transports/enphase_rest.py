# Description: Scraper transport for the Enphase IQ Gateway (Envoy) local HTTPS REST API; polls JSON endpoints and decodes fields addressed by endpoint and JSON path through the protocol registry map.
# File: enphase_rest.py
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

# Scraper transport for the Enphase IQ Gateway (Envoy) local HTTPS REST API.
#
# The gateway has no register map in the Modbus sense: it serves JSON documents from a handful of
# endpoints (/api/v1/production, /ivp/livedata/status, ...). This transport therefore uses a
# text-keyed protocol ("register_key_type": "text" in the protocol JSON) whose ``register`` column
# is a REST key instead of a number:
#
#     /ivp/livedata/status#meters.pv.agg.p.avg          endpoint # dotted JSON path
#     /ivp/ensemble/power#devices:.*.soc|avg            "*" walks a list, "|avg" aggregates it
#
# Each cycle the transport fetches every endpoint that has a due row (once, however many rows read
# from it), turns every addressed JSON value into the raw bytes protocol_settings expects for the
# row's data type, and hands them to protocol_settings.process_registery() so unit multipliers,
# adjustments and enum codes behave exactly as they do for Modbus and CAN protocols.
#
# Authentication follows the gateway firmware: firmware 7.x+ requires a JWT bearer token (supplied
# directly, from a file, or fetched and renewed from Enphase Enlighten with the owner's
# credentials); older firmware uses HTTP digest authentication. The gateway uses a self-signed
# certificate, so certificate verification is off unless a CA certificate is configured.
import base64
import binascii
import json
import math
import re
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, cast

import requests
import urllib3
from requests.auth import HTTPDigestAuth

from classes.log_redaction import register_secret
from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    registry_map_entry,
)
from defs.common import TransportSettings

from .transport_base import transport_base

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
''' anything json.loads() can return '''

WILDCARD: str = "*"
''' path segment that walks every element of a list (or every value of an object) '''

AGGREGATES: frozenset[str] = frozenset({"sum", "avg", "min", "max", "count"})
''' aggregate suffixes (``|sum``) a key may end with; they reduce the values a wildcard path matched '''

AUTH_TYPES: frozenset[str] = frozenset({"bearer", "digest", "none"})
''' authentication schemes the transport can use '''

ENLIGHTEN_LOGIN_URL: str = "https://enlighten.enphaseenergy.com/login/login.json"
''' Enphase cloud login; returns a session_id (step 1 of the JWT exchange) '''

ENTREZ_TOKEN_URL: str = "https://entrez.enphaseenergy.com/tokens"  # noqa: S105 - a URL, not a secret
''' Enphase token service; exchanges a session_id and the gateway serial for a local JWT (step 2) '''

# data types a JSON value can be encoded as. Whole-number types are (bytes, signed).
_INTEGER_TYPES: dict[Data_Type, tuple[int, bool]] = {
    Data_Type.USHORT: (2, False),
    Data_Type.SHORT: (2, True),
    Data_Type.UINT: (4, False),
    Data_Type.INT: (4, True),
    Data_Type.ACC32: (4, False),
    Data_Type.UINT64: (8, False),
    Data_Type.INT64: (8, True),
}
_FLAG_TYPES: dict[Data_Type, int] = {
    Data_Type.BIT8_FLAGS: 2,
    Data_Type.BIT16_FLAGS: 2,
    Data_Type.BIT32_FLAGS: 4,
}
_TEXT_TYPES: frozenset[Data_Type] = frozenset({
    Data_Type.ASCII,
    Data_Type.STRING,
    Data_Type.STRING16,
    Data_Type.STRING32,
})
_FLOAT_TYPES: dict[Data_Type, str] = {
    Data_Type.FLOAT32: ">f",
    Data_Type.FLOAT64: ">d",
}


@dataclass(frozen=True)
class RestKey:
    """
    A parsed ``register`` cell of an Enphase REST protocol map.

    Syntax: ``<endpoint>[#<path>][|<aggregate>]``

    * ``endpoint`` starts with ``/`` and may carry a query string (``/ivp/meters/readings``).
    * ``path`` is a dotted walk through the JSON document: object keys, list indexes
      (``devices:.0.soc``, negative indexes count from the end) and ``*`` to walk every element.
      An empty path addresses the document itself.
    * ``aggregate`` is ``sum``, ``avg``, ``min``, ``max`` or ``count`` and reduces the values the
      path matched; a path containing ``*`` must have one.
    """
    endpoint: str
    path: tuple[str, ...] = ()
    aggregate: str = ""

    @classmethod
    def parse(cls, text: str) -> RestKey:
        """Scheduling path: N/A — setup, runs once per map row. Raises ValueError for a malformed key."""
        endpoint: str
        fragment: str
        endpoint, _, fragment = text.strip().partition("#")
        endpoint = endpoint.strip()
        if not endpoint.startswith("/"):
            message: str = f"REST key {text!r}: the endpoint must start with '/'"
            raise ValueError(message)

        aggregate: str = ""
        path_text: str = fragment.strip()
        if "|" in path_text:
            path_text, _, aggregate = path_text.rpartition("|")
            aggregate = aggregate.strip().lower()
            if aggregate not in AGGREGATES:
                message: str = f"REST key {text!r}: unknown aggregate {aggregate!r} (use {', '.join(sorted(AGGREGATES))})"
                raise ValueError(message)

        path: tuple[str, ...] = ()
        if path_text.strip():
            path = tuple(segment.strip() for segment in path_text.strip().split("."))
            if any(not segment for segment in path):
                message: str = f"REST key {text!r}: the path has an empty segment"
                raise ValueError(message)

        if WILDCARD in path and not aggregate:
            message: str = f"REST key {text!r}: a '*' path needs an aggregate such as |sum"
            raise ValueError(message)

        return cls(endpoint=endpoint, path=path, aggregate=aggregate)


@dataclass
class EndpointBinding:
    """One protocol-map row bound to the REST key it reads."""
    entry: registry_map_entry
    registry_type: Registry_Type
    key: RestKey


@dataclass
class DecodeBatch:
    """
    The raw bytes of one decode pass plus the rows they belong to.

    Rows that share a key share ``entry.register``; a second row with different bytes for the
    same id (another data type or aggregate) cannot share a pass, so it gets a batch of its own.
    """
    raw: dict[int, bytes] = field(default_factory=lambda: {})
    entries: list[registry_map_entry] = field(default_factory=lambda: [])


def walk_path(node: JsonValue, segments: tuple[str, ...]) -> list[JsonValue]:
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved) — pure helper called every poll.

    Follow ``segments`` from ``node`` and return every value they reach. A missing key, an index
    out of range or a step into a scalar yields no match rather than an error: the gateway omits
    fields that do not apply to a site (no battery, no consumption CT).
    """
    if not segments:
        return [node]

    head: str = segments[0]
    rest: tuple[str, ...] = segments[1:]

    if head == WILDCARD:
        children: list[JsonValue]
        if isinstance(node, list):
            children = list(node)
        elif isinstance(node, dict):
            children = list(node.values())
        else:
            return []
        matches: list[JsonValue] = []
        for child in children:
            matches.extend(walk_path(child, rest))
        return matches

    if isinstance(node, dict):
        return walk_path(node[head], rest) if head in node else []

    if isinstance(node, list):
        try:
            index: int = int(head)
        except ValueError:
            return []
        return walk_path(node[index], rest) if -len(node) <= index < len(node) else []

    return []


def _numbers(items: list[JsonValue]) -> list[int | float]:
    ''' the numeric items of a match list; booleans are flags, not numbers, and are left out '''
    return [item for item in items if isinstance(item, (int, float)) and not isinstance(item, bool)]


def extract_value(document: JsonValue, key: RestKey) -> JsonValue:
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved) — pure helper called every poll.

    Resolve ``key`` against a decoded JSON document and return the single value it addresses, or
    None when the document does not contain it. With an aggregate the values the path matched
    are reduced: ``sum`` of nothing is 0 and ``count`` of nothing is 0, while ``avg``, ``min`` and
    ``max`` of nothing are None (no value) so a site without batteries reports no state of charge.
    """
    matches: list[JsonValue] = walk_path(document, key.path)

    if not key.aggregate:
        return matches[0] if matches else None

    items: list[JsonValue] = matches
    if len(matches) == 1 and WILDCARD not in key.path:
        # "devices:|count": the path names the collection itself, so aggregate its elements
        only: JsonValue = matches[0]
        if isinstance(only, list):
            items = list(only)
        elif isinstance(only, dict):
            items = list(only.values())

    if key.aggregate == "count":
        return sum(1 for item in items if item is not None)

    numbers: list[int | float] = _numbers(items)
    if key.aggregate == "sum":
        return sum(numbers)
    if not numbers:
        return None
    if key.aggregate == "avg":
        return math.fsum(numbers) / len(numbers)
    return min(numbers) if key.aggregate == "min" else max(numbers)


def is_encodable(data_type: Data_Type) -> bool:
    """Scheduling path: N/A — setup. True when ``encode_value`` can represent ``data_type``."""
    return (
        data_type in _INTEGER_TYPES
        or data_type in _FLAG_TYPES
        or data_type in _TEXT_TYPES
        or data_type in _FLOAT_TYPES
        or data_type in (Data_Type.BYTE, Data_Type.HEX)
    )


def _as_number(value: JsonValue) -> int | float | None:
    ''' a JSON value as a number: numbers as they are, booleans as 0/1, numeric strings parsed; None otherwise '''
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and not math.isfinite(value) else value
    if isinstance(value, str):
        try:
            parsed: float = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def encode_value(data_type: Data_Type, value: JsonValue) -> bytes | None:
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved) — pure helper called every poll.

    Turn a decoded JSON value into the big-endian wire bytes ``protocol_settings`` decodes for
    ``data_type``, so the shared decode path (unit multiplier, adjustments, enum codes) applies
    unchanged. Returns None when the value cannot be represented — it is null, an object or list,
    not numeric for a numeric type, or outside the range of an integer type. Fractions are
    rounded for whole-number types; use a float type to keep them.
    """
    if value is None or isinstance(value, (list, dict)):
        return None

    if data_type in _TEXT_TYPES:
        text: str = ("true" if value else "false") if isinstance(value, bool) else str(value)
        return text.encode("utf-8")

    if data_type == Data_Type.HEX:
        try:
            return bytes.fromhex(str(value))
        except ValueError:
            return None

    number: int | float | None = _as_number(value)
    if number is None:
        return None

    if data_type in _FLOAT_TYPES:
        try:
            return struct.pack(_FLOAT_TYPES[data_type], number)
        except (struct.error, OverflowError):
            return None

    integer: int = round(number)

    if data_type == Data_Type.BYTE:
        # protocol_settings reads a BYTE as the high byte of a 16-bit register
        return bytes([integer, 0]) if 0 <= integer <= 0xFF else None

    if data_type in _FLAG_TYPES:
        size: int = _FLAG_TYPES[data_type]
        return integer.to_bytes(size, "big") if 0 <= integer < 1 << (size * 8) else None

    if data_type in _INTEGER_TYPES:
        width, signed = _INTEGER_TYPES[data_type]
        low: int = -(1 << (width * 8 - 1)) if signed else 0
        high: int = (1 << (width * 8 - 1)) - 1 if signed else (1 << (width * 8)) - 1
        return integer.to_bytes(width, "big", signed=signed) if low <= integer <= high else None

    return None


def jwt_expiry(token: str) -> float | None:
    """
    Scheduling path: N/A — token housekeeping at connect.

    The ``exp`` claim (unix seconds) of a JWT, read without verifying the signature — the gateway
    does the verifying; this only decides when to renew. None if ``token`` is not a JWT or has no
    usable ``exp``.
    """
    parts: list[str] = token.split(".")
    if len(parts) != 3:
        return None
    payload: str = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims: object = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    expiry: object = cast(dict[str, object], claims).get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    return float(expiry)


class enphase_rest(transport_base):

    transport_type = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    DUE_SLACK_MS: float = 500.0
    ''' a row is read when it is due within this many ms, so a cycle that starts a little early does not skip a whole interval '''

    REFRESH_RETRY_SECONDS: float = 900.0
    ''' minimum gap between failed attempts to renew the JWT, so a wrong password is not retried every cycle '''

    host: str
    ''' gateway hostname or IP address '''

    port: int
    ''' gateway TCP port; 443 for HTTPS, 80 for plain HTTP '''

    def __init__(self, settings: TransportSettings) -> None:
        super().__init__(settings)

        self.host = settings.get("host", fallback="").strip()
        if not self.host:
            raise ValueError("Host is not set")

        self.use_https: bool = settings.getboolean("use_https", fallback=True)
        ''' firmware 7.x+ serves HTTPS only; set false for pre-7.x gateways, which use plain HTTP '''

        self.port = settings.getint("port", fallback=443 if self.use_https else 80)

        self.verify_tls: bool = settings.getboolean("verify_tls", fallback=False)
        ''' the gateway presents a self-signed certificate, so verification is off by default '''

        self.ca_cert: str = settings.get("ca_cert", fallback="").strip()
        ''' CA bundle or certificate to verify the gateway against; implies verification '''
        if self.ca_cert and not Path(self.ca_cert).is_file():
            message: str = f"ca_cert file not found: {self.ca_cert}"
            raise ValueError(message)

        self.auth_type: str = settings.get("auth_type", fallback="bearer").strip().lower()
        if self.auth_type not in AUTH_TYPES:
            message: str = f"Unknown auth_type {self.auth_type!r} (use {', '.join(sorted(AUTH_TYPES))})"
            raise ValueError(message)

        self.token: str = settings.get("token", fallback="").strip()
        ''' JWT for firmware 7.x+; valid for a year '''
        self.token_file: str = settings.get("token_file", fallback="").strip()
        ''' file holding the JWT, for setups that keep secrets out of config.cfg '''
        self.token_refresh_days: float = settings.getfloat("token_refresh_days", fallback=14.0)
        ''' renew the JWT this many days before it expires (needs the Enlighten credentials) '''

        self.enlighten_username: str = settings.get("enlighten_username", fallback="").strip()
        self.enlighten_password: str = settings.get("enlighten_password", fallback="").strip()
        ''' Enphase Enlighten account; only used to fetch and renew the JWT, never sent to the gateway '''

        self.username: str = settings.get("username", fallback="envoy").strip()
        self.password: str = settings.get("password", fallback="").strip()
        ''' digest authentication (pre-7.x): user "envoy", password the last six digits of the serial number '''

        self.timeout: float = settings.getfloat("timeout", fallback=10.0)
        ''' seconds to wait for one response '''
        self.request_delay: float = settings.getfloat("request_delay", fallback=0.5)
        ''' seconds between requests inside one cycle; the gateway throttles rapid-fire requests '''

        self._session: requests.Session | None = None
        self._digest: HTTPDigestAuth | None = None
        self._request_lock: threading.RLock = threading.RLock()
        self._next_refresh_attempt: float = 0.0
        self._missing_logged: set[str] = set()
        self._unsupported_logged: set[str] = set()
        self._tls_warned: bool = False

        self._bindings: dict[str, list[EndpointBinding]] = {}
        ''' protocol-map rows grouped by the endpoint they read, in map order '''
        self._description_entries: dict[Registry_Type, list[registry_map_entry]] = {}
        ''' the generated *_desc rows, which decode from their source row rather than from the document '''

        self._build_bindings()
        self._load_token_file()

    # ------------------------------------------------------------------ #
    # setup                                                              #
    # ------------------------------------------------------------------ #

    def _build_bindings(self) -> None:
        '''
        Scheduling path: N/A — setup, runs once.

        Groups the protocol map's rows by endpoint. A row whose key does not parse, whose data
        type cannot hold a JSON value, or that is not readable is left out with a warning, so one
        bad row does not take the rest of the map down.
        '''
        if self.protocolSettings is None:
            raise ValueError("protocol_version is not set; enphase_rest needs a REST protocol such as enphase_iq_gateway_rest")

        text_keyed: bool = self.protocolSettings.settings.get("register_key_type", "numeric").strip().lower() == "text"
        if not text_keyed:
            message: str = (
                f"Protocol '{self.protocolSettings.protocol}' is not text-keyed; "
                "enphase_rest needs \"register_key_type\": \"text\" in the protocol JSON"
            )
            raise ValueError(message)

        for registry_type, entries in self.protocolSettings.registry_map.items():
            for entry in entries:
                if entry.description_source:
                    self._description_entries.setdefault(registry_type, []).append(entry)
                    continue
                if entry.write_mode in (WriteMode.READDISABLED, WriteMode.WRITEONLY):
                    continue
                if not is_encodable(entry.data_type):
                    self._log.warning(f"Skipping '{entry.variable_name}': data type {entry.data_type.name} cannot hold a REST value")
                    continue
                try:
                    key: RestKey = RestKey.parse(entry.register_key)
                except ValueError as err:
                    self._log.warning(f"Skipping '{entry.variable_name}': {err}")
                    continue
                self._bindings.setdefault(key.endpoint, []).append(EndpointBinding(entry, registry_type, key))

        if not self._bindings:
            self._log.warning(f"Protocol '{self.protocolSettings.protocol}' has no readable REST rows for {self.transport_name}")

    def _load_token_file(self) -> None:
        ''' Scheduling path: N/A — setup. Reads the JWT from ``token_file`` when no ``token`` is configured. '''
        if self.token or not self.token_file:
            return
        try:
            self.token = Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as err:
            self._log.error(f"Could not read token_file {self.token_file}: {err}")
            return
        register_secret(self.token)  # not in config.cfg, so the log filter has not seen it

    # ------------------------------------------------------------------ #
    # scheduling                                                         #
    # ------------------------------------------------------------------ #

    @property
    def scrape_target(self) -> str:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Identifies the gateway: host, port and protocol. The gateway only schedules transports
        with a non-empty scrape_target, and sections that read the same gateway with the same
        protocol are consolidated into one group, so the gateway is polled once per cycle.
        '''
        protocol: str = str(getattr(self, "protocol_version", "") or "")
        return f"{self.host}:{self.port}:{protocol}"

    def interleaved_cycle_timeout(self) -> float:
        '''
        Scheduling path: Interleaved only.

        Worst case is every endpoint timing out, so scale with the endpoint count rather than
        using the 60 second default.
        '''
        default: float = super().interleaved_cycle_timeout()
        return max(default, 2.0 * len(self._bindings) * (self.timeout + self.request_delay))

    # ------------------------------------------------------------------ #
    # connection                                                         #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved) — called at startup and on reconnect from any read path.

        Opens a fresh HTTP session, reads the gateway's serial number, makes sure a usable JWT
        is held, and proves the credentials by fetching the first endpoint of the map. Returns
        True when the gateway answered; on failure the next cycle tries again.
        '''
        if self.first_connect:
            self.first_connect = False
            self._log.info(f"Connecting to Enphase IQ Gateway: host={self.host}, port={self.port}")
        else:
            self._log.info(f"Reconnecting transport {self.transport_name}")

        self._close_session()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        if self.use_https and not self.ca_cert and not self.verify_tls:
            # InsecureRequestWarning would otherwise be printed for every request
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            if not self._tls_warned:
                self._tls_warned = True
                self._log.info("TLS certificate verification is off (the gateway's certificate is self-signed); set ca_cert to verify it")

        self._discover_device_info()

        if not self._prepare_auth():
            self.connected = False
            return False

        probe: str | None = next(iter(self._bindings), None)
        if probe is not None and self._get_json(probe) is None:
            self.connected = False
            return False

        for bindings in self._bindings.values():
            for binding in bindings:
                binding.entry.next_read_timestamp = 0.0  # a (re)connect reads everything on the first cycle

        self.connected = True
        return True

    def cleanup(self) -> None:
        '''
        Scheduling path: N/A — shutdown, runs once regardless of read_mode.

        Closes the HTTP session, then lets transport_base mark the transport disconnected.
        '''
        self._close_session()
        super().cleanup()

    def enable_write(self) -> None:
        '''
        Scheduling path: N/A — setup, runs once regardless of read_mode.

        The local API is read-only apart from the Enpower relay and battery mode, which this
        transport does not drive, so write mode is refused rather than silently ignored.
        '''
        self.write_enabled = False
        self._log.warning(f"{self.transport_name}: the Enphase local REST API is read-only here; write mode ignored")

    def _close_session(self) -> None:
        ''' Scheduling path: N/A — connect/shutdown. '''
        session: requests.Session | None = self._session
        self._session = None
        if session is not None:
            session.close()

    def _discover_device_info(self) -> None:
        '''
        Scheduling path: N/A — runs once per connect.

        Best effort: the gateway serves an unauthenticated XML page at /info that carries its
        serial number and firmware. The serial number is needed for the device identifier and for
        renewing the JWT; a configured device_serial_number always wins. Any failure is ignored.
        '''
        session: requests.Session | None = self._session
        if session is None:
            return
        try:
            response: requests.Response = session.get(self._url("/info"), timeout=self.timeout, verify=self._verify_arg())
        except requests.RequestException as err:
            self._log.debug(f"{self.transport_name}: /info not available: {err}")
            return
        if not response.ok:
            return

        serial: re.Match[str] | None = re.search(r"<sn>\s*([^<\s]+)\s*</sn>", response.text)
        software: re.Match[str] | None = re.search(r"<software>\s*([^<]+?)\s*</software>", response.text)
        if serial and not self.device_serial_number:
            self.device_serial_number = serial.group(1)
            self.update_identifier()
            self._log.info(f"Transport {self.transport_name} serial number: {self.device_serial_number}")
        if software:
            self._log.info(f"Transport {self.transport_name} firmware: {software.group(1)}")

    # ------------------------------------------------------------------ #
    # authentication                                                     #
    # ------------------------------------------------------------------ #

    def _prepare_auth(self) -> bool:
        '''
        Scheduling path: N/A — runs once per connect.

        Bearer: renews the JWT when it is missing or close to expiry (if Enlighten credentials
        are configured) and refuses to continue without a usable one. Digest: derives the
        password from the serial number when none is configured. None: nothing to do.
        '''
        if self.auth_type == "none":
            return True

        if self.auth_type == "digest":
            password: str = self.password or self.device_serial_number[-6:]
            if not password:
                self._log.error(f"{self.transport_name}: digest authentication needs a password or the gateway's serial number")
                return False
            self._digest = HTTPDigestAuth(self.username, password)
            return True

        if self._token_needs_refresh():
            self._refresh_token()

        if not self.token:
            self._log.error(
                f"{self.transport_name}: no JWT token. Set token or token_file, or set enlighten_username, "
                "enlighten_password (and device_serial_number) so one can be fetched"
            )
            return False

        expiry: float | None = jwt_expiry(self.token)
        if expiry is not None and expiry <= time.time():
            self._log.error(f"{self.transport_name}: the JWT token has expired; renew it or set the Enlighten credentials")
            return False
        return True

    def _token_needs_refresh(self) -> bool:
        ''' Scheduling path: N/A — runs once per connect and on a 401. True when there is no token or it expires within token_refresh_days. '''
        if not self.token:
            return True
        expiry: float | None = jwt_expiry(self.token)
        return expiry is not None and expiry - time.time() < self.token_refresh_days * 86400.0

    def _request_token(self) -> str:
        '''
        Scheduling path: N/A — called by _refresh_token only.

        The two Enphase cloud calls. Raises requests.RequestException for a transport or HTTP
        failure and ValueError for a reply that lacks a session id or is not a JWT.
        '''
        login: requests.Response = requests.post(
            ENLIGHTEN_LOGIN_URL,
            data={"user[email]": self.enlighten_username, "user[password]": self.enlighten_password},
            timeout=self.timeout,
        )
        login.raise_for_status()
        login_body: object = login.json()
        session_id: object = cast(dict[str, object], login_body).get("session_id") if isinstance(login_body, dict) else None
        if not isinstance(session_id, str) or not session_id:
            no_session: str = "the Enlighten login returned no session_id"
            raise ValueError(no_session)

        exchange: requests.Response = requests.post(
            ENTREZ_TOKEN_URL,
            json={"session_id": session_id, "serial_num": self.device_serial_number, "username": self.enlighten_username},
            timeout=self.timeout,
        )
        exchange.raise_for_status()
        token: str = exchange.text.strip()
        if token.count(".") != 2:
            not_jwt: str = "the token service did not return a JWT"
            raise ValueError(not_jwt)
        return token

    def _refresh_token(self) -> bool:
        '''
        Scheduling path: N/A — runs once per connect and on a 401.

        Fetches a new JWT from Enphase: the Enlighten login yields a session id, which the token
        service exchanges, together with the gateway serial number, for the token. Needs
        enlighten_username, enlighten_password and a serial number. A failure is not retried for
        REFRESH_RETRY_SECONDS. Returns True when a new token is held.
        '''
        if not (self.enlighten_username and self.enlighten_password):
            return False
        if time.monotonic() < self._next_refresh_attempt:
            return False
        if not self.device_serial_number:
            self._log.error(f"{self.transport_name}: renewing the JWT needs the gateway serial number; set device_serial_number")
            self._next_refresh_attempt = time.monotonic() + self.REFRESH_RETRY_SECONDS
            return False

        try:
            token: str = self._request_token()
        except (requests.RequestException, ValueError) as err:
            self._log.error(f"{self.transport_name}: could not renew the Enphase JWT: {err}")
            self._next_refresh_attempt = time.monotonic() + self.REFRESH_RETRY_SECONDS
            return False

        self.token = token
        register_secret(token)
        self._next_refresh_attempt = 0.0
        expiry: float | None = jwt_expiry(token)
        valid_until: str = time.strftime("%Y-%m-%d", time.localtime(expiry)) if expiry is not None else "unknown"
        self._log.info(f"{self.transport_name}: Enphase JWT renewed, valid until {valid_until}")
        return True

    # ------------------------------------------------------------------ #
    # wire access                                                        #
    # ------------------------------------------------------------------ #

    def _url(self, endpoint: str) -> str:
        ''' Scheduling path: All — absolute URL of an endpoint; the port is left out when it is the scheme's default '''
        scheme: str = "https" if self.use_https else "http"
        host: str = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        default_port: int = 443 if self.use_https else 80
        netloc: str = host if self.port == default_port else f"{host}:{self.port}"
        return f"{scheme}://{netloc}{endpoint}"

    def _verify_arg(self) -> bool | str:
        ''' Scheduling path: All — the ``verify`` argument for requests: a CA path when configured, otherwise the verify_tls flag '''
        return self.ca_cert if self.ca_cert else self.verify_tls

    def _headers(self) -> dict[str, str]:
        ''' Scheduling path: All — per-request headers; the bearer token is added here so a renewed token is used at once '''
        if self.auth_type == "bearer" and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _send(self, url: str) -> requests.Response | None:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        One authenticated GET. None when the gateway could not be reached, in which case the
        transport is marked disconnected so the gateway reconnects on its next cycle.
        '''
        session: requests.Session | None = self._session
        if session is None:
            self._log.error(f"{self.transport_name}: request made before connect()")
            self.connected = False
            return None
        try:
            with self._request_lock:
                return session.get(
                    url,
                    headers=self._headers(),
                    auth=self._digest if self.auth_type == "digest" else None,
                    timeout=self.timeout,
                    verify=self._verify_arg(),
                )
        except requests.RequestException as err:
            self._log.error(f"Request to {url} failed: {err}")
            self.connected = False
            return None

    def _get_json(self, endpoint: str) -> JsonValue:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        One GET of an endpoint, decoded as JSON; None for any failure. A request that cannot
        reach the gateway, or a 401 that renewing the token does not fix, marks the transport
        disconnected so the gateway reconnects on its next cycle. Other HTTP errors (a throttled
        or busy gateway) leave it connected, and the endpoint is simply missing from this cycle.
        '''
        url: str = self._url(endpoint)
        response: requests.Response | None = self._send(url)

        if response is not None and response.status_code == 401 and self.auth_type == "bearer" and self._refresh_token():
            response = self._send(url)  # once more, with the renewed token

        if response is None:
            return None

        if response.status_code == 401:
            self._log.error(f"{self.transport_name}: {url} answered 401 Unauthorized; the JWT token is wrong or expired")
            self.connected = False
            return None

        if not response.ok:
            self._log.warning(f"{self.transport_name}: {url} answered HTTP {response.status_code}")
            return None

        try:
            return cast(JsonValue, response.json())
        except ValueError:
            self._log.warning(f"{self.transport_name}: {url} did not return JSON")
            return None

    # ------------------------------------------------------------------ #
    # polling                                                            #
    # ------------------------------------------------------------------ #

    def _due_endpoints(self, now_ms: float) -> list[str]:
        ''' Scheduling path: All — the endpoints with at least one row due, in map order '''
        return [
            endpoint
            for endpoint, bindings in self._bindings.items()
            if any(b.entry.next_read_timestamp <= now_ms + self.DUE_SLACK_MS for b in bindings)
        ]

    def _poll_iter(self, now_ms: float) -> Iterator[tuple[str, JsonValue]]:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Fetches each due endpoint once and yields (endpoint, document) after every attempt,
        keeping the cycle bookkeeping the gateway uses to decide whether a result is complete
        enough for completeness-sensitive bridges. A failed endpoint yields None. If the
        transport drops mid-cycle the remaining endpoints are counted as skipped, not tried.
        '''
        due: list[str] = self._due_endpoints(now_ms)
        self._cycle_expect_unit(len(due))

        for position, endpoint in enumerate(due):
            if position and self.request_delay > 0:
                time.sleep(self.request_delay)

            document: JsonValue = self._get_json(endpoint)
            if document is None:
                self.cycle_mark_incomplete()
            else:
                self._cycle_mark_unit_complete()
            yield endpoint, document

            if not self.connected:
                remaining: int = len(due) - position - 1
                if remaining:
                    self.cycle_mark_incomplete(remaining)
                return

    def _decode_document(self, endpoint: str, document: JsonValue, now_ms: float) -> dict[str, int | float | str]:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Decodes the due rows of one endpoint from its document. A row whose value is absent or
        cannot be represented is omitted (reported once). Raw bytes go through
        protocol_settings.process_registery(), the same path Modbus and CAN values take.
        '''
        if self.protocolSettings is None:
            return {}

        batches: dict[Registry_Type, list[DecodeBatch]] = {}

        for binding in self._bindings.get(endpoint, []):
            entry: registry_map_entry = binding.entry
            if entry.next_read_timestamp > now_ms + self.DUE_SLACK_MS:
                continue
            entry.next_read_timestamp = now_ms + entry.read_interval

            value: JsonValue = extract_value(document, binding.key)
            raw: bytes | None = encode_value(entry.data_type, value)
            if raw is None:
                self._report_missing(entry, value)
                continue

            type_batches: list[DecodeBatch] = batches.setdefault(binding.registry_type, [])
            batch: DecodeBatch | None = next(
                (b for b in type_batches if b.raw.get(entry.register, raw) == raw),
                None,
            )
            if batch is None:
                batch = DecodeBatch()
                type_batches.append(batch)
            batch.raw[entry.register] = raw
            batch.entries.append(entry)

        info: dict[str, int | float | str] = {}
        for registry_type, type_batches in batches.items():
            extras: list[registry_map_entry] = self._description_entries.get(registry_type, [])
            for batch in type_batches:
                info.update(self.protocolSettings.process_registery(batch.raw, batch.entries + extras))
        return info

    def _report_missing(self, entry: registry_map_entry, value: JsonValue) -> None:
        ''' Scheduling path: All — says once per row why it has no value, then stays quiet (a site without a battery reports no battery rows every cycle) '''
        if entry.variable_name in self._missing_logged:
            return
        self._missing_logged.add(entry.variable_name)
        reason: str = "is not in the response" if value is None else f"has value {value!r}, which does not fit {entry.data_type.name}"
        self._log.info(f"{self.transport_name}: '{entry.variable_name}' ({entry.register_key}) {reason}; it is left out")

    def read_data(self) -> dict[str, int | float | str]:
        '''
        Scheduling path: Sequential and Concurrent (also the fallback for standalone reads).

        Fetches every due endpoint, then decodes the documents.
        '''
        now_ms: float = time.time() * 1000.0
        info: dict[str, int | float | str] = {}

        for endpoint, document in self._poll_iter(now_ms):
            if document is not None:
                info.update(self._decode_document(endpoint, document, now_ms))

        if not info:
            self._log.info(f"Data is empty; {self.transport_name} busy or not responding?")

        return info

    def read_data_iter(self) -> Iterator[bool]:
        '''
        Scheduling path: Interleaved only (called directly for standalone transports, or as
        read_group_data_iter's per-member fallback).

        Fetches one endpoint at a time, yielding True after each (success or failure) so other
        transports can take their turn. Call get_partial_data() to retrieve the result.

        When called from read_group_data_iter, _cycle_active is already True, so cycle
        tracking is left to the group iterator.
        '''
        _owner: bool = not getattr(self, "_cycle_active", False)
        if _owner:
            self._start_cycle_tracking()

        now_ms: float = time.time() * 1000.0
        yielded: bool = False
        for endpoint, document in self._poll_iter(now_ms):
            if document is not None:
                self._partial_info.update(self._decode_document(endpoint, document, now_ms))
            yielded = True
            yield True

        if not yielded:
            yield True  # nothing was due; still one (empty) block, as the base class does

        if _owner:
            self.finish_cycle_tracking(self._partial_info)
