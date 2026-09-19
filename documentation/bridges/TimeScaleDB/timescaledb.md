# TimescaleDB Module for Multi Protocol Gateway

---

## Overview

The TimescaleDB module is a **transform / sink transport** for the Multi Protocol Gateway.  
Its primary responsibility is to:

- Receive telemetry data from an upstream scraper transport (e.g. Modbus TCP connected inverter)
- Persist time-series data into a **TimescaleDB (PostgreSQL)** backend
- Detect **stale data conditions**
- Trigger **automatic upstream and downstream reconnects** when data stops flowing
- Enable downstream visualization and analytics via **Grafana**

The module does **not** scrape data itself. Instead, it acts as a consumer of bridged data streams and focuses on persistence, monitoring, and reliability.

---

## Architecture Overview

``` text
[ Inverter / Device ]
          |
          v
[ Modbus / TCP Transport ]
          |
          v
[ Protocol Gateway ]
          |
          v
[ TimescaleDB Transport ]
          |
          v
[ TimescaleDB (Postgres) ]
          |
          v
[ Grafana ]
```

---

## What the TimescaleDB Module Does

### Core Responsibilities

- Converts incoming measurements into normalized rows
- Writes data into hypertables optimized for time-series workloads
- Maintains metadata for scraped devices
- Detects stale data conditions
- Requests upstream reconnects when stale data persists
- Backlogs data during database outages and replays that data on database recovery
- Provide Grafana-ready metrics for visualization

### Stale Data Handling

The module tracks:

- Time since last successful write
- Number of reconnect attempts
- Retry backoff interval

When data becomes stale:

1. A reconnect is requested from the Protocol Gateway
2. The upstream scraper transport is reset and a reconnection is tried
3. Scraping resumes automatically if the device is reachable

---

