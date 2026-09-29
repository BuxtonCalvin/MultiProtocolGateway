# InfluxDB Bridge

The InfluxDB bridge lets MPG send device data to an InfluxDB v1 or v3 server for time-series storage and visualization, and gives admins tools to correct historical data and shift date ranges. This document covers how data travels from a device to InfluxDB, the two output transports (`influxdb_out` for v1 and `influxdb3_out` for v3), their reliability features, the admin UI's Metrics Edit and Timeshift Data screens, and troubleshooting for all of the above.

## Table of Contents

1. [How Data Flows Through MPG](#how-data-flows-through-mpg)
2. [Output Transport](#output-transport)
3. [Advanced Features: Reconnection, Stale Detection & Persistent Backlog](#advanced-features-reconnection-stale-detection--persistent-backlog)
4. [Metrics Edit — Editing or Deleting Historical Metric Values](#metrics-edit--editing-or-deleting-historical-metric-values)
5. [Time-Shifted Data Import / Export](#time-shifted-data-import--export)
6. [Troubleshooting](#troubleshooting)

See also: [InfluxDB3.md](InfluxDB3.md) for a Docker-based InfluxDB 3 Core setup, and [config.influxdb.example](config.influxdb.example) for a sample bridge config.

---

## How Data Flows Through MPG

Understanding the path a value takes from a device to an InfluxDB point explains most of the behaviors (and most of the troubleshooting) described later in this document.

### 1. Configuration and startup

MPG's `config.cfg` is made of sections. Every section that has a `transport =` key (or whose name starts with `transport`) becomes a live transport object when the gateway starts:

- A **scraper** section (for example `transport = modbus_tcp`) reads a device on a `read_interval`.
- A **bridge** section (`transport = influxdb_out` or `transport = influxdb3_out`) receives data from scrapers. A bridge has no `read_interval` of its own.
- A scraper is linked to one or more bridges by naming the bridge section(s) in its `bridge =` setting (comma separated for more than one). The value must match the bridge section's name exactly, for example `bridge = transport.influxdb`.

At startup the gateway constructs every transport, calls `connect()` on each, and then wires the bridge links. For an InfluxDB bridge, `connect()` creates the client, verifies the server is reachable, and (if a backlog file from a previous run exists) immediately tries to flush it. If the server is unreachable at startup the bridge simply starts in the "not connected" state and buffers data until a later reconnect succeeds.

You can edit `config.cfg` by hand or use the admin UI. The admin UI stages changes and applies them with **Commit All Changes**, which writes `config.cfg` and then reloads the running gateway (see [Admin UI and configuration changes](#admin-ui-and-configuration-changes) below).

### 2. Reading and routing

On every tick the gateway checks each scrape group and reads any whose `read_interval` has elapsed. It then routes results to bridges:

1. The scraper decodes a full read cycle into one dictionary of `variable_name -> value`.
2. The gateway filters that dictionary for each member using its **variable mask** (or, if there is no mask, the variables defined in its protocol registry map). Fields synthesized by the transport (derived metrics) are always kept.
3. For each bridge named in the scraper's `bridge =` setting, the gateway calls `bridge.write_data(member_data, scraper)`.

Details that matter for InfluxDB:

- **One point per device per cycle.** Each cycle produces a single wide point containing every forwarded variable as a field, not one point per variable.
- **Write cadence is the scraper's `read_interval`.** The InfluxDB bridge never polls anything itself; if a device is not read, nothing is written.
- **Partial cycles are still written.** Unlike the TimescaleDB bridge (which sets `write_requires_complete_cycle`), the InfluxDB bridges accept partial data, so a cycle cut short by timeouts can produce a point with fewer fields.
- **Empty reads are dropped before the bridge.** If a device returns no data, the gateway logs a warning and never calls the bridge for that cycle.
- **Serial-frame scrapers** (which decode frames as they arrive) can additionally hand the gateway one variable at a time; each such call reaches the bridge as a single-field write.

### 3. Inside the bridge

`write_data()` in `influxdb_out` / `influxdb3_out` does the following for each call:

1. Runs [stale data detection](#stale-data-detection) for the source transport (this happens even when InfluxDB is offline).
2. Calls `_check_connection()`, which may ping/probe the server and reconnect (see [Connection Monitoring](#connection-monitoring)).
3. **Online:** builds a point (measurement, tags, fields, timestamp) and appends it to an in-memory batch. The batch is written when a flush condition is met (see [Batching](#batching)).
4. **Offline:** builds the same point and stores it in the persistent backlog (if `enable_persistent_storage` is on; otherwise the point is discarded with the log line `Persistent storage disabled, data will be lost`). Any points still waiting in the in-memory batch are moved into the backlog first, so every point is stored once and in capture order.

The point's timestamp is the time the bridge received the data (nanoseconds since the epoch), not a time reported by the device.

### 4. Saving to InfluxDB

| | `influxdb_out` (InfluxDB v1) | `influxdb3_out` (InfluxDB v3) |
| --- | --- | --- |
| Python client | `influxdb` (`InfluxDBClient`) | `influxdb3-python` (`InfluxDBClient3`) |
| Authentication | `username` / `password` | `token` (plus `org` for Cloud) |
| Write call | `write_points(list_of_dicts)` | `write(record=[Point, ...], database=...)` |
| Connectivity check | `ping()` | SQL query `SELECT 1 FROM information_schema.tables LIMIT 1` |
| Database creation | Created automatically if missing | Created by the server on first write (see `auto_create_database`) |
| Default port | 8086 | 8181 |
| Backlog file | `influxdb_backlog_<section name>.pkl` | `influxdb3_backlog_<section name>.pkl` |

Everything else (point construction, batching, reconnect logic, stale detection, backlog handling) is implemented identically in both transports.

### 5. What the web UI does with the bridge

The admin UI never sits in the write path. It interacts with an InfluxDB bridge in four ways:

- **Device page for the bridge** — shows a **Bridge Health** panel (batch pending, backlog, periodic reconnect status, stale transports) and a **Storage Overview** panel (see [Bridge Health and Storage Overview panels](#bridge-health-and-storage-overview-panels)). Both are read-only and are looked up by the bridge's section name, so they work with several InfluxDB bridges configured.
- **Metrics Edit** and **Timeshift Data** — query and write InfluxDB directly through the running bridge's own client connection. See those sections below. These screens use the **first** `influxdb_out` (v1) and the **first** `influxdb3_out` (v3) bridge found on the gateway, so if you configure more than one bridge of the same version, only the first is used.
- **Nav menu** — the InfluxDB menu appears only when at least one InfluxDB bridge is loaded, and each version's entries appear only when a bridge of that version is loaded.
- **Settings** — the UI's default values for each bridge setting come from the transport defaults shipped with the app; the settings you change are staged until committed.

#### Admin UI and configuration changes

Bridge settings edited in the UI are staged until you press **Commit All Changes**. A commit writes `config.cfg`, then reloads the gateway: the old gateway is stopped, every transport is rebuilt from the new file, and every transport reconnects. For an InfluxDB bridge this means:

- Its connection is re-established, and any backlog file on disk is loaded again and flushed on the first successful connect.
- Points still waiting in the in-memory batch when the old gateway is stopped are saved. The gateway calls each bridge's `cleanup()`, which writes the pending batch to InfluxDB if the bridge is connected, or stores it in the backlog file if not. It never attempts a reconnect at that point, so a reload or shutdown is not held up by backoff waits. The bridge then closes its client. (A hard kill or crash can still lose points that were only in memory.)

Metrics Edit and Timeshift operations are **not** part of the config commit; Metrics Edit changes are applied by **Commit All Changes** (see that section), and Timeshift imports write immediately.

---

## Output Transport

The InfluxDB output transports send data from your devices to an InfluxDB server. `influxdb_out` targets InfluxDB v1.x; `influxdb3_out` targets InfluxDB 3 (Core, Enterprise, or Cloud). Both share the same behavior except where noted.

### Features

- **Batch writing**: points are collected and written together (see [Batching](#batching)).
- **Automatic database creation**: v1 creates the database if it does not exist; v3 relies on the server creating it on the first write.
- **Device information tags**: device metadata is written as tags for easy querying and filtering.
- **Type handling**: numeric values are stored as floats by default; text and enumerated values are stored as strings (see [Fields](#fields)).
- **Connection monitoring**: connection checks and reconnection with optional exponential backoff.
- **Persistent backlog**: points that cannot be delivered are saved to disk and replayed after reconnection.
- **Stale data detection**: detects a device that keeps returning identical values and asks the gateway to reconnect it.
- **Admin UI integration**: Bridge Health and Storage Overview panels, plus the Metrics Edit and Timeshift screens.

### Configuration

Bridge settings go in a section with `transport = influxdb_out` (v1) or `transport = influxdb3_out` (v3). The section name is up to you (the admin UI uses names like `transport.influxdb`); scrapers refer to it by that exact name in `bridge =`.

#### Basic configuration (InfluxDB v1)

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
measurement = device_data
```

#### Basic configuration (InfluxDB v3)

```ini
[transport.influxdb3]
transport = influxdb3_out
host = http://localhost
port = 8181
database = solar
token = apiv3_your_token_here
measurement = device_data
```

For v3, `host` may include a scheme (`http://` or `https://`); if you omit it, `http://` is assumed, so use `https://` explicitly for TLS or InfluxDB Cloud. A port embedded in `host` (for example `http://myhost:8181`) is honored, and a separate `port` setting overrides it. `org` is only sent when set, and is needed only for InfluxDB Cloud deployments that require it.

#### Advanced configuration (InfluxDB v1)

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
username = admin
password = your_password
measurement = device_data
include_timestamp = true
include_device_info = true
force_float = true
batch_size = 100
batch_timeout = 10.0
log_level = INFO

# Connection monitoring
reconnect_attempts = 5
reconnect_delay = 5.0
connection_timeout = 10
use_exponential_backoff = true
max_reconnect_delay = 300.0
periodic_reconnect_interval = 14400.0

# Persistent backlog
enable_persistent_storage = true
persistent_storage_path = backlogs
max_backlog_size = 10000
max_backlog_age = 86400

# Stale data detection
stale_data_timeout = 300
max_stale_attempts = 3
retry_delay_mins = 5
```

#### Configuration options

Options marked **v1** or **v3** apply to only that transport; all others apply to both.

| Option | Default | Description |
| --- | --- | --- |
| `host` | `localhost` (v1); an InfluxDB Cloud URL (v3) | Server hostname or IP. **v3:** may include `http://` / `https://` (a bare hostname gets `http://`) and an optional `:port`. Always set it explicitly for v3, because the built-in default points at InfluxDB Cloud. |
| `port` | `8086` (v1) / empty (v3) | Server port. **v3:** if set, overrides a port embedded in `host`. |
| `database` | `solar` | Database name. |
| `username`, `password` | empty | **v1:** authentication (optional). Not used by v3. |
| `token` | empty | **v3:** API token used for writes and queries. |
| `org` | empty | **v3:** organization; only sent when set (InfluxDB Cloud). |
| `auto_create_database` | `true` | **v3:** if the database is missing at connect time and this is `false`, the connection is aborted with a warning. If `true`, the server creates the database on the first write. |
| `measurement` | `device_data` | Measurement (v3: table) that receives all points from this bridge. |
| `include_timestamp` | `true` | Attach the bridge's receive time to each point. If `false`, the server assigns its own write time. |
| `include_device_info` | `true` | Write device metadata as tags. If `false`, points carry no tags. |
| `force_float` | `true` | Store every numeric field as a float (see [Fields](#fields)). |
| `use_utc_timestamp` | `false` | Choose whether the bridge treats "now" as UTC or machine-local time and reports that zone (`machine_timezone`) to the admin screens. The stored epoch timestamp is the same either way. |
| `batch_size` | `100` | Number of points that triggers a flush. |
| `batch_timeout` | `10.0` | Seconds since the last successful write after which the next arriving point triggers a flush. |
| `connection_timeout` | `10` | Timeout in seconds. **v1:** the client timeout for all requests. **v3:** applies to the HTTP side calls only (heap-profile probe, edition check capped at 5 s, Metrics Edit delete requests); writes and queries use the InfluxDB 3 client's own timeouts. |
| `reconnect_attempts` | `5` | Reconnection attempts per reconnect cycle. |
| `reconnect_delay` | `5.0` | Base delay between attempts (seconds). |
| `use_exponential_backoff` | `true` | Double the delay after each failed attempt. |
| `max_reconnect_delay` | `300.0` | Cap on the backoff delay (seconds). |
| `periodic_reconnect_interval` | `14400.0` | Seconds between proactive connection checks; `0` disables. |
| `enable_persistent_storage` | `true` | Save undeliverable points to a backlog file. |
| `persistent_storage_path` | `backlogs` | Backlog folder, relative to the MPG install directory. |
| `max_backlog_size` | `10000` | Maximum points held in the backlog. |
| `max_backlog_age` | `86400` | Maximum backlog point age in seconds (applied when the backlog file is loaded). |
| `stale_data_timeout` | `300` | Seconds of unchanged data before a device is treated as stale. |
| `max_stale_attempts` | `3` | See [Stale Data Detection](#stale-data-detection). |
| `retry_delay_mins` | `5` | See [Stale Data Detection](#stale-data-detection). |
| `data_dir` | empty | **v1:** local path to InfluxDB's data directory, used only to show on-disk size in the Storage Overview. |
| `object_store_dir` | empty | **v3:** local path to InfluxDB 3's object store, used only for on-disk size in the Storage Overview. |
| `debug_pprof_url` | empty | **v3:** optional URL of the server's heap-profile debug endpoint, probed by the Storage Overview. |
| `log_level` | inherits root | Log level for this bridge (`DEBUG`, `INFO`, ...). |

The connection health-check throttle (300 seconds between routine checks) is fixed in the code and is not a configuration option.

### Batching

Points are batched in memory. A flush is triggered **only when a new point arrives** and either:

- the batch holds at least `batch_size` points, or
- at least `batch_timeout` seconds have passed since the last successful write.

There is no background timer. Consequences:

- The very first point after startup is written immediately (no write has happened yet, so the timeout has already elapsed).
- If your device `read_interval` is longer than `batch_timeout` (for example a 15 second read interval with the default 10 second timeout), every arriving point finds the timeout expired and is written immediately, so effective batches are one point. Batching only accumulates when several devices report within `batch_timeout` of each other, or when `batch_timeout` is larger than the read interval.
- If data stops arriving, a partially filled batch stays in memory until the next point arrives (or until the bridge is closed). The **Write batch pending** figure on the Bridge Health panel shows how many points are waiting.

`batch_size` counts points (one per device per cycle), not fields.

### Connection Monitoring

Each bridge checks its connection from inside `write_data()` and when it flushes a batch, so checks only happen while data is flowing.

#### Health checks

- Routine checks are throttled to **once every 300 seconds**. Between checks the bridge trusts its last known state.
- **v1** checks with the client's `ping()`. **v3** runs `SELECT 1 FROM information_schema.tables LIMIT 1` against the configured database, which validates network, credentials and database access together.
- A failed check triggers a reconnect (see below). A failed *write* also triggers a reconnect and one retry of the batch.

#### Reconnection logic

- Up to `reconnect_attempts` attempts, each creating a fresh client and repeating the connectivity check.
- Between failed attempts the bridge waits `reconnect_delay` seconds, doubling each time when `use_exponential_backoff` is on and capped at `max_reconnect_delay`. There is no wait after the last attempt.
- On success the backlog is flushed immediately.
- On failure the bridge is marked disconnected and new points go to the backlog; the next reconnect attempt happens on the next arriving point after the 300 second check throttle has passed.
- **Reconnection blocks the caller.** The waits are ordinary sleeps performed on the thread that delivered the data. In `sequential` read mode that thread is the main polling loop, so all devices stop being read while a reconnect cycle runs (with the defaults, roughly 75 seconds of waiting, plus up to `connection_timeout` per attempt against an unreachable host). In `concurrent` and `interleaved` modes only the affected worker waits. If your InfluxDB server is often unreachable, use a smaller `reconnect_attempts`/`reconnect_delay`.

Connection state changes also trigger MPG's normal connection-lost / connection-restored notifications when messaging (Pushover/Telegram) is configured.

### Data Structure

Each `write_data()` call becomes one point:

- **Measurement:** the `measurement` setting. All devices sent through one bridge share this measurement and are distinguished by their tags.
- **Tags:** described below.
- **Fields:** one per forwarded variable.
- **Time:** the receive time in nanoseconds (if `include_timestamp = true`).

#### Tags (if `include_device_info = true`)

- `device_identifier`: the device serial number, trimmed and lower-cased
- `device_name`: the device name (defaults to `<manufacturer>_<serial>`)
- `device_manufacturer`: device manufacturer (defaults to `MPG`)
- `device_model`: device model (see the note below)
- `device_serial_number`: device serial number as configured
- `transport`: the source scraper transport's section name

If a device reports a model code in a `LCDMachineModelCode` value (and it isn't `MPG`), that value replaces `device_model` for that device from then on.

If you set `include_device_info = false`, points carry no tags, so points from different devices with the same timestamp will overwrite each other's fields. Leave it on when more than one device writes to a measurement.

#### Fields

Each variable is typed by these rules (identical for v1 and v3), evaluated in order:

1. If the variable's protocol definition marks it as an **enumerated value** or an **ASCII/string** value, it is stored as a **string**.
2. If the value is already text containing letters (such as a synthetic label), it is stored as a **string**.
3. Otherwise the value is converted to a number. If `force_float` is `true` (the default), or the register has a scaling factor (`unit_mod` other than 1), it is stored as a **float**. If `force_float` is `false` and the value is a whole number, it is stored as an **integer**; otherwise a float.
4. If conversion fails, the value is stored as a **string**.

With the default `force_float = true`, virtually every numeric field is a float. This avoids the "field type conflict" errors InfluxDB raises when a field is written as an integer one moment and a float the next. Changing `force_float` on an existing measurement can itself cause type conflicts, because InfluxDB v1 keeps the type of the first value written to a field (per shard).

#### Time

- `include_timestamp = true` (default): the point carries the bridge's receive time in nanoseconds. Points saved to the backlog keep this original time, so replayed data lands at the right moment.
- `include_timestamp = false`: no time is attached and the server stamps the point when it is written. **Do not combine this with the persistent backlog**: replayed points would all receive the replay time instead of the time they were captured.

### Bridge Health and Storage Overview panels

The bridge's device page in the admin UI shows two read-only panels.

**Bridge Health** shows: points pending in the write batch (`n / batch_size`), points in the backlog (`n / max_backlog_size`, marked *buffering* when non-zero, or *Disabled*), the periodic reconnect interval and when it last ran, and how many source transports are currently flagged stale. Connection status is shown in the page's own status badge.

**Storage Overview** is a best-effort snapshot:

- **v1:** retention policies, discovered measurements, an approximate row count for the first measurement (the highest per-field count), server runtime/database/engine statistics where the server exposes them, and the on-disk size of `data_dir` if configured and readable from the MPG host.
- **v3:** the tables in the database, per-table row counts and file sizes (from `system.parquet_files`, with a `COUNT(*)` fallback for row counts if that table is empty), a column/type map from `information_schema.columns`, the size of `object_store_dir` if configured, and an optional heap-profile probe if `debug_pprof_url` is set. What populates depends on the InfluxDB 3 edition and version you run.

Both panels use whatever the bridge is currently connected to; if the bridge is not connected they display a "Not connected" message.

### Example Bridge Configuration

```ini
# Source device (e.g., Modbus RTU)
[transport.growatt_inverter]
transport = modbus_rtu
port = /dev/ttyUSB0
baudrate = 9600
protocol_version = growatt_2020_v1.24
device_serial_number = 123456789
device_manufacturer = Growatt
device_model = SPH3000
read_interval = 15
bridge = transport.influxdb

# InfluxDB output
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
measurement = inverter_data
```

A scraper can list several bridges (`bridge = transport.influxdb, transport.mqtt`), and several scrapers can share one bridge.

### Installation without Docker

Install the client library for the version you use:

```bash
pip install influxdb            # InfluxDB v1  (influxdb_out)
pip install influxdb3-python    # InfluxDB v3  (influxdb3_out)
```

Both are also listed in `requirements.txt`.

### InfluxDB Setup

**v1:**

1. Install InfluxDB v1:

   ```bash
   # Ubuntu/Debian
   sudo apt install influxdb influxdb-client
   sudo systemctl enable influxdb
   sudo systemctl start influxdb

   # Or download from https://portal.influxdata.com/downloads/
   ```

2. Creating the database is optional; MPG creates it if it doesn't exist (the user needs permission to do so). To create it yourself:

   ```bash
   echo "CREATE DATABASE solar" | influx
   ```

**v3:** see [InfluxDB3.md](InfluxDB3.md) for a Docker Compose setup with InfluxDB 3 Core and the Explorer UI, including how to generate the admin token MPG needs.

### Querying Data

Because each point is one wide row per device per cycle, every variable is its own column/field (there is no generic `value` or `field_name` column).

**InfluxDB v1 (InfluxQL):**

```sql
-- Show all measurements
SHOW MEASUREMENTS

-- Show the fields MPG has written
SHOW FIELD KEYS FROM device_data

-- Query recent data
SELECT * FROM device_data WHERE time > now() - 1h

-- Query a specific device
SELECT * FROM device_data WHERE device_identifier = '123456789'

-- Aggregate one variable (use the variable name from your protocol)
SELECT mean("battery_voltage") FROM device_data
WHERE device_identifier = '123456789' AND time > now() - 6h
GROUP BY time(5m)
```

**InfluxDB v3 (SQL):**

```sql
SELECT time, battery_voltage
FROM device_data
WHERE device_identifier = '123456789' AND time > now() - INTERVAL '6 hours'
ORDER BY time DESC
```

### Integration with Grafana

InfluxDB data can be visualized in Grafana:

1. Add InfluxDB as a data source in Grafana (InfluxQL for v1; SQL or InfluxQL for v3, using the database name and token).
2. Use the same host, port and credentials as your bridge configuration.
3. Create dashboards using queries like the ones above, or import the sample dashboards: [GrafanaInfluxDBDashboard.json](../../dashboards/GrafanaInfluxDBDashboard.json) (v1) and [GrafanaInfluxDB3Dashboard.json](../../dashboards/GrafanaInfluxDB3Dashboard.json) (v3).

For connection problems, missing data, or tuning `batch_size`/`batch_timeout`, see [Troubleshooting](#troubleshooting).

---

## Advanced Features: Reconnection, Stale Detection & Persistent Backlog

### Overview

The bridges include features to cope with unreliable networks and long outages:

1. **Exponential backoff**: increasing delays between reconnection attempts.
2. **Periodic reconnection check**: a proactive connection check at a fixed interval.
3. **Persistent backlog**: undeliverable points are stored on disk and replayed later.
4. **Stale data detection**: notices a device that keeps returning identical values.

All of these are enabled by default (except that stale detection only acts on frozen data), and they behave the same in `influxdb_out` and `influxdb3_out`.

### Exponential Backoff

#### How it works

When `use_exponential_backoff = true`, the wait between failed reconnection attempts is `reconnect_delay × 2^(n-1)` seconds (where *n* is the attempt that just failed), capped at `max_reconnect_delay`. The first attempt is made immediately; delays are only inserted **between** attempts, so with `reconnect_attempts = 5` there are four waits.

With the defaults (`reconnect_delay = 5`, `reconnect_attempts = 5`):

| After failed attempt | Wait before next attempt |
| --- | --- |
| 1 | 5 s |
| 2 | 10 s |
| 3 | 20 s |
| 4 | 40 s |
| 5 | none (gives up) |

That is 75 seconds of waiting in total (plus time spent in each failing attempt, up to `connection_timeout` for v1). With `use_exponential_backoff = false`, every wait is `reconnect_delay`.

#### Configuration

```ini
[transport.influxdb]
use_exponential_backoff = true
reconnect_delay = 5.0
max_reconnect_delay = 300.0
reconnect_attempts = 5
```

#### Example scenarios

```text
Short network glitch:   attempt 1 succeeds immediately                → no delay
Server restart:         attempt 1 fails, wait 5 s; attempt 2 fails,
                        wait 10 s; attempt 3 succeeds                 → ~15 s of waiting
Extended outage:        attempts 1-5 all fail (5 + 10 + 20 + 40 = 75 s
                        of waiting), bridge marked disconnected,
                        points go to the backlog
```

After a failed reconnect cycle, the next one is not attempted until the next point arrives after the 300 second health-check throttle. A long outage therefore produces one reconnect cycle roughly every five minutes, not a continuous retry loop.

### Periodic Reconnection Check

#### How it works

Every `periodic_reconnect_interval` seconds (default 14400, or 4 hours), the next arriving point triggers a proactive connection check, regardless of the 300 second throttle:

- If the bridge believes it is disconnected, it starts a reconnect cycle immediately.
- Otherwise it pings (v1) or runs the health query (v3). If the check passes, nothing else happens; the existing connection is kept. If it fails, a reconnect cycle starts.

Like all checks it runs inside `write_data()`, so it only happens while data is arriving. It does not run on its own timer during quiet periods, and it does not tear down a healthy connection. Set `periodic_reconnect_interval = 0` to disable it. The Bridge Health panel shows the interval and when the check last ran.

```ini
[transport.influxdb]
periodic_reconnect_interval = 14400.0   # 4 hours (default)
# periodic_reconnect_interval = 0       # disable
```

### Stale Data Detection

Stale detection catches a scraper that keeps delivering *identical* data (for example a gateway that returns its last cached values when the device behind it has stopped responding). It does not detect a scraper that delivers no data at all (in that case the gateway logs "No data ... device may be unresponsive" and the bridge is never called).

How it works:

1. For every source transport the bridge remembers the last payload it received. This is tracked for each source separately and runs on every `write_data()`, even while InfluxDB itself is offline.
2. A new payload counts as unchanged if every field matches the payload that started the current unchanged period (numbers are compared with a small tolerance, so floating-point noise does not count as a change). The timer starts when the data last changed and keeps running while it stays the same; any changed field restarts it and clears the counters below.
3. If the payload has been unchanged for longer than `stale_data_timeout` seconds (default 300), the source is flagged **stale**.
4. Each time a stale payload arrives, the bridge may ask the gateway to reconnect that source transport (which marks it disconnected and forces a fresh read on the next cycle) and send a **"MPG Stale Data Alert"** notification through any configured messaging service. Requests are spaced at least `retry_delay_mins` apart and stop after `max_stale_attempts` requests in one stale period.
5. The source stays flagged stale until its data changes; the Bridge Health panel shows the count of stale sources out of the sources tracked.

With the defaults, a source whose data freezes is flagged after 5 minutes, gets a reconnect request and alert immediately, and up to two more at 5 minute intervals if the data is still frozen. After that the bridge stops asking until the data changes and freezes again.

```ini
[transport.influxdb]
stale_data_timeout = 300     # seconds of unchanged data before a source is stale
max_stale_attempts = 3
retry_delay_mins = 5
```

### Persistent Storage (Data Backlog)

#### How it works

The backlog is a safety net for points the bridge could not deliver:

1. **While the server is unreachable**, each new point is added to the backlog (after any points still pending in the write batch) and the backlog file is rewritten on disk.
2. **If a batch write fails** and the immediate reconnect-and-retry also fails, the points from that batch are added to the backlog.
3. **When the connection is restored** (either at startup or after a reconnect), the whole backlog is sent to InfluxDB in a single write call, then cleared and the file rewritten. If that write fails the backlog is kept and retried on the next successful reconnect.
4. **Across restarts**: the backlog file is loaded when the bridge starts, so points survive a restart of MPG and are flushed when the bridge first connects.

What the backlog does **not** cover:

- Points that were written successfully to InfluxDB are never stored in the backlog.
- Points sitting in the in-memory write batch while the bridge is online (up to `batch_size` points) are not on disk until a write fails or the gateway is stopped or reloaded (see *Shutdown* below). A crash or hard kill can lose them.
- If `enable_persistent_storage = false`, points arriving while the server is unreachable are discarded (log line: `Persistent storage disabled, data will be lost`).

Limits and housekeeping:

- **Size:** when the backlog exceeds `max_backlog_size`, the oldest points are dropped (log line: `Backlog full, removed N oldest point(s)`).
- **Age:** `max_backlog_age` is enforced when the backlog file is **loaded** (at startup or reload). It does not expire points while MPG keeps running, so a long-running outage is limited by `max_backlog_size`, not by age.
- **One copy per point:** a point captured during an outage is stored in the backlog exactly once, so `max_backlog_size` is the real capacity. (Older MPG versions stored outage points twice; a backlog file written by an old version may still contain duplicates, which are harmless because identical points overwrite each other in InfluxDB.)
- **File rewrite cost:** the entire backlog file is rewritten each time points are added (once per point while offline, once per batch when a failed batch is stored), so a very large `max_backlog_size` makes each write during an outage progressively more expensive.
- **Shutdown:** when the gateway is stopped or reloaded, points still pending in the write batch are written to InfluxDB if the bridge is connected, otherwise they are added to the backlog file. No reconnect is attempted at that moment.
- **Timestamps:** replayed points keep the time they were captured only when `include_timestamp = true` (the default).

#### Configuration

```ini
[transport.influxdb]
enable_persistent_storage = true
persistent_storage_path = backlogs      # relative to the MPG install directory
max_backlog_size = 10000
max_backlog_age = 86400
```

`persistent_storage_path` is always resolved **relative to the MPG install directory**: leading slashes are stripped, so `/data/backlogs` becomes `<install dir>/data/backlogs`.

#### Storage structure

One pickle file per bridge, named after the bridge's section name:

```text
backlogs/
├── influxdb_backlog_transport.influxdb.pkl     # influxdb_out bridge named "transport.influxdb"
└── influxdb3_backlog_transport.influxdb3.pkl   # influxdb3_out bridge named "transport.influxdb3"
```

The files are ordinary (uncompressed) Python pickle files. They are loaded with `pickle`, so only keep MPG-written files in this folder and protect it from untrusted writers.

#### Docker

In the container the install directory is `/app`, so backlogs are written to `/app/backlogs`. That folder is **not** persisted by default: if the container is recreated the backlog is lost. To keep it, mount a volume in your compose file:

```yaml
    volumes:
      - mpg/backlogs:/app/backlogs
```

#### Example recovery log

```text
[2026-01-15 10:30:00] Connection check failed: Connection refused
[2026-01-15 10:30:00] Attempting to reconnect to InfluxDB at localhost:8086
[2026-01-15 10:32:00] Failed to reconnect after 5 attempts
[2026-01-15 10:32:00] Not connected to InfluxDB, storing data in backlog
...
[2026-01-15 18:45:00] Attempting to reconnect to InfluxDB at localhost:8086
[2026-01-15 18:45:00] Successfully reconnected to InfluxDB
[2026-01-15 18:45:00] Flushing 2847 backlog points to InfluxDB
[2026-01-15 18:45:00] Successfully wrote 2847 backlog points to InfluxDB
```

(For v3 the messages read `InfluxDB v3` in place of `InfluxDB`.)

### Configuration Examples

#### Stable network (local InfluxDB)

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar

reconnect_attempts = 3
reconnect_delay = 2.0
use_exponential_backoff = false

periodic_reconnect_interval = 1800.0   # 30 minutes

enable_persistent_storage = true
max_backlog_size = 1000
max_backlog_age = 3600

use_utc_timestamp = true

stale_data_timeout = 300
max_stale_attempts = 3
retry_delay_mins = 5
```

#### Unstable network (remote InfluxDB)

```ini
[transport.influxdb]
transport = influxdb_out
host = remote.influxdb.com
port = 8086
database = solar

reconnect_attempts = 10
reconnect_delay = 5.0
use_exponential_backoff = true
max_reconnect_delay = 600.0

periodic_reconnect_interval = 900.0    # 15 minutes

enable_persistent_storage = true
max_backlog_size = 50000
max_backlog_age = 604800               # 1 week (applied at startup/reload)
```

Remember that with a long `reconnect_attempts`/backoff schedule in `sequential` read mode, every failed reconnect cycle pauses reads of all devices while it runs (the schedule above waits roughly 15 minutes per cycle). Prefer `concurrent` read mode, or shorter schedules, for unreliable remote servers.

#### High-volume data

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar

reconnect_attempts = 5
reconnect_delay = 1.0
use_exponential_backoff = true
max_reconnect_delay = 60.0

enable_persistent_storage = true
max_backlog_size = 100000
max_backlog_age = 86400

batch_size = 500
batch_timeout = 30.0
```

#### InfluxDB v3

```ini
[transport.influxdb3]
transport = influxdb3_out
host = http://influxdb_v3-core
port = 8181
database = solar
token = apiv3_your_token_here
measurement = device_data
auto_create_database = true

enable_persistent_storage = true
persistent_storage_path = backlogs
```

### Monitoring and Maintenance

#### Check backlog status

The admin UI's Bridge Health panel shows the live backlog count. From the command line:

```bash
# Check backlog file sizes
ls -lh backlogs/

# Count points in each backlog file (Python)
python3 -c "
import pickle, os
for file in os.listdir('backlogs'):
    if file.endswith('.pkl'):
        with open(f'backlogs/{file}', 'rb') as f:
            data = pickle.load(f)
            print(f'{file}: {len(data)} points')
"
```

#### Monitor logs

Use your configured log file (`[logging] log_dir` / `log_file`; `logs/MPG.log` by default):

```bash
# Backlog activity
grep -i "backlog\|persistent" logs/MPG.log

# Reconnection attempts
grep -i "reconnect" logs/MPG.log

# Periodic checks
grep -i "periodic" logs/MPG.log

# Stale data
grep -i "stale" logs/MPG.log
```

#### Clean up old backlog files

Only remove backlog files for bridges you no longer use, and only while MPG is stopped; a backlog contains data that has not yet reached InfluxDB.

### Performance Considerations

- **Memory:** the backlog is held in memory as well as on disk, so memory use grows with `max_backlog_size` and the number of fields per point (a wide inverter payload has hundreds of fields).
- **Disk:** the backlog file is a single pickle rewritten each time points are added, so a large backlog makes each write during an outage more expensive.
- **Recovery upload:** the whole backlog is sent in one write request. A very large backlog can produce a large request and a spike of load on the InfluxDB server when the connection returns.
- **Batching:** larger `batch_size`/`batch_timeout` values reduce write requests but increase how much data is only in memory at any moment.

### Best Practices

1. **Size the backlog for your outages.** Points per hour = (devices × 3600 ÷ `read_interval`). Set `max_backlog_size` a little above the number you expect (for example, one device read every 60 seconds for a 24-hour outage: 1,440 points, so about 2,000).
2. **Keep `include_timestamp = true`** whenever the backlog is enabled.
3. **Persist `/app/backlogs`** in Docker if you rely on the backlog.
4. **Keep `force_float = true`** unless you have a reason not to; it avoids field type conflicts.
5. **Test recovery** by stopping InfluxDB briefly, then confirm the backlog fills (Bridge Health panel) and drains after restart.
6. **Pick a read mode that suits your reconnect schedule** (see *Connection Monitoring*).

### Migration from Previous Versions

Features described here are enabled by default and existing configurations continue to work. To turn features off:

```ini
[transport.influxdb]
use_exponential_backoff = false
enable_persistent_storage = false
periodic_reconnect_interval = 0
```

---

## Metrics Edit — Editing or Deleting Historical Metric Values

### Edit Overview

The **InfluxDB → Metrics Edit 1.x** / **Metrics Edit 3.x** admin screens let an administrator correct or remove specific metric *values* — for one device, over a chosen date/time range — on either an InfluxDB v1 (`influxdb_out`) or InfluxDB v3 (`influxdb3_out`) bridge. Both nav items lead to the same screen; which one(s) appear depends on which InfluxDB bridge version(s) are actually configured and connected.

This is the InfluxDB counterpart of [TimescaleDB's Metrics Edit screen](../TimeScaleDB/timescaledb.md#45-metrics-edit--editing-or-deleting-historical-metric-values), built for the same purpose — fixing a bad sensor reading, clearing data captured during a known test or outage — but InfluxDB v1 and v3 use fundamentally different query engines (InfluxQL vs. SQL/DataFusion) and neither one supports a SQL-style `UPDATE`, so "editing" a value works differently here than it does against TimescaleDB. **Read the "How Editing and Deleting Actually Work" section below before using this screen** — the two actions don't behave the way they would against a relational database, and the differences matter.

### Using the Metrics Edit Screen

1. Open **InfluxDB → Metrics Edit 1.x** or **Metrics Edit 3.x** from the admin menu (only the version(s) with a connected bridge are shown).
2. Pick a **measurement** on the left.
3. Pick the **device** whose data you want to edit.
4. Pick the **field** to target — exactly one field per edit; see "Why only one field at a time" below.
5. Pick a **start** and **end** date/time for the range to affect.
6. Choose an **action**:
   - **Delete value(s)** — see the version-specific warning below; both versions remove the *entire point/row*, not just the selected field.
   - **Set value** — overwrites the selected field with a replacement value you enter.
7. Click **Preview** to see how many points/rows match and a sample of their current values before changing anything.
8. Click **Add to Staged Changes**.
9. Use the existing **Commit All Changes** button in the header to apply every staged edit — InfluxDB edits, TimescaleDB Metrics Edit changes, and TimescaleDB column deletions are all applied together by that one button. Staged edits can be reviewed and individually removed before committing, or abandoned entirely with **Discard Changes**.

### Why Only One Field at a Time

Unlike a wide TimescaleDB table, an InfluxDB measurement has no single declared schema an admin edits column-by-column — every field is looked up independently, and a **Set value** edit only ever makes sense applied to one field's own type. Letting an admin pick several fields of different types (say, one integer field and one boolean field) for a single replacement value would produce an ambiguous, easy-to-misuse result, so the Field picker is a single dropdown rather than a checklist — the same design TimescaleDB's Metrics Edit screen uses for the same reason.

### How Editing and Deleting Actually Work

Neither InfluxDB v1 nor InfluxDB v3 (Core or Enterprise) supports an `UPDATE` statement. Correcting a value means **re-writing a point/row** at the exact same series identity (tags) and exact same timestamp, containing only the corrected field — both storage engines merge a new write into what's already stored at that (series, timestamp) key on a per-field basis, so every other field already recorded at that timestamp is left untouched. This screen queries the current matching points/rows first (to capture each one's exact tag set and timestamp), then re-writes just the one field.

Deleting is where the two versions diverge:

| | InfluxDB v1 | InfluxDB v3 |
| --- | --- | --- |
| **Mechanism** | InfluxQL `DELETE FROM ... WHERE <tags/time>` | InfluxDB 3 **Enterprise** row-delete request (`influxdb3 delete rows` / `POST /api/v3/row_delete_requests`) |
| **Scope** | Whole **point** (every field) at each matching timestamp — InfluxQL's `DELETE` has no field predicate | Whole **row** (every field) at each matching timestamp — the row-delete predicate supports tag equality only, no field predicate |
| **Timing** | Synchronous — applied by the time the request returns | **Asynchronous** — the request is only *accepted*; the compactor applies it later, by default within 24 hours (server-configurable), and the targeted rows remain queryable until then |
| **Availability** | Any InfluxDB v1 server | **InfluxDB 3 Enterprise only**, and only after the storage engine has been upgraded (`--use-pacha-tree` / `--upgrade-pacha-tree`) — InfluxDB 3 Core has no delete capability at all and rejects the request outright |
| **Permissions** | Whatever the configured bridge token already allows | Additionally requires `db:<database>:delete` permission on the token used |

Because of this, **"Delete value(s)" always removes every field at each matching timestamp for the selected device — never just the field shown in the Field picker.** The screen shows a warning to this effect whenever "Delete value(s)" is selected, worded per version, and the staged-changes list marks a v3 delete as *"(whole row, async)"* so it isn't mistaken for something that already happened the moment it was committed. If a v3 server isn't Enterprise, or hasn't had the storage engine upgrade, committing a staged delete fails with a clear error rather than silently doing nothing.

### Value Type Validation

A replacement value entered for **Set Value** is checked against the field's reported type before it is even staged:

- **InfluxDB v1** fields are checked against the type InfluxQL's `SHOW FIELD KEYS` reports (`float`, `integer`, `string`, or `boolean`).
- **InfluxDB v3** fields are checked against the Arrow type `information_schema.columns` reports (e.g. `Float64`, `Int64`, `Utf8`, `Boolean`).

An invalid value — text into an integer field, an unrecognized boolean spelling, a non-whole number into an integer field — is rejected immediately, with a clear error, rather than only surfacing when Commit All Changes is pressed.

### Staging and Commit

Like every other admin screen in this app, nothing is written to InfluxDB (or queued for asynchronous deletion) until **Commit All Changes** is pressed. Staged InfluxDB edits — v1 and v3 together — share one staged-changes list, independent from TimescaleDB's own staged column deletions and Metrics Edit changes; all of them are applied by the same "Commit All Changes" press and cleared together by "Discard Changes."

If an edit fails while committing (for example the server rejects it), the entries that already succeeded are cleared from the staged list, the failing entry and the ones after it stay staged, and the commit reports an error so you can fix the cause and press **Commit All Changes** again. A commit with no staged InfluxDB edits never touches InfluxDB, so it succeeds even if the bridge is offline.

### Bridge Connection and Scope

Metrics Edit reads and writes InfluxDB through the running bridge's own client connection. It does not use the bridge's write batch or persistent backlog, so:

- The bridge must be **connected**. If it is not, previewing or committing fails with *"Not connected to InfluxDB -- bridge must be connected before editing metric values."* Edits are never queued for later.
- The screen uses the **first** `influxdb_out` bridge (for 1.x) and the **first** `influxdb3_out` bridge (for 3.x) on the gateway. If you configure several bridges of the same version, only the first can be edited from this screen.
- The rewritten value goes straight to the database on commit; it does not pass through `batch_size`/`batch_timeout`.

---

## Time-Shifted Data Import / Export

The **Timeshift Data** screen is the admin UI's tool for exporting a date range of data to a CSV file, replaying/repairing data back into the same or a different time range, and importing spreadsheet exports from EG4 inverter monitoring into an existing measurement or table. It works against InfluxDB v1, InfluxDB v3, and TimescaleDB.

Open it from the InfluxDB (or TimescaleDB) nav dropdown's **Timeshift Data** item (`/pages/timeshift-data`). A version toggle at the top of the page switches between InfluxDB 1.x, InfluxDB 3.x, and TimescaleDB — only the destinations that actually have a connected bridge are shown.

### Picking a Destination

- **InfluxDB (v1/v3)**: type a **Measurement** name. Existing measurements on the connected bridge are suggested as you type; typing a name that doesn't exist yet creates it.

Everything else on the page — timezone, tags/device, the confidence/coercion/delete-existing row, and both the Export and Import panels — stays disabled until a measurement/table is chosen, since none of those settings mean anything without a target.

### Source Modes

Three radio options at the top of the page choose what you're doing:

1. **Export InfluxDB range to CSV** — pick a date range on the connected bridge and download a time-shifted CSV.
2. **Import EG4 spreadsheet** — upload a spreadsheet exported from the EG4 monitoring website. Only selectable when at least one `eg4_*` protocol is configured on this gateway.
3. **Re-import an exported/edited CSV** — re-import a CSV this same screen previously exported, optionally hand-edited first (e.g. in Excel, to fix a bad sensor reading or remove bad rows).

### Common Settings

These apply regardless of source mode:

- **Local Machine Timezone** — an IANA zone name (e.g. `America/Los_Angeles`), preselected to this bridge's configured zone. Used to interpret naive timestamps (an EG4 export's local time) and to display the Source/Target time fields.
- **Tags** (InfluxDB) — the six standard MPG tags (`device_identifier`, `device_name`, `device_manufacturer`, `device_model`, `device_serial_number`, `transport`) are seeded as rows; pick a value already stored in the measurement or choose **+ New value…** to enter your own. `device_identifier` is required. A source spreadsheet column whose name matches a tag key is ignored as a data field by default.
- **Device** (TimescaleDB) — replaces the Tags panel: pick the device every point in this export/import belongs to, from the same device picker the Metrics Edit screen uses, scoped to whichever table is selected. Only devices that already have at least one row in that table are listed.
- **Metric Match Confidence Threshold** — a 0–1 score (default **0.85**) controlling how confident the fuzzy field-name matcher must be before it pre-fills a mapping suggestion for an EG4 import; see "Field Matchup" below.
- **Allow float coercion on type value conflicts** — lets a value be coerced toward the field's existing type (e.g. a whole-number float written to an integer field) instead of being rejected. Always on for TimescaleDB, since values there are always coerced to each column's declared type.
- **Delete existing points in target range first** — only offered for InfluxDB v1; for v3 and TimescaleDB, use the Metrics Edit screen's delete instead.

### Export: InfluxDB Range to CSV

With **Export InfluxDB range to CSV** selected:

1. Optionally filter to one **Device Identifier**.
2. Pick a **Source Start** and **Source End** for the range to export.
3. Pick a **Target Start** for the shifted timestamps that will appear in the CSV — leave it equal to Source Start to export with no shift.
4. Click **Export to CSV**. The browser's own Save dialog is where the file lands.

Every timestamp in the export is shifted by `Target Start − Source Start`, applied uniformly across the whole range. Tag columns are dropped from the CSV — on re-import, tags are re-supplied as fixed values via the Tags panel, not read back from the file.

### Import: EG4 Spreadsheet or Re-imported CSV

With **Import EG4 spreadsheet** or **Re-import an exported/edited CSV** selected:

1. Choose the **File** (`.csv`, `.xls`, or `.xlsx`).
2. Set **Source Start** and **Target Start** — the same `Target Start − Source Start` delta is applied to every row's timestamp; leave them equal for no shift. For an EG4 upload, Source Start defaults to the earliest timestamp found in the file once it's scanned.
3. Click **Upload & Scan Fields**. The file is parsed and held server-side, and a **Field Matchup** table appears.
4. Pick the **Time Column** — the column holding each row's timestamp. The page guesses one automatically where it can (an exact "time"/"date"/"timestamp" header, the first column already typed as a date/time, or a header that's clearly a time label).
5. Review the Field Matchup table (see below), adjusting any row as needed.
6. Click **Preview** to see row/field counts and a sample of what would be written, without touching the database.
7. Click **Import to InfluxDB** (or **Import to TimescaleDB**) to write it for real. A confirmation dialog reminds you this writes immediately — it is **not** part of the staged "Commit All Changes" flow used elsewhere in the admin UI.

#### Field Matchup

Each source column gets one row in the Field Matchup table:

- **Re-imported CSV**: column names already match InfluxDB field names (they came from this screen's own Export), so the mapping shown is the identity mapping — shown for review/override rather than guessed.
- **EG4 spreadsheet**: column names rarely match the schema (`pv1Voltage`, `AC Voltage (V)`, `GridFrequencyHz`, ...), so each column is fuzzy-matched against the measurement/table's existing field names. A match scoring at or above the Confidence Threshold is pre-filled; a column that doesn't reach the threshold starts with **Ignore this column** pre-checked, since an EG4 export typically carries many columns MPG never records — the row is still shown, so you can un-check Ignore and either pick an existing field or **+ New field…** for it. (**+ New field…** isn't offered when importing into a TimescaleDB wide table, since that would require an `ALTER TABLE` this screen doesn't perform; it's offered normally for the narrow table.)
- A column whose name matches a tag key (or is `time`/`measurement`) is pre-checked **Ignore this column** by default, but can still be un-ignored and mapped as an ordinary field if that's genuinely wanted.
- Uploading a brand-new measurement/table with no existing schema leaves nothing pre-ignored on the matching basis above, since there's nothing yet to match against.

### Value Handling

Every cell is normalized before being written:

- Hex strings (`0x1478`) become integers.
- Percent strings (`25%`) become floats (`25.0`).
- Numeric strings (`"123"`, `"45.6"`) become integers/floats.
- Blank cells become nothing (the field is simply omitted from that point).

If **Allow float coercion** is on, a value is then nudged toward the field's already-recorded type — checked against InfluxQL's `SHOW FIELD KEYS` types for InfluxDB v1, or the Arrow types `information_schema.columns` reports for InfluxDB v3 (mapped onto the same float/integer/string/boolean buckets). A value that still conflicts with the field's existing type after coercion is skipped and reported rather than written, so it can't silently corrupt the measurement.

### Results

**Preview** never writes anything and never queues anything for deletion — it only reports what an import with the current mapping/tags/time settings would do. **Import** writes points immediately once confirmed, and reports:

- points written
- rows skipped for having no mapped fields, or an unparseable/missing timestamp
- columns left unmapped
- any type mismatches that were skipped rather than written

### Bridge Connection and Scope

Like Metrics Edit, Timeshift reads and writes InfluxDB through the running bridge's own client connection, using the **first** `influxdb_out` (1.x) or `influxdb3_out` (3.x) bridge on the gateway. An import writes immediately and does **not** pass through the bridge's write batch or persistent backlog, so the bridge must be connected when you click Import; if the server is unreachable the import fails rather than being buffered. For InfluxDB v1, writes are sent in chunks of 1,000 points, and if **Allow float coercion** is on, a field type conflict reported by the server is retried with that field forced to float.

---

## Troubleshooting

### Common Issue: Data Stops Being Written to InfluxDB

Work through the checks below in order. Most problems come down to configuration keys, an unreachable server, credentials, or field type conflicts.

#### 0. Confirm the bridge is actually loaded

- The bridge section must contain `transport = influxdb_out` (or `influxdb3_out`). A section that uses `type = ...` instead is silently ignored.
- Each scraper must name the bridge section exactly in `bridge =` (for example `bridge = transport.influxdb`). A mismatch logs `Bridge '<name>' not found for '<scraper>'`.
- If you edited the config in the admin UI, make sure you pressed **Commit All Changes** and that the gateway reload reported success.
- On the bridge's device page, the **Bridge Health** panel should render. The **InfluxDB** menu only appears when at least one InfluxDB bridge is loaded.

#### 1. Check logs

Enable debug logging for the bridge:

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
log_level = DEBUG
```

Look for these messages (v3 variants say `InfluxDB v3`):

- `Not connected to InfluxDB, storing data in backlog` — the server is unreachable; points are being buffered
- `Persistent storage disabled, data will be lost` (or `..., N point(s) will be lost`) — unreachable *and* no backlog, so points are being discarded
- `Connection check failed: ...` — a routine or periodic check failed
- `Attempting to reconnect to InfluxDB at ...` / `Reconnection attempt N/M failed` — a reconnect cycle is running
- `Failed to reconnect after N attempts` — the cycle gave up; the next one starts on a later point
- `Failed to write batch to InfluxDB: ...` — the server rejected or could not receive a write (the message includes the reason, e.g. a field type conflict or authorization error)
- `Wrote N points to InfluxDB` — a successful write (at INFO level; DEBUG logs sample field values instead)
- `Backlog full, removed N oldest point(s)` — `max_backlog_size` was reached during an outage
- `Flushing N backlog points to InfluxDB` — the backlog is being replayed

#### 2. Check the InfluxDB server

**v1:**

```bash
systemctl status influxdb
curl -i http://localhost:8086/ping
echo "SHOW DATABASES" | influx
```

**v3:**

```bash
# Replace host/port/token/database with your values
curl -i http://localhost:8181/health
curl -s -G -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "db=solar" --data-urlencode "q=SELECT 1" \
  http://localhost:8181/api/v3/query_sql
```

(For v3, MPG's own connectivity check is the query `SELECT 1 FROM information_schema.tables LIMIT 1` against your database, so a database or token problem shows up there.)

#### 3. Check network connectivity

```bash
ping your_influxdb_host
telnet your_influxdb_host 8086      # 8181 for v3
```

From inside Docker, remember that `localhost` is the MPG container itself; use the InfluxDB container's service name.

### Root Causes and Solutions

#### 1. Network connectivity issues

**Symptoms:** connection timeouts, intermittent gaps, repeated reconnect messages, and (in `sequential` read mode) all devices pausing during reconnect cycles.

**Solutions:**

```ini
[transport.influxdb]
# Slower networks: allow more time (v1)
connection_timeout = 30
# Keep reconnect cycles short so they don't stall polling
reconnect_attempts = 3
reconnect_delay = 5.0
```

Reconnect waits happen on the thread that delivered the data. In `sequential` mode that stalls every device, so keep the total wait short or switch to `concurrent` mode.

#### 2. InfluxDB server restarts

**Symptoms:** connection refused, a gap in the data, then recovery.

**Solutions:** check the InfluxDB server logs for crashes. With the backlog enabled, MPG keeps the data collected during the outage and replays it after the next successful reconnect. Note that a failed reconnect cycle is retried on the next point after the 5 minute check throttle, so recovery can lag the server coming back by up to about five minutes.

#### 3. Memory or resource problems

**Symptoms:** slow responses, hung connections, batch write failures.

**Solutions:**

```ini
[transport.influxdb]
# Reduce batch size to lower request size
batch_size = 50
batch_timeout = 5.0
```

#### 4. Authentication problems

**Symptoms:** authentication errors in the log, a connection that succeeds but writes that fail.

**Solutions:**

- **v1:** verify `username`/`password` and the user's privileges on the database. Test manually:

  ```bash
  curl -i -u username:password "http://localhost:8086/query?q=SHOW%20DATABASES"
  ```

- **v3:** verify `token`. The token needs permission to read (for the connectivity check and the admin screens) and write to the database. For **Delete value(s)** in Metrics Edit it additionally needs delete permission on the database (see that section). `username`/`password` are not used by the v3 transport.

#### 5. Database, measurement or field-type problems

**Symptoms:** data appears but not where you expect, `field type conflict` errors in the log.

**Solutions:**

- Verify `database` and `measurement`; all devices on a bridge write to that one measurement.
- Field type conflicts happen when a field's type changes (for example integer to float). Keep `force_float = true` (the default). If you turned it off, or the measurement already contains integer fields, either turn it back on and write to a new measurement, or fix the existing data.
- Text and enumerated variables are always stored as strings. If such a variable was previously stored as a number (or vice versa), InfluxDB v1 will reject the write for that field.

#### 6. v3-specific: database not found / wrong host

- If the database does not exist and `auto_create_database = false`, the bridge refuses to connect and logs a warning. Create the database on the server, or set `auto_create_database = true` to let the server create it on the first write.
- If `host` has no scheme, `http://` is assumed. For a TLS-enabled server or InfluxDB Cloud, include `https://`.
- If you set both an embedded port (`http://myhost:8181`) and a `port`, the `port` setting wins.

#### 7. Points arrive late, in bursts, or seem "stuck"

- Points are only flushed when a new point arrives (there is no timer). If devices stop reporting, the last few points stay in the batch until the next one arrives. Check **Write batch pending** on the Bridge Health panel.
- If `batch_timeout` is smaller than your `read_interval`, every point is written immediately; if it is larger, several points may be grouped. Adjust `batch_timeout` to the freshness you need.

#### 8. Data from a device is missing fields

- The gateway filters each device's data by its variable mask (or the protocol's variable list) before the bridge sees it. A variable that is not in the mask never reaches InfluxDB.
- The InfluxDB bridge accepts partial cycles, so a cycle that was cut short by timeouts writes only what was read.

### Configuration Best Practices

#### Recommended configuration

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
measurement = device_data
include_timestamp = true
include_device_info = true

# Connection monitoring
reconnect_attempts = 5
reconnect_delay = 5.0
connection_timeout = 10

# Batching (adjust to your read interval)
batch_size = 100
batch_timeout = 10.0

# Data handling
force_float = true
log_level = INFO
```

#### For unstable networks

```ini
[transport.influxdb]
reconnect_attempts = 5
reconnect_delay = 10.0
use_exponential_backoff = true
max_reconnect_delay = 120.0
connection_timeout = 30

# Bigger backlog
enable_persistent_storage = true
max_backlog_size = 20000

batch_size = 50
batch_timeout = 5.0
```

#### For high-volume data

```ini
[transport.influxdb]
batch_size = 500
batch_timeout = 30.0
reconnect_attempts = 3
reconnect_delay = 2.0
```

### Monitoring and Alerts

#### 1. Monitor whether data is arriving

```bash
# v1: count points in the last hour
curl -s "http://localhost:8086/query?db=solar&q=SELECT%20count(*)%20FROM%20device_data%20WHERE%20time%20%3E%20now()%20-%201h"
```

For v3, run `SELECT count(*) FROM device_data WHERE time > now() - INTERVAL '1 hour'` through the Explorer UI or the query API.

#### 2. Suggested alert conditions

- No new points in the last hour
- The Bridge Health panel shows a growing backlog, or the backlog approaching `max_backlog_size`
- Repeated `Failed to reconnect` messages
- Stale transports greater than zero
- MPG connection-lost / **MPG Stale Data Alert** notifications (Pushover/Telegram) if you have messaging configured

#### 3. Log monitoring

```bash
# Connection issues
grep -i "connection\|reconnect\|failed" logs/MPG.log

# Data flow
grep -i "wrote .* points\|backlog" logs/MPG.log
```

### Testing Your Setup

#### 1. Test data flow with a simple configuration

```ini
[transport.test_source]
transport = modbus_rtu
port = /dev/ttyUSB0
baudrate = 9600
protocol_version = test_protocol
read_interval = 5
bridge = transport.influxdb_test

[transport.influxdb_test]
transport = influxdb_out
host = localhost
port = 8086
database = test
measurement = test_data
log_level = DEBUG
```

#### 2. Verify data in InfluxDB

```sql
-- Check if data is being written
SELECT * FROM test_data ORDER BY time DESC LIMIT 10

-- Check the data rate
SELECT count(*) FROM test_data WHERE time > now() - 1h
```

#### 3. Test recovery

1. Stop the InfluxDB service for a minute or two while MPG runs.
2. Watch **Backlog buffer** on the bridge's Bridge Health panel grow.
3. Start InfluxDB. The backlog drains on the first reconnect after the next point arrives (this may take up to about five minutes because of the check throttle).
4. Confirm the gap is filled in InfluxDB.

### Advanced Troubleshooting

#### 1. Enable verbose logging

```ini
[logging]
level = DEBUG

[transport.influxdb]
log_level = DEBUG
```

#### 2. Check the bridge wiring

```ini
# Bridge names must match exactly
[transport.source]
transport = modbus_tcp
bridge = transport.influxdb

[transport.influxdb]
transport = influxdb_out
# A bridge does not need its own "bridge =" setting
```

#### 3. Monitor system resources

```bash
free -h
df -h
netstat -an | grep 8086      # 8181 for v3
```

#### 4. InfluxDB v1 server tuning (influxdb.conf)

```ini
[data]
wal-fsync-delay = "1s"
cache-max-memory-size = "1g"
series-id-set-cache-size = 100
```

### Output Transport Tuning

- Adjust `batch_size` and `batch_timeout` for your read interval; remember that a flush only happens when a new point arrives.
- Larger batches reduce network overhead but leave more data only in memory.
- Increase `connection_timeout` (v1) for slow networks, but keep `reconnect_attempts` small in `sequential` read mode.

### Common Error Messages

#### "Failed to connect to InfluxDB"

- Check that InfluxDB is running
- Verify `host` and `port`
- Check firewall settings
- v3: check the token and that the database exists (or `auto_create_database` is on)

#### "Failed to write batch to InfluxDB"

- Check the InfluxDB server's resources
- Verify database permissions / token permissions
- Check for field type conflicts (the reason is included in the message)

#### "Not connected to InfluxDB, storing data in backlog"

- The connection was lost and a reconnect is pending
- Check network connectivity
- Watch for `Reconnection attempt` messages

#### "Connection check failed"

- Network issue or InfluxDB restart
- Check InfluxDB server status
- Verify network connectivity

### Getting Help

If you're still having problems:

1. **Collect information:**
   - Gateway logs at DEBUG level
   - InfluxDB server logs
   - Network connectivity test results
   - Your config sections for the bridge and the scraper (remove passwords and tokens)

2. **Test steps:**
   - Confirm InfluxDB is reachable manually
   - Try a minimal configuration like the one above

3. **Provide details:**
   - Operating system and Python version (or Docker)
   - InfluxDB version and edition (v1, v3 Core, Enterprise, Cloud)
   - Network setup (local or remote InfluxDB)
   - Data volume and frequency, and MPG read mode

### Prevention

- **Monitor** data flow, backlog size and server health.
- **Validate** a configuration on a test database before deploying.
- **Back up** InfluxDB itself; the MPG backlog only covers short outages, is limited by `max_backlog_size`, and is not a substitute for database backups.

### Backlog & Reconnection Issues

#### Backlog not flushing

**Symptoms:** backlog points remain after the server is back; no `Flushing N backlog points` message.

**Explanation and solutions:**

- The backlog is flushed when a reconnect succeeds. After an outage the next reconnect cycle only starts on a point that arrives after the 5 minute check throttle, so allow a few minutes.
- If the flush itself fails (server capacity, permissions, a type conflict inside the replayed data), the backlog is kept and retried after the next successful reconnect. Check `Failed to flush backlog` in the log for the reason.
- The whole backlog is sent in one request; a very large backlog can time out. Reduce `max_backlog_size` if this recurs.

#### Backlog contains duplicate points

Current versions store each outage point once. If a backlog file written by an older MPG version contains duplicates, they are identical and harmless when replayed (InfluxDB keeps one point per series and timestamp).

#### Backlog lost after a restart or container recreation

- In Docker, `/app/backlogs` must be a mounted volume to survive container recreation.
- `max_backlog_age` is applied when the file is loaded: points older than that are discarded at startup or reload.
- Loading fails (with `Failed to load backlog` in the log) if the file is corrupt; the bridge then starts with an empty backlog.

#### Replayed points have the wrong timestamps

Replayed points keep their original time only when `include_timestamp = true`. With `include_timestamp = false` the server assigns the replay time.

#### Excessive memory usage

**Solutions:** reduce `max_backlog_size`; a very large backlog is held in memory as well as on disk.

#### Disk space issues

**Symptoms:** `Backlog full` warnings; low disk space.

**Solutions:**

- Reduce `max_backlog_size`
- Move `persistent_storage_path` to a larger disk (the path is relative to the MPG install directory; in Docker, mount a volume at `/app/backlogs`)
- Remove backlog files of bridges you no longer use (with MPG stopped)

#### Reconnection too aggressive

**Symptoms:** long stalls during outages (especially in `sequential` mode), network congestion.

**Solutions:**

- Reduce `reconnect_attempts`
- Reduce `reconnect_delay`/`max_reconnect_delay`
- Keep `use_exponential_backoff` enabled
- Use `concurrent` read mode so a reconnect wait only affects one device group

### Stale Data Issues

#### "MPG Stale Data Alert" received

A device returned identical values for longer than `stale_data_timeout`. MPG has asked the gateway to reconnect that source. If the device is genuinely idle (for example a meter that legitimately reports constant values overnight), increase `stale_data_timeout`, or raise `retry_delay_mins` / lower `max_stale_attempts` to reduce repeat alerts.

#### Reconnect requests stop after a few attempts

While a source stays stale, MPG requests a reconnect (and sends an alert) at most `max_stale_attempts` times, at least `retry_delay_mins` apart. Once the limit is reached nothing more is sent until the data changes and the source becomes stale again.

### Metrics Edit Issues

#### "Not connected to InfluxDB -- bridge must be connected before editing metric values"

The Metrics Edit and Timeshift screens work through the running bridge's connection. Fix the bridge's connection first (check its Bridge Health panel and the log).

#### Edits apply to the wrong server

These screens use the first InfluxDB bridge of each version. If you have more than one bridge of the same version, only the first is edited.

#### v3 delete is rejected

Deleting rows requires InfluxDB 3 **Enterprise** with the upgraded storage engine and a token with delete permission on the database; see [How Editing and Deleting Actually Work](#how-editing-and-deleting-actually-work).

### Timeshift Data Screen Issues

#### "Import EG4 spreadsheet" is greyed out

- No `eg4_*` protocol is configured on this gateway — this source mode only applies to EG4 inverter exports.

#### Every column in Field Matchup starts as "Ignore this column"

- The Metric Match Confidence Threshold is set too high for how different the spreadsheet's column names are from the existing field names — lower it and re-scan, or map columns manually.
- The measurement/table has no existing fields yet (a brand-new destination) — in that case nothing is pre-matched; map each column manually.

#### Preview or Import fails outright

- No measurement/table was picked before uploading — pick one first; the Export/Import panels are disabled until then.
- No Time Column was selected, or Source Start wasn't set.
- For TimescaleDB, no Device was picked in the Device panel.
- For InfluxDB, the bridge is not connected (the import writes through the bridge's connection).

#### Import reports type mismatches

- The value being written conflicts with the field's already-recorded type on the destination (checked against `SHOW FIELD KEYS` for v1, `information_schema.columns` for v3). Turn on **Allow float coercion** to have compatible values nudged into the expected type automatically, or fix the source data.
