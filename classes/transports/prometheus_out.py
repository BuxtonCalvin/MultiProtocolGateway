# bridge transport module for a Prometheus scraping (pull-model) output transport with an in-memory metric registry, dynamic per-field Gauge/Counter/Histogram metrics, and per-machine target health tracking.
# File: prometheus_out.py
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

"""
Bridge module for a Prometheus scraping (pull-model) output transport.

Every other bridge in this codebase (timescaledb, influxdb_out, mqtt) is a
*push* transport: MPG's own worker loops decide when data leaves the
process. Prometheus is the opposite -- an external Prometheus server decides
when to pull, on its own schedule, by issuing an HTTP GET against this
bridge's ``/metrics`` endpoint. That inversion is the entire design
challenge this module solves, and it's why the architecture still mirrors
timescaledb.py / influxdb_out.py's separation of "background collection"
from "external I/O" even though the I/O direction is reversed:

    * Background:  every scraper's ``write_data()`` call (one per
                    completed read cycle, at whatever ``read_interval``
                    that scraper is configured with) updates an in-memory
                    ``prometheus_client`` registry. This is pure Python
                    dict/object mutation -- no network I/O, no blocking.
    * External I/O: a Prometheus server's scrape of ``/metrics`` reads
                    straight out of that same in-memory registry and never
                    touches a scraper transport, a lock shared with the
                    read loop, or triggers a device read of any kind.

Because ``prometheus_client``'s Gauge/Counter/Histogram objects already
retain whatever value was last ``.set()``/``.inc()``/``.observe()``d, a
slow 60s-interval machine simply keeps re-serving its last known reading to
every 10s Prometheus scrape in between -- there is no separate "cache" data
structure to keep in sync; the metric object *is* the cache.

Multi-machine labeling: every metric this bridge creates carries a
mandatory ``device_name`` label (see ``_MANDATORY_LABEL``). Two machines
reporting the same field name (e.g. "voltage") share one Prometheus metric
name (``device_voltage``) and are distinguished only by that label -- never
by minting per-machine metric names -- so PromQL queries and Grafana panels
work across the whole fleet without per-machine dashboard edits.

Dynamic typing: this bridge doesn't know ahead of time what fields a given
protocol will send, so metrics are created lazily, on first sight of a
field name, by ``DynamicMetricsRegistry`` -- see that class's docstring for
the Gauge vs. Counter vs. Histogram classification rules.

FastAPI integration: ``attach_metrics_route()`` mounts this bridge's
registry onto an existing FastAPI app (e.g. the WebServer UI already
running in this process) via ``prometheus_client.make_asgi_app()``. For
headless deployments with no WebServer running at all, set
``enable_standalone_server = true`` and this bridge runs its own tiny
uvicorn server on ``standalone_host:standalone_port`` instead.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import (
    make_asgi_app as _make_asgi_app,  # type: ignore[assignment]
)

from defs.common import TransportSettings

from .transport_base import transport_base

if TYPE_CHECKING:
    # Soft dependency only. This transport module must import cleanly even
    # in a headless deployment that has neither fastapi nor uvicorn
    # installed (enable_standalone_server=False, no WebServer attached --
    # the bridge just accumulates metrics nobody scrapes yet). Only used
    # for the type annotation on attach_metrics_route() below; never
    # evaluated at runtime under `from __future__ import annotations`.
    from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Every metric this bridge creates carries this label, always first,
# always populated -- see module docstring "Multi-machine labeling".
_MANDATORY_LABEL: str = "device_name"

_METRIC_NAME_RE: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9_:]")
_METRIC_NAME_LEADING_DIGIT_RE: re.Pattern[str] = re.compile(r"^[0-9]")


# ---------------------------------------------------------------------------
# Safe value coercion / name sanitizing
# ---------------------------------------------------------------------------

def _sanitize_metric_name(raw_name: str, prefix: str = "") -> str:
    """
    Scheduling path: N/A -- pure string utility, called from write_data().

    Converts an arbitrary protocol field name (e.g. "Battery Voltage (V)")
    into a Prometheus-legal metric name (e.g. "device_battery_voltage_v_").
    Prometheus metric names must match ``[a-zA-Z_:][a-zA-Z0-9_:]*`` -- every
    disallowed character becomes an underscore, a leading digit is prefixed
    with an underscore, and an empty result falls back to "unnamed_metric"
    rather than producing an invalid/empty registration.
    """
    cleaned: str = _METRIC_NAME_RE.sub("_", raw_name.strip())
    cleaned = _METRIC_NAME_LEADING_DIGIT_RE.sub(lambda m: f"_{m.group(0)}", cleaned)
    cleaned = cleaned.strip("_") or "unnamed_metric"
    full_name: str = f"{prefix}{cleaned}".lower()
    # A prefix could itself introduce a leading digit collision or double
    # underscores; re-run the leading-digit guard once more on the combined
    # name for safety (cheap, and prefixes are short/static per bridge).
    full_name = _METRIC_NAME_LEADING_DIGIT_RE.sub(lambda m: f"_{m.group(0)}", full_name)
    return full_name


def _sanitize_label_value(value: str, max_length: int) -> str:
    """
    Scheduling path: N/A -- pure string utility, called from write_data().

    Prometheus label values are just strings with no charset restriction,
    but pathologically long values (a raw device string dumped in as a
    label) bloat cardinality and the exposition payload -- truncate
    defensively.
    """
    trimmed: str = value.strip()
    if len(trimmed) > max_length:
        trimmed = trimmed[:max_length]
    return trimmed


def _coerce_metric_value(raw: Any) -> Optional[float]:
    """
    Scheduling path: N/A -- pure value-coercion utility, called from
    write_data() once per field in the incoming payload.

    Safely casts one raw scrape value to a Prometheus-safe float, or
    returns None to signal "skip this field" without raising. Handles
    every value type a protocol's decoded registers can realistically
    produce:

      * bool  -> 1.0 / 0.0.  Must be checked *before* the int/float branch
                 below, since ``bool`` is a subclass of ``int`` in Python
                 and would otherwise silently pass through as 1/0 anyway --
                 checking explicitly makes the intent visible rather than
                 relying on that subtyping accident.
      * int / float -> float(), rejecting NaN/Inf (a poisoned metric value
                 corrupts every PromQL aggregation that touches it, so it's
                 dropped here rather than exposed).
      * str / None / anything else -> None (skipped). Prometheus metrics
                 are strictly numeric; a raw string reading (firmware
                 version, status text, ...) has no numeric representation
                 and must not crash the collection loop.
    """
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        try:
            value: float = float(raw)
        except (TypeError, ValueError):
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return None


# ---------------------------------------------------------------------------
# Metric kind classification
# ---------------------------------------------------------------------------

class MetricKind(Enum):
    """Scheduling path: N/A -- classification result used by DynamicMetricsRegistry."""
    GAUGE = "gauge"
    COUNTER = "counter"
    HISTOGRAM = "histogram"


def _classify_metric_kind(
    field_name: str,
    *,
    counter_suffixes: tuple[str, ...],
    histogram_fields: frozenset[str],
) -> MetricKind:
    """
    Scheduling path: N/A -- called once per field, per machine, from
    DynamicMetricsRegistry.record() (cheap; the registry itself caches the
    created metric object so this only affects which bucket it's cached
    under, not per-scrape cost).

    Classification rules, most specific first:
      1. Explicit ``histogram_fields`` membership (opt-in via settings --
         a single scalar reading has no natural histogram semantics on its
         own, so this is never inferred automatically).
      2. Field name ends with one of ``counter_suffixes`` (default:
         ("_total",), following Prometheus's own naming convention for
         monotonic counters) -> Counter.
      3. Everything else -> Gauge, the safe default for arbitrary sensor
         readings that can legitimately go up or down.
    """
    if field_name in histogram_fields:
        return MetricKind.HISTOGRAM
    lowered: str = field_name.lower()
    if any(lowered.endswith(suffix) for suffix in counter_suffixes):
        return MetricKind.COUNTER
    return MetricKind.GAUGE


# ---------------------------------------------------------------------------
# Dynamic metric factory / registry
# ---------------------------------------------------------------------------

class DynamicMetricsRegistry:
    """
    Scheduling path: All scraper read cycles funnel through here via
    prometheus_out.write_data() -- this class itself has no threads of its
    own and does no I/O; it only mutates in-memory prometheus_client
    metric objects under a lock.

    Thread-safe factory/cache for Prometheus Gauge/Counter/Histogram
    objects keyed by sanitized metric name. A metric is created lazily the
    first time any machine reports a given field name, then reused (with a
    fresh ``device_name`` label value) for every subsequent write from any
    machine -- this sharing is what lets N machines report "voltage" under
    one clean metric name (``device_voltage``) instead of each machine
    minting its own metric name.

    Counter handling deserves a note: incoming values from write_data() are
    always *absolute* snapshots (a meter reading), never pre-computed
    deltas -- but prometheus_client's Counter type only exposes ``.inc()``,
    not ``.set()``. This registry bridges that gap itself: it remembers the
    last absolute value seen per (metric name, label tuple) and calls
    ``.inc(delta)`` with the difference. A decrease is treated as a counter
    reset (device reboot, meter rollover) -- logged at debug level and the
    new baseline is stored without incrementing, rather than raising or
    silently going negative (which Counter would reject outright).
    """

    def __init__(
        self,
        registry: CollectorRegistry,
        counter_suffixes: tuple[str, ...] = ("_total",),
        histogram_fields: frozenset[str] = frozenset(),
        histogram_buckets: tuple[float, ...] | None = None,
        metric_prefix: str = "device_",
        extra_labelnames: tuple[str, ...] = (),
        log: logging.Logger | None = None,
    ) -> None:
        self._registry: CollectorRegistry = registry
        self._counter_suffixes: tuple[str, ...] = counter_suffixes
        self._histogram_fields: frozenset[str] = histogram_fields
        self._histogram_buckets: tuple[float, ...] | None = histogram_buckets
        self._metric_prefix: str = metric_prefix
        # device_name is always first and always present -- see module
        # docstring "Multi-machine labeling". extra_labelnames (e.g.
        # "protocol") are appended after it.
        self._labelnames: tuple[str, ...] = (_MANDATORY_LABEL, *extra_labelnames)
        self._log: logging.Logger = log or logging.getLogger(__name__)

        self._lock: threading.Lock = threading.Lock()
        self._gauges: dict[str, Gauge] = {}
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        # (metric_name, label_value_tuple) -> last absolute value seen, for
        # Counter delta computation described in the class docstring.
        self._counter_last_value: dict[tuple[str, tuple[str, ...]], float] = {}

    def _label_tuple(self, label_values: dict[str, str]) -> tuple[str, ...]:
        return tuple(label_values.get(name, "") for name in self._labelnames)

    def _get_or_create(
        self, name: str, kind: MetricKind
    ) -> Gauge | Counter | Histogram:
        """
        Returns the cached metric object for `name`, creating it if this is
        the first time this exact (name, kind) pair has been seen.

        If `name` was already registered under a *different* kind (e.g. a
        protocol first sent it as a plain field, classified Gauge, and a
        later config change added it to histogram_fields), the mismatch is
        raised as a ValueError by the caller's registration attempt against
        prometheus_client's own registry (duplicate name, different type)
        -- record() catches and logs that rather than crashing the
        collection loop; see record() below.
        """
        if kind is MetricKind.GAUGE:
            gauge: Gauge | None = self._gauges.get(name)
            if gauge is None:
                gauge = Gauge(
                    name,
                    f"MPG device metric '{name}' (gauge).",
                    self._labelnames,
                    registry=self._registry,
                )
                self._gauges[name] = gauge
            return gauge
        if kind is MetricKind.COUNTER:
            counter: Counter | None = self._counters.get(name)
            if counter is None:
                counter = Counter(
                    name,
                    f"MPG device metric '{name}' (monotonic counter).",
                    self._labelnames,
                    registry=self._registry,
                )
                self._counters[name] = counter
            return counter
        histogram: Histogram | None = self._histograms.get(name)
        if histogram is None:
            kwargs: dict[str, Any] = {"registry": self._registry}
            if self._histogram_buckets is not None:
                kwargs["buckets"] = self._histogram_buckets
            histogram = Histogram(
                name,
                f"MPG device metric '{name}' (histogram).",
                self._labelnames,
                **kwargs,
            )
            self._histograms[name] = histogram
        return histogram

    def record(self, field_name: str, value: float, label_values: dict[str, str]) -> None:
        """
        Scheduling path: called once per numeric field, per write_data()
        call. Classifies the field, creates its metric on first sight, and
        applies the value using the operation appropriate to that metric's
        type (.set() / .inc(delta) / .observe()).

        Any exception from prometheus_client itself (e.g. a genuine
        name/type collision -- see _get_or_create's docstring) is caught
        and logged rather than propagated, so one malformed field can never
        abort the rest of a machine's write cycle.
        """
        name: str = _sanitize_metric_name(field_name, prefix=self._metric_prefix)
        kind: MetricKind = _classify_metric_kind(
            field_name,
            counter_suffixes=self._counter_suffixes,
            histogram_fields=self._histogram_fields,
        )
        labels: tuple[str, ...] = self._label_tuple(label_values)

        try:
            with self._lock:
                metric: Gauge | Counter | Histogram = self._get_or_create(name, kind)

                if kind is MetricKind.GAUGE:
                    assert isinstance(metric, Gauge)
                    metric.labels(*labels).set(value)

                elif kind is MetricKind.COUNTER:
                    assert isinstance(metric, Counter)
                    key: tuple[str, tuple[str, ...]] = (name, labels)
                    previous: float | None = self._counter_last_value.get(key)
                    if previous is None:
                        # First sample for this series -- establish the
                        # baseline without incrementing (delta unknown).
                        metric.labels(*labels)  # register the child series
                    elif value >= previous:
                        delta: float = value - previous
                        if delta > 0:
                            metric.labels(*labels).inc(delta)
                    else:
                        self._log.debug(
                            f"Counter '{name}' decreased ({previous} -> {value}) for "
                            f"labels {labels}; treating as a reset, not decrementing."
                        )
                    self._counter_last_value[key] = value

                else:
                    assert isinstance(metric, Histogram)
                    metric.labels(*labels).observe(value)

        except Exception as exc:
            self._log.debug(f"Skipped metric '{name}' ({kind.value}) for labels {labels}: {exc}")

    def metric_count(self) -> int:
        """Total number of distinct metric *names* currently registered (not series/label combinations)."""
        with self._lock:
            return len(self._gauges) + len(self._counters) + len(self._histograms)


# ---------------------------------------------------------------------------
# Per-machine target health state (backs the diagnostics dashboard)
# ---------------------------------------------------------------------------

@dataclass
class _MachineState:
    """
    Scheduling path: mutated only from write_data() (per-cycle) and the
    stale-monitor background thread (periodic); read from
    get_target_health() for the diagnostics dashboard. All access goes
    through prometheus_out._state_lock.
    """
    machine_id: str
    device_name: str = ""
    protocol_name: str = ""
    interval: float = 0.0
    last_scrape_timestamp: float = 0.0
    last_metric_count: int = 0
    last_skipped_count: int = 0
    total_writes: int = 0
    scrape_failures: int = 0
    currently_stale: bool = False
    first_seen: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# The bridge transport
# ---------------------------------------------------------------------------

class prometheus_out(transport_base):
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved) -- like every
    bridge, write_data() is called by the gateway at the end of each
    scraper's completed read cycle, regardless of which scheduling mode
    that scraper runs under.

    Prometheus (pull-model) output transport. See module docstring for the
    full architecture rationale.
    """

    transport_type = "bridge"

    # ------------------------------------------------------------------
    # Class-level attribute declarations (overridden in __init__)
    # ------------------------------------------------------------------

    # Metric naming / labeling
    metric_prefix: str = "device_"
    include_protocol_label: bool = True
    max_label_value_length: int = 128
    counter_metric_suffixes: str = "_total"      # comma-separated
    histogram_fields: str = ""                   # comma-separated, opt-in
    histogram_buckets: str = ""                  # comma-separated floats, optional override

    # FastAPI mount-in-existing-app path
    metrics_path: str = "/metrics"

    # Standalone server (for headless deployments with no WebServer attached)
    enable_standalone_server: bool = False
    standalone_host: str = "0.0.0.0"  # noqa: S104
    standalone_port: int = 9110

    # Staleness / target-health monitoring
    staleness_multiplier: float = 3.0
    stale_check_interval: float = 5.0

    def __init__(self, settings: TransportSettings) -> None:
        """
        Initialize the Prometheus transport bridge.

        Args:
            settings (TransportSettings): Configuration section containing
                metric-naming, labeling, and (optional) standalone-server
                options.

        Configuration options:
            - metric_prefix (str): Prefix applied to every device metric
              name (default: "device_"), e.g. field "voltage" ->
              "device_voltage".
            - include_protocol_label (bool): Add a second "protocol" label
              alongside the mandatory "device_name" label (default: True).
            - max_label_value_length (int): Truncate label values longer
              than this (default: 128).
            - counter_metric_suffixes (str): Comma-separated field-name
              suffixes treated as Prometheus Counters rather than Gauges
              (default: "_total").
            - histogram_fields (str): Comma-separated exact field names to
              record as Histograms instead of Gauges (default: "", i.e.
              none -- opt-in only, see DynamicMetricsRegistry).
            - histogram_buckets (str): Comma-separated float bucket
              boundaries for histogram fields (default: "", i.e. use
              prometheus_client's default buckets).
            - metrics_path (str): Path this bridge's metrics are served
              under when mounted into an existing FastAPI app via
              attach_metrics_route() (default: "/metrics").
            - enable_standalone_server (bool): Run this bridge's own
              uvicorn/FastAPI HTTP server instead of relying on an
              already-running WebServer to mount it (default: False).
            - standalone_host (str): Bind host for the standalone server
              (default: "0.0.0.0").
            - standalone_port (int): Bind port for the standalone server
              (default: 9110).
            - staleness_multiplier (float): A machine is flagged stale
              (and scrape_failures_total incremented) once this many
              multiples of its own read_interval have elapsed since its
              last write_data() call (default: 3.0).
            - stale_check_interval (float): Seconds between background
              staleness sweeps (default: 5.0).
            - device_name (str): Name for the bridge itself, used only in
              logs/notifications (default: "Prometheus MPG Bridge").

        Thread behavior:
            - Starts a lightweight background thread that periodically
              sweeps per-machine state for staleness (see
              _stale_monitor_loop).
            - Optionally starts a uvicorn server thread when
              enable_standalone_server is True.
            - All registry mutation is protected by locks internal to
              DynamicMetricsRegistry and this class's own _state_lock, so
              concurrent write_data() calls from multiple scraper threads
              (Concurrent/Interleaved scheduling) are safe.
        """
        super().__init__(settings)

        # -------------------------
        # Metric naming / labeling
        # -------------------------
        self.metric_prefix = settings.get("metric_prefix", fallback=self.metric_prefix)
        self.include_protocol_label = settings.getboolean(
            "include_protocol_label", fallback=self.include_protocol_label
        )
        self.max_label_value_length = settings.getint(
            "max_label_value_length", fallback=self.max_label_value_length
        )
        self.counter_metric_suffixes = settings.get(
            "counter_metric_suffixes", fallback=self.counter_metric_suffixes
        )
        self.histogram_fields = settings.get("histogram_fields", fallback=self.histogram_fields)
        self.histogram_buckets = settings.get("histogram_buckets", fallback=self.histogram_buckets)

        # -------------------------
        # FastAPI mount / standalone server
        # -------------------------
        self.metrics_path = settings.get("metrics_path", fallback=self.metrics_path)
        self.enable_standalone_server = settings.getboolean(
            "enable_standalone_server", fallback=self.enable_standalone_server
        )
        self.standalone_host = settings.get("standalone_host", fallback=self.standalone_host)
        self.standalone_port = settings.getint("standalone_port", fallback=self.standalone_port)

        # -------------------------
        # Staleness monitoring
        # -------------------------
        self.staleness_multiplier = settings.getfloat(
            "staleness_multiplier", fallback=self.staleness_multiplier
        )
        self.stale_check_interval = settings.getfloat(
            "stale_check_interval", fallback=self.stale_check_interval
        )

        # Bridge's own display name -- distinct from any single machine's
        # device_name label value. Re-read after super().__init__ so this
        # fallback wins regardless of transport_base's own device_name
        # resolution order (see influxdb_out / timescaledb for why this is
        # re-read here rather than trusted from the base class alone).
        self.device_name = settings.get("device_name", fallback="Prometheus MPG Bridge")
        # host/port surfaced for transport_base's connection-notification
        # log lines only; this bridge has no single "connection" to lose in
        # the way a database client does (see close()/self.connected below).
        self.host = self.standalone_host
        self.port = self.standalone_port

        # -------------------------
        # Runtime state
        # -------------------------
        self.registry: CollectorRegistry = CollectorRegistry()

        counter_suffixes: tuple[str, ...] = tuple(
            s.strip().lower() for s in self.counter_metric_suffixes.split(",") if s.strip()
        ) or ("_total",)
        histogram_field_set: frozenset[str] = frozenset(
            s.strip() for s in self.histogram_fields.split(",") if s.strip()
        )
        parsed_buckets: tuple[float, ...] | None = None
        if self.histogram_buckets.strip():
            try:
                parsed_buckets = tuple(
                    float(b.strip()) for b in self.histogram_buckets.split(",") if b.strip()
                )
            except ValueError:
                self._log.warning(
                    f"Could not parse histogram_buckets '{self.histogram_buckets}' as floats; "
                    "falling back to prometheus_client's default buckets."
                )

        extra_labelnames: tuple[str, ...] = ("protocol",) if self.include_protocol_label else ()

        self._metrics: DynamicMetricsRegistry = DynamicMetricsRegistry(
            registry=self.registry,
            counter_suffixes=counter_suffixes,
            histogram_fields=histogram_field_set,
            histogram_buckets=parsed_buckets,
            metric_prefix=self.metric_prefix,
            extra_labelnames=extra_labelnames,
            log=self._log,
        )

        # Internal loop-health telemetry -- labeled by machine_id per spec,
        # NOT by device_name, so it survives a machine's device_name being
        # edited in config without losing its history under the old label.
        self._scrape_duration: Gauge = Gauge(
            "mpg_prometheus_bridge_scrape_duration_seconds",
            "Time spent processing one write_data() cycle for a machine.",
            ("machine_id",),
            registry=self.registry,
        )
        self._last_scrape_timestamp: Gauge = Gauge(
            "mpg_prometheus_bridge_last_scrape_timestamp_seconds",
            "Unix timestamp of the last successful write_data() call for a machine.",
            ("machine_id",),
            registry=self.registry,
        )
        self._scrape_failures: Counter = Counter(
            "mpg_prometheus_bridge_scrape_failures_total",
            "Number of times a machine's data was flagged stale (missed its expected scrape interval).",
            ("machine_id",),
            registry=self.registry,
        )

        self._state_lock: threading.Lock = threading.Lock()
        self._machine_state: dict[str, _MachineState] = {}

        self._start_time: float = time.time()

        self._stop_event: threading.Event = threading.Event()
        self._stale_thread: threading.Thread = threading.Thread(
            target=self._stale_monitor_loop,
            name=f"{self.transport_name}-stale-monitor",
            daemon=True,
        )
        self._stale_thread.start()

        self._standalone_server: Any = None
        self._standalone_thread: threading.Thread | None = None
        if self.enable_standalone_server:
            self._start_standalone_server()

        # This bridge has no single external connection to lose the way a
        # database client does -- once the registry and (optional)
        # standalone server are up, it's ready to be scraped. Setting this
        # True (rather than leaving the base class default) avoids a
        # spurious "connection lost" notification on shutdown -- see the
        # connected-setter docs in transport_base.
        self.connected = True

    # ------------------------------------------------------------------
    # Machine identity
    # ------------------------------------------------------------------

    def _machine_id_for(self, from_transport: Any) -> str:
        """
        Scheduling path: called once per write_data()/init_bridge() call.

        Stable key for the target-health table and the machine_id-labeled
        internal telemetry gauges. Prefers transport_name (unique per
        configured transport section) over device_name (user-editable,
        not guaranteed unique) so a mid-run device_name edit doesn't
        fracture one machine's history across two rows.
        """
        transport_name: str = str(getattr(from_transport, "transport_name", "") or "")
        if transport_name:
            return transport_name
        device_name: str = str(getattr(from_transport, "device_name", "") or "")
        return device_name or "unknown_machine"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init_bridge(self, from_transport: transport_base) -> None:
        """
        Scheduling path: N/A -- setup, called once per attached scraper,
        before that scraper's first read cycle.

        Pre-registers the machine in _machine_state with whatever is known
        at wiring time (interval, protocol) so it appears on the
        diagnostics dashboard immediately as "never reported" rather than
        being invisible until its first successful write -- a machine that
        never manages a single successful scrape is exactly the case this
        dashboard exists to surface.
        """
        machine_id: str = self._machine_id_for(from_transport)
        with self._state_lock:
            if machine_id not in self._machine_state:
                self._machine_state[machine_id] = _MachineState(
                    machine_id=machine_id,
                    device_name=str(getattr(from_transport, "device_name", "") or machine_id),
                    protocol_name=str(getattr(from_transport, "protocol_name", "") or ""),
                    interval=float(getattr(from_transport, "read_interval", 0.0) or 0.0),
                )

    def write_data(self, data: dict[str, int | float | str ], from_transport: "transport_base") -> None:

        """
        Scheduling path: All (Sequential, Concurrent, Interleaved) -- called
        by the gateway once per completed read cycle for every scraper
        bridged to this transport.

        Casts each field to a Prometheus-safe float (skipping non-numeric
        values safely -- see _coerce_metric_value), records it into the
        in-memory registry via DynamicMetricsRegistry, and updates this
        machine's target-health state plus the internal loop-health gauges.
        Never raises: a malformed payload from one machine must never take
        down another machine's metrics or crash the calling scrape loop.
        """
        if not data:
            return

        start: float = time.perf_counter()
        machine_id: str = self._machine_id_for(from_transport)
        device_name: str = str(getattr(from_transport, "device_name", "") or machine_id)
        protocol_name: str = str(getattr(from_transport, "protocol_name", "") or "")

        label_values: dict[str, str] = {
            _MANDATORY_LABEL: _sanitize_label_value(device_name, self.max_label_value_length),
        }
        if self.include_protocol_label:
            label_values["protocol"] = _sanitize_label_value(protocol_name, self.max_label_value_length)

        written: int = 0
        skipped: int = 0
        for field_name, raw_value in data.items():
            value: float | None = _coerce_metric_value(raw_value)
            if value is None:
                skipped += 1
                continue
            self._metrics.record(field_name, value, label_values)
            written += 1

        duration: float = time.perf_counter() - start
        now: float = time.time()
        interval: float = float(getattr(from_transport, "read_interval", 0.0) or 0.0)

        with self._state_lock:
            state: _MachineState | None = self._machine_state.get(machine_id)
            if state is None:
                state = _MachineState(machine_id=machine_id)
                self._machine_state[machine_id] = state
            state.device_name = device_name
            state.protocol_name = protocol_name
            if interval:
                state.interval = interval
            state.last_scrape_timestamp = now
            state.last_metric_count = written
            state.last_skipped_count = skipped
            state.total_writes += 1
            state.currently_stale = False

        self._scrape_duration.labels(machine_id=machine_id).set(duration)
        self._last_scrape_timestamp.labels(machine_id=machine_id).set(now)

        self._log.debug(
            f"[{machine_id}] recorded {written} metric(s), skipped {skipped} "
            f"non-numeric field(s), in {duration * 1000:.1f}ms."
        )

    # ------------------------------------------------------------------
    # Staleness / target-health monitoring
    # ------------------------------------------------------------------

    def _stale_monitor_loop(self) -> None:
        """
        Scheduling path: N/A -- own background thread, independent of every
        scraper's read_interval.

        Periodically sweeps every known machine and flags one "stale" the
        moment it's gone longer than (interval * staleness_multiplier)
        since its last successful write_data() call, incrementing
        scrape_failures_total exactly once per stale *transition* (not
        once per sweep) -- a machine that's been offline for an hour counts
        as one ongoing failure, not 720 of them at a 5s sweep interval.
        A machine with interval == 0 (never yet reported, or a scraper
        that hasn't loaded a read_interval) is skipped -- there is no
        expected cadence to judge it against yet.
        """
        while not self._stop_event.wait(self.stale_check_interval):
            now: float = time.time()
            with self._state_lock:
                for state in self._machine_state.values():
                    if state.interval <= 0 or state.last_scrape_timestamp <= 0:
                        continue
                    threshold: float = state.interval * self.staleness_multiplier
                    elapsed: float = now - state.last_scrape_timestamp
                    if elapsed > threshold and not state.currently_stale:
                        state.currently_stale = True
                        state.scrape_failures += 1
                        self._scrape_failures.labels(machine_id=state.machine_id).inc()
                        self._log.warning(
                            f"[{state.machine_id}] no data for {elapsed:.0f}s "
                            f"(expected every {state.interval:.0f}s) -- flagged stale."
                        )

    def get_target_health(self) -> list[dict[str, Any]]:
        """
        Scheduling path: N/A -- read-only snapshot for the diagnostics
        dashboard (routers/pages.py Prometheus target-health partial).

        Returns one row per machine this bridge has ever been wired to or
        received data from:
          {machine_id, device_name, protocol_name, connected, interval,
           scrape_failures_total, last_scrape_timestamp, last_metric_count,
           last_skipped_count, total_writes}

        `connected` is True only when the machine has reported at least
        once AND is within its own staleness threshold right now --
        matches the state the background monitor uses, so the dashboard
        never disagrees with what actually drives scrape_failures_total.
        A machine with interval == 0 is reported connected purely on
        "has it ever sent anything", since there's no cadence to judge
        staleness against.
        """
        now: float = time.time()
        rows: list[dict[str, Any]] = []
        with self._state_lock:
            for state in sorted(self._machine_state.values(), key=lambda s: s.machine_id):
                has_reported: bool = state.last_scrape_timestamp > 0
                if not has_reported:
                    connected = False
                elif state.interval > 0:
                    connected = (now - state.last_scrape_timestamp) <= (state.interval * self.staleness_multiplier)
                else:
                    connected = True
                rows.append({
                    "machine_id": state.machine_id,
                    "device_name": state.device_name,
                    "protocol_name": state.protocol_name,
                    "connected": connected,
                    "interval": state.interval,
                    "scrape_failures_total": state.scrape_failures,
                    "last_scrape_timestamp": state.last_scrape_timestamp or None,
                    "last_metric_count": state.last_metric_count,
                    "last_skipped_count": state.last_skipped_count,
                    "total_writes": state.total_writes,
                })
        return rows

    def get_bridge_summary(self) -> dict[str, Any]:
        """
        Scheduling path: N/A -- read-only snapshot for the diagnostics
        dashboard's summary card.

        Returns:
          {metrics_registered, total_machines, connected_count,
           stale_count, never_reported_count, standalone_server_enabled,
           standalone_server_running, metrics_path, standalone_host,
           standalone_port, uptime_seconds}
        """
        targets: list[dict[str, Any]] = self.get_target_health()
        connected_count: int = sum(1 for t in targets if t["connected"])
        never_reported_count: int = sum(1 for t in targets if t["last_scrape_timestamp"] is None)
        stale_count: int = len(targets) - connected_count - never_reported_count

        server_running: bool = bool(
            self._standalone_thread is not None and self._standalone_thread.is_alive()
        )

        return {
            "metrics_registered": self._metrics.metric_count(),
            "total_machines": len(targets),
            "connected_count": connected_count,
            "stale_count": stale_count,
            "never_reported_count": never_reported_count,
            "standalone_server_enabled": self.enable_standalone_server,
            "standalone_server_running": server_running,
            "metrics_path": self.metrics_path,
            "standalone_host": self.standalone_host,
            "standalone_port": self.standalone_port,
            "uptime_seconds": time.time() - self._start_time,
        }

    # ------------------------------------------------------------------
    # FastAPI / standalone server integration
    # ------------------------------------------------------------------

    def get_asgi_app(self) -> Any:
        """
        Returns a Starlette ASGI app serving this bridge's in-memory
        registry, suitable for ``app.mount(path, bridge.get_asgi_app())``.
        See module-level attach_metrics_route() for the one-line helper
        most callers want instead of calling this directly.
        """
        return _make_asgi_app(registry=self.registry) # type: ignore

    def _start_standalone_server(self) -> None:
        """
        Scheduling path: N/A -- setup, runs once during __init__ when
        enable_standalone_server is True.

        Runs a minimal FastAPI app (this bridge's /metrics only, plus a
        trivial /healthz) under its own uvicorn server on a background
        thread, for deployments where no WebServer/FastAPI app is already
        running to mount into. Soft-imports fastapi/uvicorn so this module
        still imports cleanly when those packages aren't installed and
        this feature simply isn't used.
        """
        try:
            import uvicorn
            from fastapi import FastAPI
        except ImportError as exc:
            self._log.error(
                f"enable_standalone_server=True but fastapi/uvicorn are not "
                f"installed ({exc}); the standalone metrics server will not start. "
                f"Install them, or mount this bridge into an existing FastAPI app "
                f"instead via attach_metrics_route()."
            )
            return

        app: FastAPI = FastAPI(
            title=f"MPG Prometheus Bridge ({self.transport_name})",
            docs_url=None,
            redoc_url=None,
        )
        attach_metrics_route(app, self, self.metrics_path)

        @app.get("/healthz")
        def _healthz() -> dict[str, Any]:
            return {"status": "ok", "machines_tracked": len(self._machine_state)}

        config: Any = uvicorn.Config(
            app,
            host=self.standalone_host,
            port=self.standalone_port,
            log_level="warning",
        )
        server: Any = uvicorn.Server(config)
        self._standalone_server = server

        def _run() -> None:
            try:
                server.run()
            except Exception as exc:
                self._log.error(f"Standalone Prometheus metrics server crashed: {exc}")

        thread: threading.Thread = threading.Thread(
            target=_run,
            name=f"{self.transport_name}-metrics-http",
            daemon=True,
        )
        self._standalone_thread = thread
        thread.start()
        self._log.info(
            f"Standalone Prometheus metrics server starting on "
            f"http://{self.standalone_host}:{self.standalone_port}{self.metrics_path}"
        )

    # ------------------------------------------------------------------
    # Close / cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Gracefully terminate this bridge. Stops the stale-monitor thread
        and, if running, the standalone uvicorn server. There is no
        network connection or write batch to flush -- the registry simply
        stops being updated; any still-mounted /metrics endpoint (embedded
        in a WebServer app that outlives this bridge) will keep serving
        the last values it had until that app itself shuts down.
        """
        self._log.debug(f"Closing Prometheus transport bridge '{self.transport_name}'...")

        self._stop_event.set()
        if self._stale_thread.is_alive():
            self._stale_thread.join(timeout=5.0)

        server: Any = getattr(self, "_standalone_server", None)
        if server is not None:
            try:
                server.should_exit = True
                thread: threading.Thread | None = getattr(self, "_standalone_thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=5.0)
            except Exception as exc:
                self._log.warning(f"Error stopping standalone metrics server: {exc}")
            finally:
                self._standalone_server = None

        self.connected = False
        self._log.info(f"Prometheus transport bridge '{self.transport_name}' closed cleanly.")

    def __del__(self) -> None:
        try:
            if hasattr(self, "close") and callable(getattr(self, "close", None)):
                self.close()
        except Exception as e:
            if hasattr(self, "_log"):
                try:
                    self._log.error(f"Exception in __del__: {e}")
                except Exception:
                    self._log.error(f"Exception in __del__: {e}")


# ---------------------------------------------------------------------------
# Module-level FastAPI helper
# ---------------------------------------------------------------------------

def attach_metrics_route(app: "FastAPI", bridge: "prometheus_out", path: str | None = None) -> None:
    """
    Mounts `bridge`'s in-memory metrics registry onto an existing FastAPI
    (or any Starlette-compatible) `app` at `path` (default:
    ``bridge.metrics_path``), using ``prometheus_client.make_asgi_app()``.

    This is the "clean setup helper" for the common case -- a WebServer
    FastAPI app that's already running attaches this bridge's /metrics
    endpoint onto itself with one call, e.g. from main.py's app-assembly
    code:

        from classes.transports.prometheus_out import attach_metrics_route
        bridge = get_prometheus_bridge(gateway, "transport.prometheus_out")
        if bridge is not None:
            attach_metrics_route(app, bridge)

    Raises whatever ``app.mount()`` raises (e.g. Starlette's error on a
    duplicate mount path) -- callers mounting more than one Prometheus
    bridge into the same app must give each a distinct `path` /
    `metrics_path`.
    """
    mount_path: str = path or bridge.metrics_path
    app.mount(mount_path, bridge.get_asgi_app())