## 4. Database Schemas

  ! [Timescale DB Architecture](../../../documentation/architecture/mermaid-diagrams.md#timescaledb-telemetry-schema-created-by-timescaledb-bridge)

### 4.1 Narrow Table

One row per metric per timestamp.

| Column | Description |
| --- | --- |
| m_time | Timestamp |
| device_info_id | Device identifier ID |
| metric | Metric name |
| value | Metric value |

#### Narrow Table Benefits

- Ideal for Grafana
- Flexible schema
- Efficient aggregation

### 4.2 Wide Table (If Less than 160 metrics chosen via the MPG variable filters)

One row per timestamp with multiple metric columns.

| Column | Description |
| --- | --- |
| m_time | Timestamp |
| device_info_id | Device identifier ID |
| inverter_power | Example metric |
| grid_voltage | Example metric |
| panel_voltage | Example metric |
| etc. | etc. |

#### Wide Table Benefits

- Faster inserts
- Easier CSV/SQL exports

#### Wide Table Column Deletion

- You may add and subtract metrics from the wide table to your liking via the mask and screen settings detailed in the MPG readme.  However, if you subtract a metric from the timescaledb bridge, you should delete the column in the wide table that captures that metric.  

![Timescale Delete](../../../classes/WebServer/static/screenshots/timescale_delete.png)

> **Note:** Delete Columns changes a wide table's *shape* — it drops a column outright, for every device and every timestamp. To correct or remove specific *values* (e.g. a bad reading from a sensor fault, or data captured during a known test/outage) for one device over a chosen time range without touching the schema, use **Metrics Edit** instead — see section 4.5 below.

### 4.3 Device Info Table

| Column | Description |
| --- | --- |
| device_info_id | Device Identifier ID |
| device_identifier | Device or Inverter Identifier ** |
| device_serial_number | Device or Inverter Serial Number ** |
| device_name | Device of Inverter Informal Name ** |
| device_manufacturer | Device or Inverter Manufacturer |
| device_model | Device or Inverter Model |
| device_firmware | Device or Inverter Firmware |
| device_location | Device or Inverter Location |
| transport | Device or Inverter Scraper Transport |

** =  Determines uniqueness of the Device.

### 4.4 Metric Catalog

| Column | Description |
| --- | --- |
| id | Metric Unique ID |
| metric_name | Metric Name as shown in the MPG Registry |
| clean_column_name | Metric Name sanitized for SQL |
| data_type | Metric Data Type (default Double Precision) |
| created_at | Table Add Date |
| notes | metric name descriptions |

Here is a screen shot of how the schema looks in PGadmin.  The tables reside in the public folder.

![PGAdmin for TimescaleDB](pgAdminImage.png)

---

### 4.5 Metrics Edit — Editing or Deleting Historical Metric Values

The **Timescale DB → Metrics Edit** admin screen lets an administrator correct or remove specific metric *values* — for one device, over a chosen date/time range — on either the shared narrow table or a wide table, without changing either table's schema. This is the tool to reach for when a sensor fault, a mis-wired input, a device test, or a known outage window put bad or unwanted values into the database and you want them fixed or cleared, as opposed to Wide Table Column Deletion (section 4.2), which permanently drops an entire metric column, table-wide, for every device and timestamp.

![Timescale Metrics Edit](../../../classes/WebServer/static/screenshots/timescale_metrics_edit.png)

#### Using the Metrics Edit Screen

1. Open **Timescale DB → Metrics Edit** from the admin menu.
2. Pick a **table** on the left — either the shared `device_metrics_narrow` table, or one wide-table protocol.
3. Pick the **device** whose data you want to edit.
4. Check the **field(s)** (metric names, or wide-table columns) to target.
5. Pick a **start** and **end** date/time for the range to affect.
6. Choose an **action**:
   - **Delete value(s)** — clears the selected field(s) over the range.
   - **Set value** — overwrites the selected field(s) with a replacement value you enter.
7. Click **Preview** to see how many rows match and a sample of their current values before changing anything.
8. Click **Add to Staged Changes**.
9. Use the existing **Commit All Changes** button in the header to apply every staged Metrics Edit (and any staged Delete Columns changes) at once. Nothing is written to TimescaleDB before this step — staged edits can be reviewed and individually removed from the staged-changes list, or abandoned entirely with **Discard Changes**.

#### Narrow vs. Wide Behavior

The two table shapes require slightly different semantics for "delete," since a narrow row holds a single metric while a wide row holds every metric for that device/timestamp:

| Table | Delete | Set Value |
| --- | --- | --- |
| Narrow (`device_metrics_narrow`) | Removes the matching `(m_time, device_info_id, metric_name)` rows outright | Overwrites `metric_value` (numeric) or `metric_ascii` (text) for the matching rows |
| Wide (`device_metrics_wide__*`) | Sets the selected column(s) to `NULL` for the matching rows — the row itself can't be removed, since it also holds every other metric recorded at that timestamp | Overwrites the selected column(s) in place |

#### Value Type Validation

A replacement value entered for **Set Value** is checked against the field's type before it is even staged:

- **Wide table columns** are checked against their declared `metric_catalog.data_type` (e.g. an `INTEGER` column rejects non-whole numbers and out-of-range values, a `BOOLEAN` column only accepts recognizable spellings like `true`/`false`, a `TEXT` column accepts anything).
- **Narrow table metrics** have no fixed declared type (every metric shares the same `metric_value`/`metric_ascii` pair), so the screen instead infers numeric vs. text from what's already been recorded for that device/metric, and validates the new value against that inference.

An invalid value is rejected immediately, with a clear error, rather than only surfacing when Commit All Changes is pressed.

#### What Happens on Commit

Applying a Metrics Edit runs through the same category of safety measures as a wide-table column deletion:

1. Data ingestion is briefly paused so the write path can't race the edit.
2. Any compression job configured for the affected table is paused, and just the chunks overlapping the edited time range are decompressed (not the whole table, since the edit never writes outside that range).
3. The delete/update itself runs under the same schema advisory lock used for structural changes, so it can't race a concurrent Delete Columns commit against the same table.
4. The hourly/daily/weekly/monthly rollup views covering the edited time range are refreshed afterward, so pre-aggregated rollups reflect the correction rather than continuing to serve stale numbers for that period.
5. The paused compression job is resumed automatically, whether or not the edit succeeded.

No row data outside the selected device, fields, and time range is ever touched.

### 4.6  Rebuilds:  Compression and Rollups

#### Rebuild Compression

The **Timescale DB → Rebuild Compression** admin screen decompresses and recompresses every already-compressed chunk of a raw table (narrow or wide) and its four rollup views, in place, against whatever compression settings are configured **right now**. It never touches a view's definition and never adds, removes, or modifies a single row of data — this is purely a rewrite of how existing rows are stored on disk.

You need this after a change that alters a table's physical layout but doesn't retroactively apply to data already compressed:

- Changing `compress_segmentby` / `compress_orderby` in `hypertable_defaults` — new chunks pick up the change automatically, but chunks compressed under the old settings won't until they're rewritten.
- Running **Wide Table Column Deletion** or **Metrics Edit** against a wide table — the dropped/edited columns are gone from new compressed chunks, but older compressed chunks still carry the old column layout internally until rewritten.

![Timescale Rebuild Compression](../../../classes/WebServer/static/screenshots/timescale_rebuild_compression.png)

##### Using the Rebuild Compression Screen

1. Open **Timescale DB → Rebuild Compression** from the admin menu.
2. Check the group(s) to rebuild — the shared narrow stack, and/or one or more wide-table protocols. **Select all** / **Select none** are provided for convenience.
3. Click **Rebuild Compression**. You'll be asked to confirm, since this touches every compressed chunk in the selected group(s) — the operation itself is safe (no data is lost), but it can take a while on a large table.
4. Progress streams live as chunks are rewritten, with a single progress bar covering every selected group.
5. When finished, each group (and each table within it — the raw table plus its hourly/daily/weekly/monthly rollup views) reports its size before and after, and the percentage reduced.

##### What Gets Touched

For each selected group, every table in its stack — the raw narrow/wide table, plus its hourly, daily, weekly, and monthly rollup views — is checked for compressed chunks. Only chunks TimescaleDB already reports as compressed are touched; the newest chunk(s), still inside their `compress_after` window and not yet compressed by the background policy, are left alone. Each touched chunk is decompressed and immediately recompressed against the hypertable's current compression settings.

##### Rollup Progress and Results

Progress is weighted by each table's on-disk byte size rather than by a simple chunk count, since chunk counts aren't comparable across a group's members — a rollup view routinely has several times as many chunks as its raw table for the same span of time, while the raw table's individual chunks are much larger. Splitting each table's own known size evenly across its own chunks gives a progress bar that advances smoothly instead of racing through one table and stalling on another.

Each table's scheduled compression job is paused only while that table's own chunks are being rewritten, and is always resumed afterward, whether or not every chunk succeeded. Every chunk is attempted independently — one chunk failing to decompress or recompress (for example, due to a momentary lock conflict) does not stop the rest of that table, the rest of its group, or any other selected group.

#### Rebuild Rollup Views

The **Timescale DB → Rebuild Rollup Views** admin screen manages the four-tier continuous aggregate rollups (hourly → daily → weekly → monthly) built on top of the narrow table and each wide table. Each tier is materialized from the one below it, so the whole hourly/daily/weekly/monthly stack for a given source table is always treated as a single unit — there's no way to rebuild or refresh just one tier in isolation without risking it being built against a stale or mismatched source.

![Timescale Rebuild Rollups](../../../classes/WebServer/static/screenshots/timescale_rebuild_rollups.png)

##### Using the Rebuild Rollup Views Screen

1. Open **Timescale DB → Rebuild Rollup Views** from the admin menu.
2. Check the group(s) to act on — the shared narrow stack, and/or one or more wide-table protocols. **Select all** / **Select none** are provided for convenience.
3. Choose one of the three actions below. **Force Rebuild** asks for confirmation first, since it always drops and recreates every selected group's views regardless of whether anything looks wrong.
4. Progress streams live as each group (or, for **Refresh Now**, each individual view) is processed.
5. When finished, the screen reports each group's/view's outcome, including whether it was actually changed or left as-is.

##### The Three Actions

| Action | What It Does | When To Use It |
| --- | --- | --- |
| **Refresh Now** | Pulls the latest raw data into each selected view's *existing* definition (`CALL refresh_continuous_aggregate`) — the same thing the background refresh policy does on its own schedule. Never drops or recreates a view. | Routine catch-up between scheduled refreshes, or after a **Metrics Edit** corrected historical values outside a view's normal incremental refresh window. |
| **Rebuild Rollups** | Purges and fully re-materializes a selected group's whole rollup stack, but only for groups that actually need it — a missing view, or one whose bucket configuration no longer matches `config.cfg`. A group that already checks out is left untouched. | After changing rollup bucket/backfill settings, or after wide-table columns changed via **Delete Columns**/**Metrics Edit** and the rollups look out of sync. |
| **Force Rebuild** | Purges and fully re-materializes every selected group's whole stack unconditionally, regardless of whether it looked out of date. | When you suspect drift or corruption the normal check wouldn't catch, or you simply want a guaranteed clean rebuild. |

##### Why Whole Stacks, Not Individual Views

The daily rollup is built from the hourly rollup, the weekly rollup from the daily, and the monthly rollup from the weekly. Because of that hierarchy, rebuilding or force-rebuilding always operates at the level of a whole source-table stack (the shared narrow stack, or one wide-table protocol) — never an individual hourly/daily/weekly/monthly view on its own — so a rebuilt tier is never left pointing at a stale or mismatched layer beneath it. **Refresh Now** is the exception: since it never drops or recreates anything, it can and does report progress per individual view.

##### Progress and Results

**Rebuild Rollups** and **Force Rebuild** report progress per group, since each one delegates to the same internal setup routine the bridge uses on startup/reconnect, which rebuilds its whole stack as a single step. **Refresh Now** reports progress per individual view, since it already loops over each one independently. In every case, each group or view is attempted on its own — one failure doesn't block the rest of the selection from completing.

---

## 5. Example SQL Queries

### 5.1 Power Over Time

```sql
SELECT
  time_bucket('1 minute', m_time) AS t,
  avg(metric_value) AS power
FROM public.device_metrics_narrow
WHERE metric_name = 'pload'
GROUP BY t
ORDER BY t;
```

### 5.2 Daily Energy Estimate

```sql
SELECT
  date_trunc('day', m_time) AS day,
  sum(metric_value) * 1/60 AS kwh
FROM public.device_metrics_narrow
WHERE metric_name = 'pload'
GROUP BY day
ORDER BY day;
```

### 5.3 Device Health (Last Seen)

```sql
SELECT
  device_info_id,
  max(m_time) AS last_seen
FROM device_metrics_narrow
GROUP BY device_info_id;
```

### 5.4 All metrics from public.device_metrics_wide

```sql
SELECT * FROM public.device_metrics_wide__eg4_18kpv
ORDER BY m_time ASC, device_info_id ASC 
```

---

## 6. Docker Compose Installation

### 6.1 Images Used in the stack

- **TimescaleDB HA:** `timescaledb-ha:pg18`  The Timescale DB Application
     Note that currently this image is approximately 4.5 gb in size.
- **Protocol Gateway:** `buxtoncalvin/multiprotocolgateway:latest` The MPG application/inverter scraper
- **Grafana:** `grafana/grafana:latest`   The graphing application
- **PostGres Admin:** `dpage/pgadmin4:latest` The database administration application

### 6.2 Example docker-compose.yml

```yaml
version: "3.9"

services:

  18kPV_timescaledb:
    container_name: 18kPV_timescaledb
    image: buxtoncalvin/multiprotocolgateway:latest
    restart: always
    security_opt:
    - apparmor:unconfined
    environment:
    - TZ=America/Los_Angeles
    volumes:
    - /home/multiprotocolgateway4/config:/app/config
    - /home/multiprotocolgateway4/protocols:/app/protocols
    - /home/multiprotocolgateway4/backlogs:/app/backlogs
    - /home/multiprotocolgateway4/logs:/app/logs

    ports:
    - "1717:1717"
    expose:
    - "1717"   
    depends_on:
    - timescaledb
    logging:
    driver: "json-file"
    options:
      max-size: "10m" 
      max-file: "3


  timescaledb:
    image: timescale/timescaledb-ha:pg18
    environment:
      POSTGRES_PASSWORD: your-password
      POSTGRES_USER: your-user-name
      POSTGRES_DB: solar (or your database name)
    ports:
      # we change the access port here to allow for other postgres dbs
      - "5431:5432"
    volumes:
   # note the ha version of timescaledb uses a different data storage path compared to the standard postgres database
   # so the volume is mapped in the environment variable to account for any future changes to the path-- which as of 3/3/2026 doesn't work. So direct mapping to timescaledb-ha data folder: /home/postgres/pgdata  
   #- /home/timescaledb:/var/lib/postgresql/data
   # current data path in timescale.
   - /home/timescaledb:/home/postgres/pgdata
  
  grafana:
    container_name: grafana
    image: grafana/grafana:latest
    restart: always
    security_opt:
      - apparmor:unconfined
    ports:
      - 3000:3000
    env_file:
      - '/home/grafana/env.grafana'
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_SECURITY_ALLOW_EMBEDDING=true
      - GF_DATABASE_USER=your-user-name
      - GF_DATABASE_PASSWORD=your-password
    user: '1000'    
    depends_on:
      - timescaledb
    volumes:
      - /home/grafana:/var/lib/grafana      

  postgres_admin:
    image: dpage/pgadmin4:latest 
    container_name: pgadmin
    restart: always
    security_opt:
      - apparmor:unconfined     
    environment:
    PGADMIN_DEFAULT_EMAIL: Blah@Gmail.Com
    PGADMIN_DEFAULT_USER: your-user-name
    PGADMIN_DEFAULT_PASSWORD: your-password
    PGADMIN_DISABLE_POSTFIX: true
    volumes:
      - /home/pgadmin:/var/lib/pgadmin
    ports:
      #  set the port to 8181 to avoid typical port 80 conflicts
      - "8181:80" 
    depends_on:
      - timescaledb     

```

---

## General configuration

### 6.3 Grafana Setup

```text

- Open Grafana: <http://localhost:3000>
- Login: admin / admin   or your username/password
- Add data source:
- Type: PostgreSQL
- Host: timescaledb:5431
- Database: metrics
- User: your-TSDB user-name
- Password: your-TSDB-password
- SSL: disabled
- Enable TimescaleDB option
- Create panels using SQL queries

```

### 6.4 MPG Configuration File Simple General (config.cfg)

```ini
[general]
read_mode = sequential

[logging]
log_dir = logs
log_file = gateway.log
level = INFO
# weekly | daily | size
rotation = weekly         
# Monday rollover
when = W0                  
interval = 1
# keep 4 weeks
backup_count = 4           
# 100MB (only if size-based)
max_bytes = 104857600      
console = true

# can be any name in the format transport.<name>
# changing the name will result in a new device being created in the Timescale DB.
[transport.Inverter]
log_level = DEBUG
transport = modbus_tcp
protocol_version = eg4_18kpv
host = 10.17.2.65
port = 502
bridge = transport.timescaledb
read_interval = 15

# Device descriptions used by Timescale for scraping an inverter/device.
manufacturer = EG4
model = 18KPV
serial_number = 4066670074
location = home
name = EG4 18kpv1
# If you want to retrieve the serial number from the inverter, uncomment the below Serial_Number and comment the above.
# You must include Serial_Number in the list in the variable_mask file along with any other variables you want to capture.
# or leave the variable_mask file blank to capture all variables.

# Serial_Number =

[transport.timescaledb]
log_level = DEBUG
transport = timescaledb
host = 10.17.2.42
port = 5431
database = solar1
username =  your-username
password = your-password

### All of the below are optional and are set to defaults if not specified
# force float coerces all values obtained to be of the float type.
force_float = true

# persistent backlog settings
enable_persistent_storage = true
backlog_storage_path = backlogs
backlog_file_name = no_connect_timescale_backlog

# max data points to store in backlog
max_backlog_size = 10000
# seconds-->  equal to 24 hours
max_backlog_age = 86400 

# TSDB Connection monitoring settings
reconnect_attempts = 5
# minutes
reconnect_delay = 5

# Exponential backoff settings (reconnect delay increases exponentially on each failure)
use_exponential_backoff = true
# minutes --> 5 hours
max_reconnect_delay = 300

## hypertable and rollup options
# changing rollup settings after data has been written, will result in automatic view deletions and rebuilds
migrate_data = True
enable_compression = True
enable_dynamic_chunk_sizing = True
enable_rollups = True
# Seconds between rollup refreshes (6 hours)
auto_refresh_interval = 21600
enable_auto_refresh = True
drop_after = 1 year

# stale data cleanup settings minutes --> 5 hours
stale_data_timeout = 300

# pushover settings / leave blank and disable if you don't use pushover
enable_pushover = True
pushover_token = your_token_here
pushover_user = your_user_key_here
# tells MPG to wait until all metrics have been read to write data to timescaledb
write_requires_complete_cycle = True
```

### 6.5 UTC Timestamp Toggle Feature

#### Timestamp Overview

This feature allows you to configure the TimescaleDB transport to use UTC timestamps instead of the local machine timezone for all time-series data. This is particularly useful for:

- Multi-site deployments across different timezones
- Consistency in timestamp comparison and aggregation
- Simplified rollup boundary calculations
- Easier data interpretation and querying

#### Configuration

##### Setting the UTC Timestamp Mode

Add the following option to your TimescaleDB transport configuration in `config.cfg`:

```ini
[timescaledb_section_name]
host = localhost
port = 5432
database = solar
username = postgres
password = your_password

# Enable UTC timestamps (default: False)
use_utc_timestamp = True
```

##### Default Behavior

- **Default Value**: `False` (uses local machine timezone)
- **Backward Compatible**: Existing deployments continue to use local timezone unless explicitly configured
- **Per-Transport**: The setting is configured per TimescaleDB transport instance

#### How It Works

##### Timestamp Generation

When `use_utc_timestamp = True`:

- All new timestamps are generated in UTC timezone using `datetime.now(timezone.utc)`
- All database timestamp fields use UTC-aware datetime objects

When `use_utc_timestamp = False` (default):

- Timestamps use the local machine timezone via `datetime.now().astimezone()`
- Behavior matches the original implementation

##### Affected Components

###### Database Tables

The following timestamp columns are affected:

- **ProtocolRegistry**: `created_at`, `updated_at`, `last_refresh_at`
- **MetricCatalog**: `created_at`
- **DeviceInfo**: `created_at`, `updated_at`
- **DeviceMetricsNarrow**: `m_time` (primary key - time-series data)
- **DeviceMetricsWide**: `m_time` (time-series data)

###### Rollup Calculations

- The rollup system uses `anchor_start_time_utc` (already UTC-based)
- Time bucket boundaries are calculated correctly with UTC timestamps or local timestamps
- Hierarchical continuous aggregates (hourly → daily → weekly → monthly) work seamlessly

###### Stale Data Detection

- Timestamp comparisons in stale data detection continue to work correctly
- The elapsed time calculation uses the same timezone consistently

#### Implementation Details

##### Initialization Flow

1. Configuration is read from `config.cfg`
2. `use_utc_timestamp` setting is loaded in `TimescaleDB.__init__()`
3. `configure_application_timezone` is called to set the global flag
4. All subsequent timestamp generations of _now_tz use the configured timezone

#### Boundary Conditions and Rollup Alignment

##### Time Bucket Boundaries

TimescaleDB's `time_bucket()` function works with timezone-aware timestamps. The system:

- Uses `anchor_start_time_utc = "2000-01-01 00:00:00+00"` for all rollups
- Aligns hourly buckets to UTC midnight boundaries
- Hierarchically depends on previous aggregates (hourly → daily → weekly → monthly)

##### Example Rollup Bucket Sizes

```ini
- Hourly Rollup: 1 hour bucket, starts 3 hours ago
- Daily Rollup: 1 day bucket, starts 3 days ago
- Weekly Rollup: 1 week bucket, starts 3 weeks ago
- Monthly Rollup: 1 month bucket, starts 3 months ago
```

Whether using UTC or local timezone, these bucket boundaries remain consistent and aligned.

##### Data Consistency

- **No Mixed Timezones**: All timestamps in a transport instance use the same timezone
- **Timezone-Aware**: All datetime objects include timezone information
- **No Conversion Loss**: UTC timestamps have full precision without DST ambiguity

#### Migration Considerations

##### Switching from Local to UTC

⚠️ **Important**: Switching the `use_utc_timestamp` setting after data has been collected will result in:

- Existing data retains its original timezone
- New data uses the new timezone setting
- A temporal discontinuity at the transition point

**Recommendation**:

- Set the timezone mode before data collection begins, or
- Create a new database/transport instance if changing timezones mid-operation

##### Data Continuity

If you must transition:

1. Document the switch time
2. Consider data export/reimport with timezone conversion
3. Set up separate rollup views if needed to handle the transition period

#### Usage Examples

##### Configuration Example 1: UTC for Cloud Deployment

```ini
[timescaledb]
host = cloud-tsdb.example.com
port = 5432
database = solar
username = cloud_user
password = ${TSDB_PASSWORD}
use_utc_timestamp = True
enable_rollups = True
auto_refresh_interval = 21600
```

##### Configuration Example 2: Local Timezone (Default)

```ini
[timescaledb]
host = localhost
port = 5432
database = solar
username = postgres
password = postgres
use_utc_timestamp = False  # Default, can be omitted
```

##### Programmatic Configuration

```python
from configparser import ConfigParser
from MultiProtocolGateway.classes.transports.timescaledb import TimescaleDB

config = ConfigParser()
config.read('config.cfg')

# Enable UTC timestamps
config['timescaledb']['use_utc_timestamp'] = 'True'

# Initialize transport
tsdb_transport = TimescaleDB(config['timescaledb'])
# Timestamps will now use UTC
```

#### Query Examples

##### Querying Data with Different Timezones

```sql
-- When using UTC timestamps, all comparisons are in UTC
-- Query data from the last 24 UTC hours
SELECT * FROM device_metrics_narrow
WHERE m_time >= NOW() - INTERVAL '1 day'
ORDER BY m_time DESC;

-- Time zone conversion on query (if needed for display)
SELECT 
    m_time AT TIME ZONE 'US/Eastern' as local_time,
    metric_name,
    metric_value
FROM device_metrics_narrow
WHERE m_time >= NOW() - INTERVAL '1 day'
ORDER BY m_time DESC;
```

##### Verifying Timestamp Timezone

```sql
-- Check the timezone of timestamps
SELECT 
    m_time,
    timezone(m_time) as tz,
    m_time AT TIME ZONE 'UTC' as in_utc
FROM device_metrics_narrow
LIMIT 1;
```

#### Troubleshooting

##### Issue: Timestamps Still in Local Timezone

**Cause**: Configuration not reloaded or transport not restarted
**Solution**: Restart the transport service after changing the configuration

##### Issue: Rollup Views Not Updating

**Cause**: Timezone boundary misalignment in legacy data
**Solution**: Verify `anchor_start_time_utc` setting and consider dropping/recreating views

##### Issue: Historical Data Shows Wrong Timezone

**Cause**: Settings changed mid-operation
**Solution**: This is expected behavior - see "Migration Considerations" section

#### Technical Notes

##### Why UTC for Rollups?

- **Consistency**: UTC eliminates DST (Daylight Saving Time) complications
- **Simplicity**: Hour boundaries are always at UTC hour marks
- **Correctness**: No ambiguous times during DST transitions
- **Interoperability**: Works seamlessly across timezones

##### DateTime Behavior

- **`datetime.now(timezone.utc)`**: Returns current time in UTC with UTC tzinfo
- **`datetime.now().astimezone()`**: Returns current time in local timezone with local tzinfo
- **`datetime.now()`**: Returns naive datetime (no timezone info) - NOT USED

All timestamps generated by this system are timezone-aware to prevent ambiguity.

#### References

- [Python datetime.timezone documentation](https://docs.python.org/3/library/datetime.html#datetime.timezone)
- [TimescaleDB time_bucket function](https://docs.timescaledb.com/latest/api/#time_bucket)
- [TimescaleDB continuous aggregates](https://docs.timescaledb.com/latest/overview/continuous-aggregates/)

---

## Summary

The TimescaleDB module provides a production-grade ingestion and monitoring layer that integrates cleanly with the Multi Protocol Gateway. It is designed to be predictable, observable, and resilient — pairing naturally with inverter telemetry, industrial sensors, and edge data collection workloads.

The TimescaleDB module provides:

- Reliable time-series persistence
- Automatic stale data detection
- Self-healing reconnect behavior
- Support for Grafana visualization
