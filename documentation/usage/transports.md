# Transports

A **transport** is MPG's fundamental unit of configuration. Every device you read from and every destination you write to is a transport. They are defined as named sections in `config.cfg` and managed through the web UI at `http://localhost:1717`.

## Table of Contents

- [Section Naming](#section-naming)
- [Scrapers and Bridges](#scrapers-and-bridges)
- [Universal Base Settings](#universal-base-settings)
- [Write Safety System](#write-safety-system)
- [Custom Transports](#custom-transports)
- [Scraper Transports](#scraper-transports)
  - [Modbus RTU](#modbus-rtu)
  - [Modbus TCP](#modbus-tcp)
  - [Modbus TLS](#modbus-tls)
  - [Modbus UDP](#modbus-udp)
  - [CAN Bus](#can-bus)
- [Bridge Transports](#bridge-transports)
  - [MQTT](#mqtt)
  - [TimescaleDB](#timescaledb)
  - [InfluxDB](#influxdb)
  - [JSON Output](#json-output)
- [Complete Configuration Examples](#complete-configuration-examples)

---

## Section Naming

Each transport is declared as an INI section with a name that begins with `transport.`:

```ini
[transport.0]
# minimal name — fine for simple setups

[transport.growatt]
# descriptive name — recommended for multi-device setups
```

The section name serves as the transport's identity throughout the system. It is the value you use in `bridge =` to link a scraper to a bridge, and it appears as the device label in the web UI. Choose names that are meaningful — changing a name after data has been written to a database will create a new device record.

---

## Scrapers and Bridges

MPG separates transports into two roles:

**Scrapers** read data from hardware. They actively poll a device on a schedule, parse the register values using a protocol map, and push the data downstream. Examples: `modbus_rtu`, `modbus_tcp`, `canbus`.

**Bridges** receive data from scrapers and write it somewhere. They are passive — they do not poll. Examples: `mqtt`, `timescaledb`, `influxdb_out`, `json_out`.

A scraper is linked to one or more bridges via the `bridge` setting. The bridge name is the section name of the destination transport. Multiple bridges can be comma-separated, or set to `broadcast` to forward data to all configured transports:

```ini
# Single bridge
bridge = transport.mqtt

# Multiple specific bridges
bridge = transport.mqtt, transport.timescaledb

# Broadcast to every transport
bridge = broadcast
```

---

## Universal Base Settings

These settings apply to every transport, scraper and bridge alike.

| Setting | Default | Description |
| --- | --- | --- |
| `transport` | *(inferred from section)* | Transport class to load. Required when the type cannot be inferred from the section name. E.g. `transport = mqtt` |
| `device_name` | `{manufacturer}_{serial_number}` | Unique friendly name for the device. Used as an identifier in MQTT topics, DB rows, and the web UI. |
| `device_manufacturer` | `MPG` | Device manufacturer. Passed through to MQTT discovery and DB metadata. |
| `device_model` | `MPG` | Device model number. |
| `device_serial_number` | *(empty)* | Serial number. If left blank, MPG will attempt to auto-fetch it from the device if the protocol supports it. |
| `device_location` | *(empty)* | Freeform location label. Passed through to all outputs as metadata. |
| `protocol_version` | *(required for Modbus/CAN)* | Protocol map to load from the `protocols/` directory. E.g. `protocol_version = growatt_v0.14`. Not required for bridge transports. |
| `bridge` | *(empty)* | Section name(s) of bridge transport(s) to forward data to. Comma-separated for multiple. |
| `read_interval` | `0` | Seconds between active polling cycles. Set to `0` to disable scheduled reads (event-driven only). Used by TimescaleDB as the flush interval. |
| `write_enabled` | `false` | Allow this transport to write data to the device. See [Write Safety System](#write-safety-system). |
| `max_precision` | `2` | Maximum decimal places for float values in all outputs. |
| `log_level` | `INFO` | Per-transport log verbosity. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

---

## Write Safety System

Writing to Modbus holding registers on a live inverter or BMS carries real risk. A wrong value can change a charge setpoint, disable a protection threshold, or cause equipment damage. MPG implements a layered write safety system.

### How Write Validation Works

Before enabling writes, MPG scores the loaded protocol map against the device's live register values. It reads the holding registers and checks how many documented values fall within the ranges specified in the protocol CSV. A score below 90% means the protocol map cannot be trusted for this device and writes are refused.

This validation runs automatically when `write_enabled = true` and a Modbus transport connects.

### Write Modes

The `write_enabled` setting accepts several values with escalating levels of trust:

| Value | Mode | Behavior |
| --- | --- | --- |
| `false` | **Read-only** | No writes. Default for all Modbus transports. |
| `true` | **Standard** | Writes enabled. Full protocol validation required (≥90% score). |
| `relaxed` | **Relaxed** ⚠️ | Writes enabled. Skips the bulk protocol validation score. Use only when you are certain you have the right protocol and the map is incomplete. |
| `unsafe` | **Unsafe** ☠️ | Writes enabled. All safety checks skipped. No validation whatsoever. |

### How Writes Are Triggered

MPG uses a deliberate inversion to make writeback safe and auditable. The MQTT bridge **subscribes** to write topics — it does not publish them. When a value arrives on a write topic, the MQTT bridge calls `write_data()` on its linked Modbus scraper, which executes the actual register write.

Write topics follow this pattern:

``` ini
{base_topic}/write/{variable_name}
```

During initialization, the MQTT bridge inspects the linked scraper's protocol map and subscribes only to variables that are marked as writable (`write_mode = RW` in the CSV). Variables marked read-only are never subscribed.

---

## Custom Transports

Any transport class can be extended without risk of being overwritten on update by following the `.custom` naming convention.

To create a custom MQTT transport:

```bash
cp classes/transports/mqtt.py classes/transports/mqtt.custom.py
```

Then in your config:

```ini
[transport.my_mqtt]
transport = mqtt.custom
```

Files ending in `.custom.py` are ignored by git and will never be touched by `git pull` or package upgrades.

---

## Scraper Transports

### Modbus RTU

Reads a device connected via RS-232 or RS-485 serial port (USB adapter, built-in serial, or direct wiring).

```ini
[transport.inverter]
transport = modbus_rtu
protocol_version = growatt_v0.14
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = Growatt
device_model = SPF 12000T
```

| Setting | Default | Description |
| --- | --- | --- |
| `port` | `/dev/ttyUSB0` | Serial port path. On Linux: `/dev/ttyUSB0`, `/dev/ttyACM0`. On Windows: `COM3`, `COM11`, etc. |
| `baudrate` | `9600` | Baud rate. Must match the device. Common values: `9600`, `19200`, `115200`. |
| `address` | `0` | Modbus device address (unit ID). |
| `max_retries_per_block` | `3` | Retry attempts per register block before moving on. |
| `batch_delay` | `0.85` | Seconds between register block reads. Increase on slow or noisy lines. |
| `enable_register_failure_tracking` | `true` | Track registers that consistently fail and temporarily disable them. |
| `max_failures_before_disable` | `5` | Failure count before a register block is soft-disabled. |
| `disable_duration_hours` | `12` | Hours to skip a failed register block before retrying. |

#### Hardware ID Port Addressing

Serial port numbers (`/dev/ttyUSB0`, `COM3`) can change across reboots, especially when multiple USB devices are connected. MPG supports stable hardware-ID addressing that survives reboots:

```ini
port = [0x1a86:0x7523::1-4]
```

The format is `[vendor_id:product_id:serial_number:location]`. Omit fields you don't need. MPG prints the hardware IDs of all detected serial ports at startup — look for lines like:

``` ini
Serial Port : /dev/ttyUSB0 = [0x1a86:0x7523::1-4]
```

---

### Modbus TCP

Reads a device over an IP network. Supports devices with a built-in Ethernet port and RS-485-to-TCP converters.

```ini
[transport.inverter]
transport = modbus_tcp
protocol_version = eg4_18kpv
host = 192.168.1.100
port = 502
read_interval = 15
bridge = transport.timescaledb
device_manufacturer = EG4
device_model = 18KPV
device_serial_number = 4066670074
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | *(required)* | IP address or hostname of the Modbus TCP device. |
| `port` | `502` | TCP port. Standard Modbus port is 502. |
| `timeout` | `7` | Connection timeout in seconds. |
| `retries` | `3` | Reconnect attempts on connection failure. |

MPG caches and reuses the TCP client across multiple transports pointed at the same `host:port`. This means two transport sections connecting to the same device share a single underlying connection automatically.

---

### Modbus TLS

Reads a Modbus device over an encrypted TLS connection. Used for devices exposed across untrusted networks.

```ini
[transport.secure_inverter]
transport = modbus_tls
protocol_version = eg4_18kpv
host = 192.168.1.100
port = 502
certfile = client.crt
keyfile = client.key
hostname = inverter.local
read_interval = 30
bridge = transport.mqtt
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | *(required)* | IP address or hostname of the device. |
| `port` | `502` | TLS-wrapped Modbus port. |
| `certfile` | *(empty)* | Client certificate filename, relative to the `config/` directory. |
| `keyfile` | *(empty)* | Client private key filename, relative to the `config/` directory. |
| `hostname` | *(same as `host`)* | TLS SNI hostname, if different from `host`. |
| `timeout` | `7` | Connection timeout in seconds. |
| `retries` | `3` | Reconnect attempts on failure. |

---

### Modbus UDP

Reads a Modbus device over UDP. Uncommon but supported for devices or gateways that use UDP transport.

```ini
[transport.udp_device]
transport = modbus_udp
protocol_version = srne_v3.9
host = 192.168.1.50
port = 502
read_interval = 20
bridge = transport.mqtt
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | *(required)* | IP address of the UDP endpoint. |
| `port` | `502` | UDP port. |
| `timeout` | `7` | Read timeout in seconds. |
| `retries` | `3` | Retry attempts on no response. |

---

### CAN Bus

Reads a device connected via CAN bus. CAN bus is a passive broadcast protocol — MPG listens to the bus and caches all messages, then processes them against the protocol map at the configured read interval.

```ini
[transport.bms]
transport = canbus
protocol_version = pylon_can
port = can0
interface = socketcan
baudrate = 500000
read_interval = 5
bridge = transport.mqtt
```

| Setting | Default | Description |
| --- | --- | --- |
| `port` | *(required)* | CAN interface name. For socketcan: `can0`, `can1`. For slcan: serial port path. Also accepts `channel`. |
| `interface` | `socketcan` | CAN adapter mode. `socketcan` for candlelight/gs_usb adapters; `slcan` for serial CAN adapters. |
| `baudrate` | `500000` | CAN bus bitrate. Common values: `250000`, `500000`. Overridden by protocol defaults if set in the protocol JSON. |
| `cache_timeout` | `120` | Seconds to retain a CAN message in cache before expiring it. |

#### CAN Bus Adapter Setup

The primary adapter used to develop and test MPG is the [FYSETC UCAN](https://www.fysetc.com/products/fysetc-ucan-board), a CANable v1.0-compatible USB adapter. These are widely available. Most ship with candlelight firmware (socketcan) by default. slcan firmware is also available at [canable.io](https://canable.io/updater/canable1.html).

**Candlelight / socketcan** adapters appear as a `gs_usb` device. The CAN interface appears as a network interface — check with `ip link show` and bring it up with:

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
```

**slcan** adapters appear as a serial device. Set `interface = slcan` and `port` to the serial path.

CAN bus support is primarily developed and tested on Linux. USB CAN adapters on Windows require additional driver work; Linux is strongly recommended.

---

## Bridge Transports

### MQTT

Publishes register values to an MQTT broker. Supports Home Assistant auto-discovery, per-register topics, JSON payloads, and bidirectional write-back to Modbus.

```ini
[transport.mqtt_out]
transport = mqtt
host = 192.168.1.10
port = 1883
username = mpg
password = your_password
base_topic = home/inverter
discovery_enabled = true
discovery_topic = homeassistant
json = false
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | *(required)* | MQTT broker IP address or hostname. |
| `port` | `1883` | MQTT broker port. Use `8883` for TLS. |
| `username` | *(empty)* | MQTT username. |
| `password` | *(empty)* | MQTT password. |
| `base_topic` | `home/device` | Root topic prefix. Device identifier is appended automatically. |
| `error_topic` | `/error` | Topic suffix for error messages. |
| `discovery_enabled` | `false` | Enable Home Assistant MQTT discovery. Publishes device config payloads so entities appear automatically in HA. |
| `discovery_topic` | `homeassistant` | Home Assistant discovery prefix. Must match the value configured in your HA MQTT integration. |
| `json` | `false` | When `true`, publish all register values as a single JSON payload per cycle instead of individual topics. |
| `holding_register_prefix` | *(empty)* | Optional prefix appended to holding register topic names. |
| `input_register_prefix` | *(empty)* | Optional prefix appended to input register topic names. |
| `reconnect_delay` | `7` | Seconds between reconnect attempts on broker disconnect. Minimum: 1. |
| `reconnect_attempts` | `21` | Maximum number of reconnect attempts before giving up. Set to `0` for unlimited. |

#### MQTT Topic Structure

Each register value is published as:

``` ini
{base_topic}/{device_identifier}/{variable_name}
```

Availability is published at:

``` ini
{base_topic}/{device_identifier}/availability    → "online" / "offline"
```

Write-enabled registers are subscribed at:

``` ini
{base_topic}/write/{variable_name}
```

Values published to a write topic are forwarded to the linked Modbus scraper and written to the device's holding register.

---

### TimescaleDB

Writes register values to a [TimescaleDB](https://www.timescale.com/) hypertable (PostgreSQL extension). This is the highest-fidelity bridge — it creates a wide-format hypertable per device with one column per register, plus automatic continuous aggregates (hourly, daily, weekly, monthly rollups) for fast historical queries.

```ini
[transport.timescaledb]
transport = timescaledb
host = localhost
port = 5432
database = solar
username = mpg
password = your_password
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | `localhost` | TimescaleDB server hostname or IP. |
| `port` | `5432` | PostgreSQL port. |
| `database` | `solar` | Database name. Created automatically if it does not exist. |
| `username` | *(required)* | PostgreSQL user. |
| `password` | *(required)* | PostgreSQL password. |
| `pool_size` | `5` | SQLAlchemy connection pool size. |
| `max_overflow` | `10` | Maximum connections above `pool_size` allowed under load. |
| `pool_recycle` | `3600` | Seconds before idle connections are recycled. |
| `force_float` | `true` | Cast all numeric values to float. Prevents type-mismatch errors when a register occasionally returns an integer where a float is expected. |
| `read_interval` | *(from scraper)* | Used as the flush interval (seconds between DB writes). Inherited from the linked scraper's `read_interval` if not set. |
| `write_requires_complete_cycle` | `false` | When `true`, only flush to the DB after a scraper cycle completes all register blocks without error. Prevents partial rows. |
| `stale_data_timeout` | `300` | Minutes before a stale (non-updating) register value is excluded from writes. |

#### Schema Management

TimescaleDB manages its own schema. On first connection MPG will:

1. Create the target database if it does not exist
2. Create a `device_info` table tracking all connected scrapers
3. Create a `device_metrics_wide` hypertable with one column per register in the protocol map
4. Create a `device_metrics_narrow` hypertable for arbitrary key-value queries
5. Configure hypertable compression
6. Create continuous aggregate views: `hourly_rollup`, `daily_rollup`, `weekly_rollup`, `monthly_rollup`

New registers added to the protocol map are automatically added as new columns on the next connection.

#### Hypertable & Rollup Options

| Setting | Default | Description |
| --- | --- | --- |
| `enable_compression` | `true` | Enable TimescaleDB native compression on the hypertable. |
| `enable_rollups` | `true` | Create continuous aggregate rollup views. |
| `enable_auto_refresh` | `true` | Automatically refresh rollup aggregates on a schedule. |
| `auto_refresh_interval` | `21600` | Seconds between rollup refreshes (default: 6 hours). |
| `migrate_data` | `true` | Rebuild rollup views when rollup settings change. |
| `drop_after` | `1 year` | Hypertable data retention period. Older chunks are dropped automatically. |

#### Persistent Backlog

If the database is unavailable, MPG buffers data points to disk and replays them when connectivity is restored.

| Setting | Default | Description |
| --- | --- | --- |
| `enable_persistent_storage` | `true` | Enable disk backlog on DB disconnect. |
| `backlog_storage_path` | `timescaledb_backlog` | Directory for backlog files, relative to the working directory. |
| `backlog_file_name` | `no_connect_backlog` | Backlog filename prefix. |
| `max_backlog_size` | `10000` | Maximum data points to store in the backlog before dropping oldest. |
| `max_backlog_age` | `86400` | Maximum backlog age in seconds (default: 24 hours). Older points are discarded on reconnect. |

#### Connection Resilience

| Setting | Default | Description |
| --- | --- | --- |
| `reconnect_attempts` | `5` | Maximum reconnect attempts before backing off. |
| `reconnect_delay` | `5` | Minutes between reconnect attempts. |
| `use_exponential_backoff` | `true` | Increase reconnect delay exponentially on repeated failure. |
| `max_reconnect_delay` | `300` | Maximum reconnect delay in minutes (default: 5 hours). |

#### Pushover Notifications

TimescaleDB can send a Pushover push notification when the bridge loses or restores database connectivity.

| Setting | Default | Description |
| --- | --- | --- |
| `enable_pushover` | `false` | Enable Pushover notifications for connection events. |
| `pushover_token` | *(empty)* | Pushover application API token. |
| `pushover_user` | *(empty)* | Pushover user key. |

---

### InfluxDB

Writes register values to an InfluxDB v1.x database. Values are stored as fields on a single measurement, with device metadata stored as tags for fast filtering.

```ini
[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
username = mpg
password = your_password
measurement = device_data
```

| Setting | Default | Description |
| --- | --- | --- |
| `host` | `localhost` | InfluxDB server hostname or IP. |
| `port` | `8086` | InfluxDB HTTP API port. |
| `database` | `solar` | Database name. Created automatically if it does not exist. |
| `username` | *(empty)* | InfluxDB username. |
| `password` | *(empty)* | InfluxDB password. |
| `measurement` | `device_data` | InfluxDB measurement name for all data points. |
| `include_timestamp` | `true` | Include a nanosecond-precision timestamp on each data point. |
| `include_device_info` | `true` | Store device metadata as InfluxDB tags (`device_name`, `device_manufacturer`, `device_model`, `device_serial_number`, `transport`). |
| `force_float` | `true` | Cast all numeric values to float to prevent InfluxDB field type conflicts across writes. |
| `batch_size` | `100` | Number of data points to accumulate before flushing to InfluxDB. |
| `batch_timeout` | `10.0` | Maximum seconds to hold a batch before flushing even if `batch_size` is not reached. |

#### InfluxDB Data Structure

Each write produces one data point per register value, with this structure:

``` ini
measurement: device_data
tags:
  device_identifier = "4066670074"
  device_name       = "my_inverter"
  device_manufacturer = "EG4"
  device_model      = "18KPV"
  transport         = "transport.inverter"
fields:
  battery_voltage   = 52.3
  grid_power        = 1450.0
  soc               = 87.0
  ...
timestamp: 1703123456789000000
```

#### Persistent Backlog

| Setting | Default | Description |
| --- | --- | --- |
| `enable_persistent_storage` | `true` | Buffer data points to disk when InfluxDB is unreachable. |
| `persistent_storage_path` | `influxdb_backlog` | Directory for backlog files. |
| `max_backlog_size` | `10000` | Maximum buffered data points. |
| `max_backlog_age` | `86400` | Maximum backlog age in seconds before points are discarded (default: 24 hours). |

#### Connection Resilience

| Setting | Default | Description |
| --- | --- | --- |
| `reconnect_attempts` | `5` | Reconnect attempts before backing off. |
| `reconnect_delay` | `5.0` | Seconds between reconnect attempts. |
| `connection_timeout` | `10` | Connection timeout in seconds. |
| `use_exponential_backoff` | `true` | Increase delay between reconnect attempts exponentially. |
| `max_reconnect_delay` | `300.0` | Maximum reconnect delay in seconds (default: 5 minutes). |
| `periodic_reconnect_interval` | `14400.0` | Force a reconnect every N seconds even if the connection appears healthy (default: 4 hours). Helps recover from silent connection drops. |

---

### JSON Output

Writes register values as JSON to stdout or a file. Primarily useful for debugging, piping to external tools, and building integrations with systems that consume JSON streams.

```ini
[transport.json_out]
transport = json_out
output_file = stdout
pretty_print = true
include_timestamp = true
include_device_info = true
```

| Setting | Default | Description |
| --- | --- | --- |
| `output_file` | `stdout` | Output destination. Use `stdout` for console, or a full file path (e.g. `/var/log/inverter.json`). |
| `pretty_print` | `true` | Indent JSON output for readability. Set to `false` for compact single-line output. |
| `append_mode` | `false` | When writing to a file, append each cycle's output rather than overwriting the file. Useful for log files. |
| `include_timestamp` | `true` | Include a Unix timestamp in the output. |
| `include_device_info` | `true` | Include device metadata block in the output. |

#### Output Format

```json
{
  "device": {
    "identifier": "4066670074",
    "name": "my_inverter",
    "manufacturer": "EG4",
    "model": "18KPV",
    "serial_number": "4066670074",
    "transport": "transport.inverter"
  },
  "timestamp": 1703123456.789,
  "data": {
    "battery_voltage": 52.3,
    "grid_power": 1450.0,
    "soc": 87.0
  }
}
```

---

## Complete Configuration Examples

### Modbus RTU → MQTT (Home Assistant)

```ini
[general]
log_level = INFO

[transport.inverter]
# Scraper: reads Growatt inverter over RS-485
transport = modbus_rtu
protocol_version = growatt_v0.14
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 15
write_enabled = false
bridge = transport.mqtt
device_manufacturer = Growatt
device_model = SPF 12000T
device_location = Home

[transport.mqtt]
# Bridge: publishes to MQTT broker with Home Assistant discovery
transport = mqtt
host = 192.168.1.10
port = 1883
username = mpg
password = your_password
base_topic = home/inverter
discovery_enabled = true
discovery_topic = homeassistant
```

---

### Modbus TCP → TimescaleDB

```ini
[general]
log_level = INFO

[logging]
log_dir = logs
log_file = gateway.log
rotation = weekly
backup_count = 4
console = true

[transport.Inverter1]
transport = modbus_tcp
protocol_version = eg4_18kpv
host = 192.168.1.100
port = 502
read_interval = 15
write_enabled = false
bridge = transport.timescaledb
device_manufacturer = EG4
device_model = 18KPV
device_serial_number = 4066670074
device_location = home

[transport.timescaledb]
transport = timescaledb
host = localhost
port = 5432
database = solar
username = mpg
password = your_password
force_float = true
enable_persistent_storage = true
enable_rollups = true
drop_after = 1 year
```

---

### Modbus RTU → MQTT + InfluxDB (dual bridge)

```ini
[general]
log_level = INFO

[transport.inverter]
transport = modbus_rtu
protocol_version = sigineer_v0.11
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 10
bridge = transport.mqtt, transport.influxdb
device_manufacturer = Sigineer
device_model = MAX Series

[transport.mqtt]
transport = mqtt
host = 192.168.1.10
port = 1883
username = mpg
password = your_password
base_topic = home/inverter
discovery_enabled = true

[transport.influxdb]
transport = influxdb_out
host = localhost
port = 8086
database = solar
username = mpg
password = your_password
measurement = inverter_metrics
batch_size = 50
```

---

### CAN Bus BMS → MQTT

```ini
[general]
log_level = INFO

[transport.bms]
transport = canbus
protocol_version = pylon_can
port = can0
interface = socketcan
baudrate = 500000
read_interval = 5
bridge = transport.mqtt
device_manufacturer = Pylon
device_model = US2000

[transport.mqtt]
transport = mqtt
host = 192.168.1.10
port = 1883
username = mpg
password = your_password
base_topic = home/bms
discovery_enabled = true
```

---

### Modbus RTU → JSON stdout (debugging)

```ini
[general]
log_level = DEBUG

[transport.inverter]
transport = modbus_rtu
protocol_version = srne_v3.9
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 5
bridge = transport.debug

[transport.debug]
transport = json_out
output_file = stdout
pretty_print = true
include_timestamp = true
include_device_info = true
```
