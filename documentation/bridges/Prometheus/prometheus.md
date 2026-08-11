# Prometheus Output Transport

The Prometheus output transport exposes data from your devices as a `/metrics` endpoint for a Prometheus server to scrape. Unlike the InfluxDB or TimescaleDB transports, this is a **pull-model** transport: instead of pushing batches out over the network on a schedule it controls, this bridge just keeps an in-memory metrics registry up to date, and an external Prometheus server decides when to read it.

## How It Differs From Push Transports

Every other output transport in this codebase (`influxdb_out`, `timescaledb`, `mqtt`) is a *push* transport — the gateway's own worker loops decide when data leaves the process, batch it, and send it over the network. Prometheus inverts that:

- **Background (this bridge's job)**: every scraper's `write_data()` call — one per completed read cycle, at whatever `read_interval` that scraper is configured with — updates an in-memory `prometheus_client` registry. This is pure in-process object mutation. No network I/O, no batching, nothing to reconnect.
- **External I/O (the Prometheus server's job)**: a Prometheus server scrapes `/metrics` on its own schedule and reads straight out of that same in-memory registry.

Because each metric object retains whatever value was last set, a slow 60-second-interval device simply keeps re-serving its last known reading to every 10-second Prometheus scrape in between — there's no separate cache to keep in sync; the metric object *is* the cache.

This also means there's no connection to lose, no reconnect logic, and no backlog/persistent-storage story the way the InfluxDB transport has — if nobody scrapes `/metrics` for a while, values just sit in memory until someone does.

## Features

- **Pull-model exposition**: serves a standard `/metrics` endpoint for any Prometheus-compatible scraper
- **Dynamic metric typing**: creates Gauge, Counter, or Histogram metrics lazily on first sight of a field name — no metric schema to predefine
- **Multi-machine labeling**: every machine reporting the same field name (e.g. `voltage`) shares one metric name (`device_voltage`), distinguished by a `device_name` label — so PromQL queries and Grafana panels work across a whole fleet without per-machine edits
- **Automatic name sanitizing**: arbitrary protocol field names (e.g. `"Battery Voltage (V)"`) are converted to Prometheus-legal metric names (e.g. `device_battery_voltage_v_`)
- **Safe value coercion**: numeric fields are converted to floats; non-numeric fields (strings, `None`, NaN/Inf) are skipped without crashing the write cycle
- **Counter delta handling**: counter fields report absolute snapshots (like a meter reading), and the bridge computes the `.inc()` delta itself, treating a decrease as a device reset rather than an error
- **Target-health tracking**: tracks per-machine staleness, scrape counts, and failure counts for a diagnostics dashboard

## Configuration

### Basic Configuration

```ini
[prometheus_output]
type = prometheus_out
metric_prefix = device_
metrics_path = /metrics
```

### Advanced Configuration

```ini
[prometheus_output]
type = prometheus_out

# Metric naming / labeling
metric_prefix = device_
include_protocol_label = true
max_label_value_length = 128
counter_metric_suffixes = _total
histogram_fields =
histogram_buckets =

# Mount path (when attached to an existing FastAPI app)
metrics_path = /metrics

# Standalone server (headless deployments only)
enable_standalone_server = false
standalone_host = 0.0.0.0
standalone_port = 9110

# Staleness / target-health monitoring
staleness_multiplier = 3.0
stale_check_interval = 5.0
```

### Configuration Options

| Option | Default | Description |
| --- | --- | --- |
| `metric_prefix` | `device_` | Prefix applied to every device metric name, e.g. field `voltage` → `device_voltage` |
| `include_protocol_label` | `true` | Add a second `protocol` label alongside the mandatory `device_name` label |
| `max_label_value_length` | `128` | Truncate label values longer than this, to avoid bloating cardinality with raw device strings |
| `counter_metric_suffixes` | `_total` | Comma-separated field-name suffixes treated as Prometheus Counters rather than Gauges |
| `histogram_fields` | `` (none) | Comma-separated exact field names to record as Histograms instead of Gauges — opt-in only, never inferred automatically |
| `histogram_buckets` | `` (default buckets) | Comma-separated float bucket boundaries for histogram fields |
| `metrics_path` | `/metrics` | Path this bridge's metrics are served under when mounted into an existing FastAPI app |
| `enable_standalone_server` | `false` | Run this bridge's own uvicorn/FastAPI HTTP server instead of relying on an already-running WebServer to mount it |
| `standalone_host` | `0.0.0.0` | Bind host for the standalone server |
| `standalone_port` | `9110` | Bind port for the standalone server |
| `staleness_multiplier` | `3.0` | A machine is flagged stale once this many multiples of its own `read_interval` have elapsed since its last write |
| `staleness_check_interval` | `5.0` | Seconds between background staleness sweeps |
| `device_name` | `Prometheus MPG Bridge` | Name for the bridge itself, used only in logs/notifications — distinct from any individual machine's `device_name` label value |

## Metric Type Classification

The bridge doesn't know ahead of time what fields a given protocol will send, so it classifies each field the first time it's seen, most specific rule first:

1. **Histogram** — if the field name is explicitly listed in `histogram_fields`. A single scalar reading has no natural histogram semantics on its own, so this is never inferred automatically.
2. **Counter** — if the field name ends with one of `counter_metric_suffixes` (default `_total`), following Prometheus's own naming convention for monotonic counters.
3. **Gauge** — everything else. This is the safe default for arbitrary sensor readings that can legitimately go up or down.

Once a field is classified, that metric object is reused for every future write from any machine — a name/type mismatch (e.g. a config change that later adds an already-seen field to `histogram_fields`) is caught and logged rather than crashing the collection loop.

### Counter Semantics

Prometheus's `Counter` type only supports `.inc()`, not `.set()`, but incoming values are always absolute snapshots (like a meter reading), never pre-computed deltas. The bridge bridges that gap itself:

- It remembers the last absolute value seen per metric+label combination.
- On each write, it computes `delta = new_value - previous_value` and calls `.inc(delta)`.
- If the new value is *lower* than the previous one, that's treated as a counter reset (device reboot, meter rollover) — logged at debug level, with the new value stored as the fresh baseline. Nothing is decremented and nothing raises.

## Data Structure

### Labels

Every metric this bridge creates carries:

- `device_name` — always present, always first. This is what distinguishes two machines reporting the same field name under one shared metric.
- `protocol` — included only if `include_protocol_label = true` (default).

### Metric Names

Field names are sanitized to Prometheus's legal character set (`[a-zA-Z_:][a-zA-Z0-9_:]*`): disallowed characters become underscores, a leading digit gets an underscore prefix, and an empty result falls back to `unnamed_metric`. The configured `metric_prefix` is prepended before this sanitizing is finalized.

### Internal Bridge Telemetry

Alongside device metrics, the bridge exposes its own health metrics, labeled by `machine_id` (the scraper's `transport_name`, not `device_name`, so history survives a `device_name` edit):

- `mpg_prometheus_bridge_scrape_duration_seconds` — time spent processing one `write_data()` cycle for a machine
- `mpg_prometheus_bridge_last_scrape_timestamp_seconds` — Unix timestamp of a machine's last successful write
- `mpg_prometheus_bridge_scrape_failures_total` — count of stale *transitions* for a machine (an hour-long outage counts once, not once per staleness sweep)

## Example Bridge Configuration

```ini
# Source device (e.g., Modbus RTU)
[growatt_inverter]
type = modbus_rtu
port = /dev/ttyUSB0
baudrate = 9600
protocol_version = growatt_2020_v1.24
device_serial_number = 123456789
device_manufacturer = Growatt
device_model = SPH3000
read_interval = 10
bridge = prometheus_output

# Prometheus output
[prometheus_output]
type = prometheus_out
metric_prefix = device_
metrics_path = /metrics
```

## Installation  (if not using docker)

1. Install the required dependency:

   ```bash
   pip install prometheus_client
   ```

2. Or add to your requirements.txt:

   ```ini
   prometheus_client
   ```

This uses `prometheus_client.make_asgi_app()` under the hood to mount at `bridge.metrics_path` (default `/metrics`). If you mount more than one Prometheus bridge into the same app, give each a distinct `metrics_path`.

## Prometheus Server Setup

Point a Prometheus server's scrape config at whichever host/port is serving this bridge's endpoint:

```yaml
scrape_configs:
  - job_name: "mpg_devices"
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:9110"]
    metrics_path: /metrics
```

Set `scrape_interval` to something reasonable relative to your fastest device's `read_interval` — scraping faster than data changes just re-reads the same cached values.

## Querying Data

Once Prometheus is scraping successfully, query with PromQL:

```promql
# Current value of a gauge across all devices
device_battery_voltage

# A specific machine
device_battery_voltage{device_name="inverter_1"}

# Rate of increase for a counter field over 5 minutes
rate(device_energy_total[5m])

# Which machines have gone stale
mpg_prometheus_bridge_scrape_failures_total
```

## Integration with Grafana

1. Add Prometheus as a data source in Grafana, pointing at your Prometheus server (not this bridge directly)
2. Build panels using PromQL, filtering/grouping by the `device_name` label to compare machines
3. Because every machine shares metric names, one dashboard panel works across your whole fleet with no per-machine edits

## Troubleshooting

### No Data Appearing in Prometheus

- Confirm the endpoint is reachable: `curl http://<host>:<port>/metrics`
- Check whether the bridge is mounted (Option A) or running standalone (Option B) — a bridge with `enable_standalone_server = false` and no `attach_metrics_route()` call has nowhere to be scraped from
- Verify your Prometheus server's `scrape_configs` target and `metrics_path` match this bridge's actual host/port/`metrics_path`
- Check Prometheus's own "Targets" page (`<prometheus>:9090/targets`) for scrape errors

### A Field Isn't Showing Up

- Non-numeric fields (strings, `None`) are silently skipped — check `last_skipped_count` in the target-health data, or enable debug logging to see per-field skip messages
- NaN/Inf values are also dropped rather than exposed, to avoid poisoning PromQL aggregations

### A Machine Shows as Stale / `connected: false`

- Staleness is judged as `elapsed_since_last_write > read_interval * staleness_multiplier`. A slow-polling device with a long `read_interval` needs proportionally longer before it's flagged — this is expected, not a bug
- A machine with `read_interval == 0` (never loaded a read interval) is never flagged stale; it's reported connected purely on whether it has ever sent anything
- Check `mpg_prometheus_bridge_scrape_failures_total{machine_id="..."}` for a history of stale transitions

### Duplicate Metric Name / Type Collision

**Symptoms:** a debug log line like `Skipped metric '...' (gauge) for labels ...: <error>`

**Cause:** the same field name was first seen as one metric kind (e.g. Gauge) and a later config change (e.g. adding it to `histogram_fields`) tries to register it under a different kind. Prometheus doesn't allow one name to back two types.

**Solution:** pick one classification for a given field name and keep `histogram_fields` / `counter_metric_suffixes` consistent across restarts, or rename the field.

### High Label Cardinality

**Symptoms:** slow scrapes, large `/metrics` payloads, high Prometheus memory usage

**Solutions:**

- Lower `max_label_value_length` if raw device strings are leaking into the `device_name` or `protocol` label
- Keep `device_name` values stable and human-scale (a handful of machines, not one label value per reading)
- Avoid feeding high-cardinality data (raw timestamps, unique IDs) into fields that get exposed as metrics

## Comparison With the Push Transports

| | InfluxDB / TimescaleDB (push) | Prometheus (pull) |
| --- | --- | --- |
| Who decides transmission timing | The gateway, on `batch_size`/`batch_timeout` | The external Prometheus server's scrape interval |
| Network I/O in the bridge itself | Yes — outbound writes, with retry/backoff | No — the bridge only serves inbound HTTP reads |
| Data loss during an outage | Backlog/persistent storage needed to avoid it | None to lose — the metric just holds its last value until scraped |
| Reconnection logic | Yes (exponential backoff, periodic reconnect) | Not applicable — there's no connection to the destination to lose |
| "Is data flowing" signal | Reconnect/backlog logs | Target-health state + `mpg_prometheus_bridge_*` metrics |
