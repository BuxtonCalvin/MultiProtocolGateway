# InfluxDB Bridge

The InfluxDB bridge lets MPG send device data to an InfluxDB v1 or v3 server for time-series storage and visualization, and gives admins tools to correct historical data and shift date ranges. This document covers the output transport itself, its reliability features, the admin UI's Metrics Edit and Timeshift Data screens, and troubleshooting for all of the above.

## Table of Contents

1. [Output Transport](#output-transport)
2. [Advanced Features: Exponential Backoff & Persistent Storage](#advanced-features-exponential-backoff--persistent-storage)
3. [Metrics Edit — Editing or Deleting Historical Metric Values](#metrics-edit--editing-or-deleting-historical-metric-values)
4. [Time-Shifted Data Import / Export](#time-shifted-data-import--export)
5. [Troubleshooting](#troubleshooting)

See also: [InfluxDB3.md](InfluxDB3.md) for InfluxDB 3-specific setup, and [config.influxdb.example](config.influxdb.example) for a sample bridge config.

---

## Output Transport

The InfluxDB output transport allows you to send data from your devices directly to an InfluxDB v1 server for time-series data storage and visualization.

### Features

- **Batch Writing**: Efficiently batches data points to reduce network overhead
- **Automatic Database Creation**: Creates the database if it doesn't exist
- **Device Information Tags**: Includes device metadata as InfluxDB tags for easy querying
- **Flexible Data Types**: Automatically converts data to appropriate InfluxDB field types
- **Configurable Timeouts**: Adjustable batch size and timeout settings
- **Connection Monitoring**: Automatic connection health checks and reconnection logic
- **Robust Error Handling**: Retries failed writes after reconnection attempts

### Configuration

#### Basic Configuration

```ini
[influxdb_output]
type = influxdb_out
host = localhost
port = 8086
database = solar
measurement = device_data
```

#### Advanced Configuration

```ini
[influxdb_output]
type = influxdb_out
host = localhost
port = 8086
database = solar
username = admin
password = your_password
measurement = device_data
include_timestamp = true
include_device_info = true
batch_size = 100
batch_timeout = 10.0
log_level = INFO

# Connection monitoring settings
reconnect_attempts = 5
reconnect_delay = 5.0
connection_timeout = 10
```

#### Configuration Options

| Option | Default | Description |
| --- | --- | --- |
| `host` | `localhost` | InfluxDB server hostname or IP address |
| `port` | `8086` | InfluxDB server port |
| `database` | `solar` | Database name (will be created if it doesn't exist) |
| `username` | `` | Username for authentication (optional) |
| `password` | `` | Password for authentication (optional) |
| `measurement` | `device_data` | InfluxDB measurement name |
| `include_timestamp` | `true` | Include timestamp in data points |
| `include_device_info` | `true` | Include device information as tags |
| `batch_size` | `100` | Number of points to batch before writing |
| `batch_timeout` | `10.0` | Maximum time (seconds) to wait before flushing batch |
| `reconnect_attempts` | `5` | Number of reconnection attempts before giving up |
| `reconnect_delay` | `5.0` | Delay between reconnection attempts (seconds) |
| `connection_timeout` | `10` | Connection timeout for InfluxDB client (seconds) |

### Connection Monitoring

The InfluxDB transport includes robust connection monitoring to handle network issues and server restarts:

#### Automatic Health Checks

- Performs connection health checks every 30 seconds
- Uses InfluxDB ping command to verify connectivity
- Automatically attempts reconnection if connection is lost

#### Reconnection Logic

- Attempts reconnection up to `reconnect_attempts` times
- Waits `reconnect_delay` seconds between attempts
- Preserves buffered data during reconnection attempts
- Retries failed writes after successful reconnection

#### Error Recovery

- Gracefully handles network timeouts and connection drops
- Maintains data integrity by not losing buffered points
- Provides detailed logging for troubleshooting

### Data Structure

The InfluxDB output creates data points with the following structure:

#### Tags (if `include_device_info = true`)

- `device_identifier`: Device serial number (lowercase)
- `device_name`: Device name
- `device_manufacturer`: Device manufacturer
- `device_model`: Device model
- `device_serial_number`: Device serial number
- `transport`: Source transport name

#### Fields

All device data values are stored as fields. The transport automatically converts:

- Numeric strings to integers or floats
- Non-numeric strings remain as strings

#### Time

- Uses current timestamp in nanoseconds (if `include_timestamp = true`)
- Can be disabled for custom timestamp handling

### Example Bridge Configuration

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
bridge = influxdb_output

# InfluxDB output
[influxdb_output]
type = influxdb_out
host = localhost
port = 8086
database = solar
measurement = inverter_data
```

### Installation without docker

1. Install the required dependency:

   ```bash
   pip install influxdb or install influxdb3-python
   ```

2. Or add to your requirements.txt:

   ``` ini
   influxdb
   influxdb3-python
   ```

### InfluxDB Setup

1. Install InfluxDB v1:

   ```bash
   # Ubuntu/Debian
   sudo apt install influxdb influxdb-client
   sudo systemctl enable influxdb
   sudo systemctl start influxdb
   
   # Or download from https://portal.influxdata.com/downloads/
   ```

2. Create a database (optional - will be created automatically):

   ```bash
   echo "CREATE DATABASE solar" | influx
   ```

### Querying Data

Once data is flowing, you can query it using InfluxDB's SQL-like query language:

```sql
-- Show all measurements
SHOW MEASUREMENTS

-- Query recent data
SELECT * FROM device_data WHERE time > now() - 1h

-- Query specific device
SELECT * FROM device_data WHERE device_identifier = '123456789'

-- Aggregate data
SELECT mean(value) FROM device_data WHERE field_name = 'battery_voltage' GROUP BY time(5m)
```

### Integration with Grafana

InfluxDB data can be easily visualized in Grafana:

1. Add InfluxDB as a data source in Grafana
2. Use the same connection details as your configuration
3. Create dashboards using InfluxDB queries

For connection issues, authentication problems, data not appearing, or performance tuning, see [Troubleshooting](#troubleshooting) below — it covers the Output Transport in detail, including the "Performance" tuning notes for `batch_size`/`batch_timeout`/`connection_timeout` that used to live here.

---

## Advanced Features: Exponential Backoff & Persistent Storage

### Overview

The InfluxDB transport now includes advanced features to handle network instability and long-term outages:

1. **Exponential Backoff**: Intelligent reconnection timing to avoid overwhelming the server
2. **Persistent Storage**: Local data storage to prevent data loss during extended outages
3. **Periodic Reconnection**: Regular connection health checks even during quiet periods

### Exponential Backoff

#### How It Works

Instead of using a fixed delay between reconnection attempts, exponential backoff increases the delay exponentially:

- **Attempt 1**: 5 seconds delay
- **Attempt 2**: 10 seconds delay  
- **Attempt 3**: 20 seconds delay
- **Attempt 4**: 40 seconds delay
- **Attempt 5**: 80 seconds delay (capped at max_reconnect_delay)

#### Configuration

```ini
[influxdb_output]
# Enable exponential backoff
use_exponential_backoff = true

# Base delay between attempts (seconds)
reconnect_delay = 5.0

# Maximum delay cap (seconds)
max_reconnect_delay = 300.0

# Number of reconnection attempts
reconnect_attempts = 5
```

#### Benefits

- **Reduces Server Load**: Prevents overwhelming the InfluxDB server during recovery
- **Network Friendly**: Respects network conditions and server capacity
- **Configurable**: Adjust timing based on your environment

#### Example Scenarios

##### Short Network Glitch

``` ini
Attempt 1: 5s delay → Success
Total time: ~5 seconds
```

##### Server Restart

``` ini
Attempt 1: 5s delay → Fail
Attempt 2: 10s delay → Fail  
Attempt 3: 20s delay → Success
Total time: ~35 seconds
```

##### Extended Outage

``` ini
Attempt 1: 5s delay → Fail
Attempt 2: 10s delay → Fail
Attempt 3: 20s delay → Fail
Attempt 4: 40s delay → Fail
Attempt 5: 80s delay → Fail
Total time: ~155 seconds, then data stored in backlog
```

### Periodic Reconnection

#### How It Works

Periodic reconnection ensures the connection to InfluxDB remains healthy even during periods when no data is being written:

- **Regular Health Checks**: Performs connection tests at configurable intervals
- **Connection Refresh**: Re-establishes connection even if it appears healthy
- **Quiet Period Handling**: Maintains connection during low-activity periods
- **Proactive Recovery**: Detects and fixes connection issues before data loss

#### Configuration

``` ini
[influxdb_output]
# Periodic reconnection interval (seconds)
periodic_reconnect_interval = 14400.0  # 4 hours (default)

# Disable periodic reconnection
periodic_reconnect_interval = 0
```

#### Benefits

- **Connection Stability**: Prevents connection timeouts during quiet periods
- **Proactive Monitoring**: Detects issues before they affect data transmission
- **Network Resilience**: Handles network changes and server restarts
- **Configurable**: Adjust interval based on your environment

#### Example Scenarios

##### Quiet Periods (No Data)

``` ini
10:00 AM: Last data written
11:00 AM: Periodic reconnection check → Connection healthy
12:00 PM: Periodic reconnection check → Connection healthy
01:00 PM: Periodic reconnection check → Connection healthy
02:00 PM: New data arrives → Immediate transmission
```

##### Network Issues During Quiet Period

``` ini
10:00 AM: Last data written
11:00 AM: Periodic reconnection check → Connection failed
11:00 AM: Attempting reconnection → Success
12:00 PM: Periodic reconnection check → Connection healthy
```

##### Server Restart During Quiet Period

``` ini
10:00 AM: Last data written
11:00 AM: Periodic reconnection check → Connection failed
11:00 AM: Attempting reconnection → Success (server restarted)
12:00 PM: Periodic reconnection check → Connection healthy
```

### Persistent Storage (Data Backlog)

#### How It Works

When InfluxDB is unavailable, data is stored locally in pickle files:

1. **Data Collection**: Points are stored in memory and on disk
2. **Automatic Cleanup**: Old data is removed based on age limits
3. **Recovery**: When connection is restored, backlog is flushed to InfluxDB
4. **Size Management**: Backlog is limited to prevent disk space issues

#### Configuration

```ini
[influxdb_output]
# Enable persistent storage
enable_persistent_storage = true

# Storage directory (relative to gateway directory)
persistent_storage_path = backlogs

# Maximum number of points to store
max_backlog_size = 10000

# Maximum age of points in seconds (24 hours)
max_backlog_age = 86400
```

#### Storage Structure

``` ini
backlogs/
├── influxdb_backlog_influxdb_output.pkl
├── influxdb3_backlog_influxdb_output.pkl
└── ...
```

#### Data Recovery Process

1. **Connection Lost**: Data continues to be collected and stored locally
2. **Reconnection**: When InfluxDB becomes available, backlog is detected
3. **Batch Upload**: All stored points are sent to InfluxDB in batches
4. **Cleanup**: Backlog is cleared after successful upload

#### Example Recovery Log

``` ini
[2024-01-15 10:30:00] Connection check failed: Connection refused
[2024-01-15 10:30:00] Not connected to InfluxDB, storing data in backlog
[2024-01-15 10:30:00] Added point to backlog. Backlog size: 1
...
[2024-01-15 18:45:00] Attempting to reconnect to InfluxDB at localhost:8086
[2024-01-15 18:45:00] Successfully reconnected to InfluxDB
[2024-01-15 18:45:00] Flushing 2847 backlog points to InfluxDB
[2024-01-15 18:45:00] Successfully wrote 2847 backlog points to InfluxDB
```

### Configuration Examples

#### For Stable Networks (Local InfluxDB)

```ini
[influxdb_output]
transport = influxdb_out
host = localhost
port = 8086
database = solar

# Standard reconnection
reconnect_attempts = 3
reconnect_delay = 2.0
use_exponential_backoff = false

# Periodic reconnection
periodic_reconnect_interval = 1800.0  # 30 minutes

# Minimal persistent storage
enable_persistent_storage = true
max_backlog_size = 1000
max_backlog_age = 3600  # 1 hour

# timestamps metric data with either the local machine time or UTC time.
use_utc_timestamp = True

# Stale data detection settings
stale_data_timeout = 300       # seconds before data is considered stale
max_stale_attempts = 3         # max reconnect attempts per stale period
retry_delay_mins = 5           # minimum minutes between stale reconnect attempts
```

#### For Unstable Networks (Remote InfluxDB)

```ini
[influxdb_output]
transport = influxdb_out
host = remote.influxdb.com
port = 8086
database = solar

# Aggressive reconnection with exponential backoff
reconnect_attempts = 10
reconnect_delay = 5.0
use_exponential_backoff = true
max_reconnect_delay = 600.0  # 10 minutes

# Frequent periodic reconnection
periodic_reconnect_interval = 900.0  # 15 minutes

# Large persistent storage for extended outages
enable_persistent_storage = true
max_backlog_size = 50000
max_backlog_age = 604800  # 1 week
```

#### For High-Volume Data

```ini
[influxdb_output]
transport = influxdb_out
host = localhost
port = 8086
database = solar

# Fast reconnection for high availability
reconnect_attempts = 5
reconnect_delay = 1.0
use_exponential_backoff = true
max_reconnect_delay = 60.0

# Less frequent periodic reconnection (data keeps connection alive)
periodic_reconnect_interval = 14400.0  # 4 hours (default)

# Large backlog for high data rates
enable_persistent_storage = true
max_backlog_size = 100000
max_backlog_age = 86400  # 24 hours

# Optimized batching
batch_size = 500
batch_timeout = 5.0
```

### Monitoring and Maintenance

#### Check Backlog Status

```bash
# Check backlog file sizes
ls -lh backlogs/

# Check backlog contents (Python script)
python3 -c "
import pickle
import os
for file in os.listdir('backlogs'):
    if file.endswith('.pkl'):
        with open(f'backlogs/{file}', 'rb') as f:
            data = pickle.load(f)
            print(f'{file}: {len(data)} points')
"
```

#### Monitor Logs

```bash
# Monitor backlog activity
grep -i "backlog\|persistent" /var/log/protocol_gateway.log

# Monitor reconnection attempts
grep -i "reconnect\|exponential" /var/log/protocol_gateway.log

# Monitor periodic reconnection
grep -i "periodic.*reconnect" /var/log/protocol_gateway.log
```

#### Cleanup Old Backlog Files

```bash
# Remove backlog files older than 7 days
find backlogs/ -name "*.pkl" -mtime +7 -delete
```

### Performance Considerations

#### Memory Usage

- **Backlog Storage**: Each point uses ~200-500 bytes in memory
- **10,000 points**: ~2-5 MB memory usage
- **100,000 points**: ~20-50 MB memory usage

#### Disk Usage

- **Backlog Files**: Compressed pickle format
- **10,000 points**: ~1-2 MB disk space
- **100,000 points**: ~10-20 MB disk space

#### Network Impact

- **Recovery Upload**: Large batches may take time to upload
- **Bandwidth**: Consider network capacity during recovery
- **Server Load**: InfluxDB may experience high load during recovery
For backlog and reconnection troubleshooting (backlog not flushing, excessive memory usage, disk space issues, overly aggressive reconnection), see [Troubleshooting](#troubleshooting) below.

### Best Practices

#### 1. Size Your Backlog Appropriately

```ini
# For 1-minute intervals, 24-hour outage
max_backlog_size = 1440  # 24 * 60

# For 5-minute intervals, 1-week outage  
max_backlog_size = 2016  # 7 * 24 * 12
```

#### 2. Monitor and Clean

- Regularly check backlog file sizes
- Clean up old files automatically
- Monitor disk space usage

#### 3. Test Recovery

- Simulate outages to test recovery
- Verify data integrity after recovery
- Monitor performance during recovery

#### 4. Plan for Scale

- Estimate data volume and outage duration
- Size backlog accordingly
- Monitor system resources

### Migration from Previous Version

If upgrading from a version without these features:

1. **No Configuration Changes Required**: Features are enabled by default with sensible defaults
2. **Backward Compatible**: Existing configurations continue to work
3. **Gradual Adoption**: Disable features if not needed:

```ini
[influxdb_output]
# Disable exponential backoff
use_exponential_backoff = false

# Disable persistent storage
enable_persistent_storage = false
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

---

## Troubleshooting

### Common Issue: Data Stops Being Written to InfluxDB

This guide helps you diagnose and fix the issue where data stops being written to InfluxDB after some time.

### Quick Diagnosis

#### 1. Check Logs

First, enable debug logging to see what's happening:

```ini
[influxdb_output]
transport = influxdb_out
host = localhost
port = 8086
database = solar
log_level = DEBUG
```

Look for these log messages:

- `"Not connected to InfluxDB, skipping data write"`
- `"Connection check failed"`
- `"Attempting to reconnect to InfluxDB"`
- `"Failed to write batch to InfluxDB"`

#### 2. Check InfluxDB Server

Verify InfluxDB is running and accessible:

```bash
# Check if InfluxDB is running
systemctl status influxdb

# Test connection
curl -i http://localhost:8086/ping

# Check if database exists
echo "SHOW DATABASES" | influx
```

#### 3. Check Network Connectivity

Test network connectivity between your gateway and InfluxDB:

```bash
# Test basic connectivity
ping your_influxdb_host

# Test port connectivity
telnet your_influxdb_host 8086
```

### Root Causes and Solutions

#### 1. Network Connectivity Issues

**Symptoms:**

- Connection timeouts
- Intermittent data loss
- Reconnection attempts in logs

**Solutions:**

```ini
[influxdb_output]
# Increase timeouts for slow networks
connection_timeout = 30
reconnect_attempts = 10
reconnect_delay = 10.0
```

#### 2. InfluxDB Server Restarts

**Symptoms:**

- Connection refused errors
- Sudden data gaps
- Reconnection success after delays

**Solutions:**

- Monitor InfluxDB server stability
- Check InfluxDB logs for crashes
- Consider using InfluxDB clustering for high availability

#### 3. Memory/Resource Issues

**Symptoms:**

- Slow response times
- Connection hangs
- Batch write failures

**Solutions:**

```ini
[influxdb_output]
# Reduce batch size to lower memory usage
batch_size = 50
batch_timeout = 5.0
```

#### 4. Authentication Issues

**Symptoms:**

- Authentication errors in logs
- Connection succeeds but writes fail

**Solutions:**

- Verify username/password in configuration
- Check InfluxDB user permissions
- Test authentication manually:

```bash
curl -i -u username:password http://localhost:8086/query?q=SHOW%20DATABASES
```

#### 5. Database/Measurement Issues

**Symptoms:**

- Data appears in InfluxDB but not in expected measurement
- Type conflicts in logs

**Solutions:**

- Verify database and measurement names
- Check for field type conflicts
- Use `force_float = true` to avoid type issues

### Configuration Best Practices

#### Recommended Configuration

```ini
[influxdb_output]
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

# Batching (adjust based on your data rate)
batch_size = 100
batch_timeout = 10.0

# Data handling
force_float = true
log_level = INFO
```

#### For Unstable Networks

```ini
[influxdb_output]
# More aggressive reconnection
reconnect_attempts = 10
reconnect_delay = 10.0
connection_timeout = 30

# Smaller batches for faster recovery
batch_size = 50
batch_timeout = 5.0
```

#### For High-Volume Data

```ini
[influxdb_output]
# Larger batches for efficiency
batch_size = 500
batch_timeout = 30.0

# Faster reconnection
reconnect_attempts = 3
reconnect_delay = 2.0
```

### Monitoring and Alerts

#### 1. Monitor Connection Status

Add this to your monitoring system:

```bash
# Check if gateway is writing data
curl -s "http://localhost:8086/query?db=solar&q=SELECT%20count(*)%20FROM%20device_data%20WHERE%20time%20%3E%20now()%20-%201h"
```

#### 2. Set Up Alerts

Monitor these conditions:

- No data points in the last hour
- Reconnection attempts > 5 in 10 minutes
- Connection failures > 3 in 5 minutes

#### 3. Log Monitoring

Watch for these log patterns:

```bash
# Monitor for connection issues
grep -i "connection\|reconnect\|failed" /var/log/protocol_gateway.log

# Monitor for data flow
grep -i "wrote.*points\|batch.*flush" /var/log/protocol_gateway.log
```

### Testing Your Setup

#### 1. Test Connection Monitoring

Run the connection test script:

```bash
python test_influxdb_connection.py
```

#### 2. Test Data Flow

Create a simple test configuration:

```ini
[test_source]
transport = modbus_rtu
port = /dev/ttyUSB0
baudrate = 9600
protocol_version = test_protocol
read_interval = 5
bridge = influxdb_output

[influxdb_output]
transport = influxdb_out
host = localhost
port = 8086
database = test
measurement = test_data
log_level = DEBUG
```

#### 3. Verify Data in InfluxDB

```sql
-- Check if data is being written
SELECT * FROM test_data ORDER BY time DESC LIMIT 10

-- Check data rate
SELECT count(*) FROM test_data WHERE time > now() - 1h
```

### Advanced Troubleshooting

#### 1. Enable Verbose Logging

```ini
[general]
log_level = DEBUG

[influxdb_output]
log_level = DEBUG
```

#### 2. Check Multiprocessing Issues

If using multiple transports, verify bridge configuration:

```ini
# Ensure bridge names match exactly
[source_transport]
bridge = influxdb_output

[influxdb_output]
transport = influxdb_out
# No bridge needed for output transports
```

#### 3. Monitor System Resources

```bash
# Check memory usage
free -h

# Check disk space
df -h

# Check network connections
netstat -an | grep 8086
```

#### 4. InfluxDB Performance Tuning

```ini
# InfluxDB configuration (influxdb.conf)
[data]
wal-fsync-delay = "1s"
cache-max-memory-size = "1g"
series-id-set-cache-size = 100
```

### Output Transport Tuning

- Adjust `batch_size` and `batch_timeout` for your use case
- Larger batches reduce network overhead but increase memory usage
- Shorter timeouts provide more real-time data but increase network traffic
- Increase `connection_timeout` for slow networks

### Common Error Messages

#### "Failed to connect to InfluxDB"

- Check if InfluxDB is running
- Verify host and port
- Check firewall settings

#### "Failed to write batch to InfluxDB"

- Check InfluxDB server resources
- Verify database permissions
- Check for field type conflicts

#### "Not connected to InfluxDB, skipping data write"

- Connection was lost, reconnection in progress
- Check network connectivity
- Monitor reconnection attempts

#### "Connection check failed"

- Network issue or InfluxDB restart
- Check InfluxDB server status
- Verify network connectivity

### Getting Help

If you're still experiencing issues:

1. **Collect Information:**
   - Gateway logs with DEBUG level
   - InfluxDB server logs
   - Network connectivity test results
   - Configuration file (remove sensitive data)

2. **Test Steps:**
   - Run the connection test script
   - Verify InfluxDB is accessible manually
   - Test with a simple configuration

3. **Provide Details:**
   - Operating system and version
   - Python version
   - InfluxDB version
   - Network setup (local/remote InfluxDB)
   - Data volume and frequency

### Prevention

#### 1. Regular Monitoring

- Set up automated monitoring for data flow
- Monitor InfluxDB server health
- Check network connectivity regularly

#### 2. Configuration Validation

- Test configurations before deployment
- Use connection monitoring settings
- Validate InfluxDB permissions

#### 3. Backup Strategies

- Consider multiple InfluxDB instances
- Implement data backup procedures
- Use InfluxDB clustering for high availability

### Backlog & Reconnection Issues

#### Backlog Not Flushing

**Symptoms:**

- Backlog points remain after reconnection
- No "Flushing X backlog points" messages

**Solutions:**

- Check InfluxDB server capacity
- Verify database permissions
- Monitor InfluxDB logs for errors

#### Excessive Memory Usage

**Symptoms:**

- High memory consumption
- Slow performance

**Solutions:**

- Reduce `max_backlog_size`
- Decrease `max_backlog_age`
- Monitor system resources

#### Disk Space Issues

**Symptoms:**

- "Backlog full" warnings
- Disk space running low

**Solutions:**

- Clean up old backlog files
- Reduce `max_backlog_size`
- Move `persistent_storage_path` to larger disk

#### Reconnection Too Aggressive

**Symptoms:**

- High CPU usage during outages
- Network congestion

**Solutions:**

- Increase `reconnect_delay`
- Reduce `reconnect_attempts`
- Enable `use_exponential_backoff`

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

#### Import reports type mismatches

- The value being written conflicts with the field's already-recorded type on the destination (checked against `SHOW FIELD KEYS` for v1, `information_schema.columns` for v3). Turn on **Allow float coercion** to have compatible values nudged into the expected type automatically, or fix the source data.
