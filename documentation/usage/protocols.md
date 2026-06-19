# Protocols

A **protocol** is the map between raw register addresses on a hardware device and the named, typed, human-readable variables MPG publishes to its outputs. Every scraper transport that reads data — Modbus RTU, Modbus TCP, CAN bus — requires a protocol to know what the register values mean.

## Table of Contents

- [Protocol File Structure](#protocol-file-structure)
- [The JSON Descriptor](#the-json-descriptor)
- [The Registry Map CSV](#the-registry-map-csv)
  - [Column Reference](#column-reference)
  - [Data Types](#data-types)
  - [Byte Order](#byte-order)
  - [Register Addressing](#register-addressing)
  - [Write Mode (`writable`)](#write-mode-writable)
  - [Value Ranges and Codes](#value-ranges-and-codes)
  - [Per-Register Read Intervals](#per-register-read-intervals)
- [Protocol Overrides](#protocol-overrides)
- [Custom Protocols](#custom-protocols)
- [Managing Write-Back via the Web UI](#managing-write-back-via-the-web-ui)
  - [Prerequisites](#prerequisites)
  - [Enabling Write-Back on a Register](#enabling-write-back-on-a-register)
  - [What Happens on Commit](#what-happens-on-commit)
  - [How the MQTT Bridge Uses the Override](#how-the-mqtt-bridge-uses-the-override)
  - [The Full Write-Back Flow, End to End](#the-full-write-back-flow-end-to-end)
- [Variable Mask and Screen](#variable-mask-and-screen)
- [Protocol Library](#protocol-library)

---

## Protocol File Structure

Each protocol lives in its own subdirectory under `protocols/` and consists of up to three files:

``` ini
protocols/
└── growatt/
    ├── growatt_v0.14.json
    ├── growatt_v0.14.input_registry_map.csv
    └── growatt_v0.14.holding_registry_map.csv
```

| File | Purpose |
| --- | --- |
| `{name}.json` | Transport defaults, baud rate, byte order, and enum code definitions |
| `{name}.input_registry_map.csv` | Read-only (input) registers — polled from the device |
| `{name}.holding_registry_map.csv` | Read/write (holding) registers — polled and optionally written |
| `{name}.registry_map.csv` | Generic register map used by protocols without input/holding distinction (CAN bus, etc.) |

A protocol is activated in `config.cfg` by setting `protocol_version` to the base filename without extension:

```ini
[transport.inverter]
protocol_version = growatt_v0.14
```

The web UI manages this setting and provides a protocol browser to select from all available protocol files.

---

## The JSON Descriptor

The `.json` file sets protocol-wide defaults that apply to all registers in the map. Most fields are optional.

```json
{
  "transport": "modbus_rtu",
  "baud": "9600",
  "byteorder": "big",
  "read_interval": "10",
  "battery_status_codes": {
    "0": "Idle",
    "1": "Charging",
    "2": "Discharging"
  }
}
```

| Key | Description |
| --- | --- |
| `transport` | Default transport class to use for this protocol. Can be overridden in `config.cfg`. |
| `baud` | Default baud rate for RTU connections. |
| `byteorder` | Default byte order: `big` (most common) or `little`. Individual registers can override this. |
| `read_interval` | Default polling interval in seconds. Overridden by the transport's `read_interval` setting. |
| `{name}_codes` | Enum code mappings for flag registers. The key must match a `documented name` column value with `_codes` appended. |

---

## The Registry Map CSV

The CSV files are the core of a protocol — one row per register. They use `;` or `,` as a delimiter (the parser auto-detects which). Both formats are compatible with LibreOffice Calc and Excel. Column names are case-insensitive and underscores are treated as spaces.

### Column Reference

| Column | Required | Description |
| --- | --- | --- |
| `variable name` | No | Friendly name used in MQTT topics, database column names, and all outputs. If blank, `documented name` is used. |
| `documented name` | Yes | Original name from the device's official documentation. Acts as the primary key for override matching. |
| `register` | Yes | Register address (decimal or `x`-prefixed hex). Supports ranges and bit/byte offsets. |
| `data type` | Yes | How to interpret the raw register bytes. See [Data Types](#data-types). |
| `unit` | No | Unit symbol (e.g. `V`, `A`, `W`, `°C`, `%`). Supports multiplier suffixes — see below. |
| `writable` | No | Write permission for this register. See [Write Mode](#write-mode-writable). |
| `values` | No | Valid value range or enum codes. Used for protocol validation and write safety. |
| `read interval` | No | Per-register polling override. See [Per-Register Read Intervals](#per-register-read-intervals). |
| `description` | No | Human-readable description. Displayed in the web UI. |
| `note` | No | Additional notes shown in the protocol editor and analysis tool. |
| `read command` | No | For protocols requiring a command byte before reading. Hex prefix `x` or raw UTF-8. |

#### Unit Multipliers

A unit string can include a numeric multiplier to automatically scale the raw register value. For example, if a register stores voltage in units of 0.1V:

``` ini
0.1V
```

MPG parses the numeric prefix, applies it as a multiplier to the raw value, and publishes the result with unit `V`. This keeps the protocol map self-describing without requiring custom parsing code.

### Data Types

| Type | Size | Description |
| --- | --- | --- |
| `USHORT` | 16 bit | Unsigned integer (0 to 65535). Default for most Modbus registers. |
| `SHORT` | 16 bit | Signed integer (-32768 to 32767). |
| `UINT` | 32 bit | Unsigned 32-bit integer. Spans two consecutive 16-bit registers. |
| `INT` | 32 bit | Signed 32-bit integer. |
| `BYTE` | 8 bit | Single unsigned byte. |
| `16BIT_FLAGS` | 16 bit | Splits the register into 16 individual bit flags, labeled `b0`–`b15`. |
| `8BIT_FLAGS` | 8 bit | Splits into 8 bit flags. |
| `32BIT_FLAGS` | 32 bit | Splits into 32 bit flags. |
| `#bit` | # bits | Unsigned sub-register value of arbitrary bit width. E.g. `3bit` is a 3-bit value (0–7). |
| `#sbit` | # bits | Signed sub-register value. |
| `#smbit` | # bits | Sign-magnitude sub-register value. |
| `ASCII` | varies | String value. Two characters per register by default. |
| `ASCII.#` | # chars | Fixed-length string. E.g. `ASCII.7` reads 7 characters. |
| `HEX` | varies | Hexadecimal string representation. |

Common alternative names are also accepted: `INT16` → `SHORT`, `UINT16` → `USHORT`, `UINT32` → `UINT`, `INT32` → `INT`.

### Byte Order

Most Modbus devices transmit multi-byte values in big-endian order, which is the default. When a register uses non-standard byte order, place a json command in the adjustments column:

```ini
{"Register_Endian": "little_endian-CDAB"}
```

The JSON descriptor's `byteorder` field sets the default for all registers in the protocol. An individual register's suffix (_l,_h) overrides the protocol default.
To ensure the entry is read with a little endian byte order, add to the adjustment field {"Register_Endian": "little_endian-CDAB"}
Registers default to big endian.

Two independent axes that fully describe how a manufacturer encodes a
multi-register (32- or 64-bit) value over Modbus.

Modbus transmits each 16-bit register in big-endian byte order on the wire.
For values that span more than one register, manufacturers have adopted four
distinct encoding conventions, corresponding to all combinations of these
two boolean axes:

+---------------------------------+--------------+----------------+------------------+
| Canonical CSV name              | word_reversed| bytes_reversed | Byte sequence    |
+=================================+==============+================+==================+
| big_endian-ABCD                 | False        | False          | ABCD (default)   |
| big_endian_byte_swap-BADC       | False        | True           | BADC             |
| little_endian-CDAB              | True         | False          | CDAB             |
| little_endian_byte_swap-DCBA    | True         | True           | DCBA             |
+---------------------------------+--------------+----------------+------------------+

``word_reversed``
    True  → the low-significance word is stored at the *lower* register
            address (CDAB / DCBA).
    False → the high-significance word is stored at the *lower* register
            address (ABCD / BADC) — standard Modbus convention.

``bytes_reversed``
    True  → the two bytes *within* each 16-bit register are stored in
            little-endian order (BADC / DCBA).
    False → bytes within each register are in big-endian order (ABCD /
            CDAB) — standard Modbus wire format.

These two axes are completely independent; controlling them separately
makes it impossible to accidentally produce a fifth, invalid combination.

### Register Addressing

The `register` column is flexible to accommodate the many ways real device documentation numbers registers.

**Plain register number:**

``` ini
40001
```

**Hexadecimal (prefix `x`):**

``` ini
x9C41
```

**Register range** (used for multi-register ASCII strings):

``` ini
7~14
```

**Reverse range** (reads registers in descending order):

``` ini
r7~14
```

**Bit offset** (`.b#`):

``` ini
40001.b3
```

Reads bit 3 of register 40001. Combine with `#bit` data types to extract arbitrary sub-register values.

**Byte offset** (`.#`):

``` ini
40001.2
```

Reads byte offset 2 of register 40001. Used in protocols with packed byte fields.

### Write Mode (`writable`)

The `writable` column controls whether MPG is permitted to write to a register. It has no effect on reading — all non-disabled registers are always read during a normal poll cycle.

| Value | Aliases | Meaning |
| --- | --- | --- |
| `R` | `READ`, `NO` | Read-only. This register is never written. Default. |
| `RD` | `READDISABLED`, `DISABLED`, `D` | Read-disabled. This register is skipped during polling and never written. Use for registers that cause device errors when read. |
| `W` | `RW`, `R/W`, `YES` | Read and write. This register can be read and, when write-back is enabled, written by an upstream MQTT command. |
| `WO` | — | Write-only. This register is never read but can be written by an upstream MQTT command. |

**Important:** marking a register `W` or `RW` in the CSV alone is not enough to enable writes. It is a necessary condition but not sufficient. The user must also explicitly enable write-back for each register through the web UI (see [Managing Write-Back via the Web UI](#managing-write-back-via-the-web-ui)), and the transport's `write_enabled` must be set to `true`.

### Value Ranges and Codes

The `values` column has two distinct purposes.

**Range validation** — used during protocol analysis scoring and Modbus write safety checks. A register with a defined range allows MPG to verify that the live device value is plausible before enabling writes:

``` ini
0~100
```

``` ini
0~65535
```

**Enum code mappings** — used to translate integer register values into human-readable strings. JSON object format:

```json
{"0": "Idle", "1": "Charging", "2": "Discharging"}
```

For flag registers (`16BIT_FLAGS`, etc.), the keys are bit identifiers:

```json
{"b0": "StandBy", "b1": "On", "b2": "Fault"}
```

Multi-bit combinations are also supported:

```json
{"b0": "StandBy", "b0&b1": "Operational", "b1": "Error"}
```

Enum codes can also be defined in the JSON descriptor file as `{documented_name}_codes`. This keeps large code tables out of the CSV.

### Per-Register Read Intervals

By default every register is polled at the transport's configured `read_interval`. The `read interval` column overrides this on a per-register basis, useful for slow-changing values like firmware version that don't need frequent polling.

| Suffix | Meaning | Example |
| --- | --- | --- |
| `x` | Multiplier of the transport interval | `7x` with a 10s transport interval → 70s |
| `s` | Absolute seconds | `30s` → 30 seconds |
| `ms` | Absolute milliseconds | `5000ms` → 5 seconds |

The effective interval is always rounded up to the nearest transport poll cycle. A value of `1s` with a transport `read_interval = 7` will still only be read every 7 seconds.

---

## Protocol Overrides

Override files allow you to modify specific rows of a protocol CSV without touching the original file. This is the correct way to make targeted changes that should survive protocol updates.  For docker, either store your overrides in the config folder to survive image updates or map a volume to the folder holding the csv files that you're using.

An override file is named by appending `.override` before `.csv`:

``` ini
protocols/growatt/growatt_v0.14.input_registry_map.override.csv
```

Override files use the same column format as the main CSV. Only non-empty columns are applied — you don't need to specify every column, only the ones you want to change.

**Matching logic:**

1. MPG first attempts to match the override row by `documented name` (primary key).
2. If no match by documented name, it falls back to matching by `register` address (secondary key).
3. If both `documented name` and `register` are unique (no match in the main CSV), the override row is treated as a new register entry and appended.

**Example** — change the data type for a single register:

```csv
documented name;data type
product id;ASCII
```

**Example** — add a new register not in the original protocol:

```csv
documented name;register;data type;variable name;unit
custom_power_limit;40999;USHORT;power_limit;W
```

Override files stored in the `config/` directory take precedence over ones in `protocols/` and are preserved across Docker volume mounts and software updates.

---

## Custom Protocols

To create a fully custom protocol — or a fork of an existing one that won't be overwritten by updates — use the `.custom` naming convention.

```bash
# Fork an existing protocol
cp protocols/eg4/eg4_v58.json                      protocols/eg4/eg4_v58.custom.json
cp protocols/eg4/eg4_v58.input_registry_map.csv    protocols/eg4/eg4_v58.input_registry_map.custom.csv
cp protocols/eg4/eg4_v58.holding_registry_map.csv  protocols/eg4/eg4_v58.holding_registry_map.custom.csv
```

Reference the custom protocol in your transport config:

```ini
[transport.inverter]
protocol_version = eg4_v58.custom
```

Files ending in `.custom.json` and `.custom.csv` are excluded from git tracking and will never be overwritten by `git pull` or package upgrades. This also applies to any filename suffix, not just `.custom` — any protocol file name that does not match a shipped protocol name is safe.

---

## Managing Write-Back via the Web UI

The web UI provides a point-and-click workflow for selecting which holding registers are writeable for a specific device. This is separate from the `writable` column in the CSV — that column establishes what the protocol *permits*. The UI workflow establishes what *your specific device* actually supports.

### Prerequisites

Before enabling write-back:

1. The register must be marked `W`, `RW`, or `R/W` in the holding registry map CSV. Registers marked `R` or `RD` cannot be enabled for write in the UI — the checkbox is locked.
2. The transport's `write_enabled` setting must be set to `true` in the device settings pane. This acts as the master switch.
3. The transport must be a Modbus type. Write-back is not supported for CAN bus.

### Enabling Write-Back on a Register

1. Open the web UI at `http://localhost:1717` and navigate to the scraper device (the scraper transport, not the bridge transport).
2. In the right panel, select the **Holding** tab under the protocol register table.
3. Each register row has three columns: **W** (write), **M** (mask), **S** (screen).
4. The **W** column shows a checkbox for every register where the protocol CSV has a write mode of `RW`, `W`, or `WO`. Registers that are read-only per the protocol show a `-` instead of a checkbox — they cannot be enabled.
5. Check the **W** checkbox for each register you want to enable for write-back. The row highlights amber to indicate a staged (uncommitted) change.
6. The tab label updates in real time: `W:3 M:0 S:0` indicates 3 registers are staged for write-back.
7. Click **Commit** in the top bar to persist the changes.

> The W/M/S toggles are **device-scoped**, not protocol-scoped. Enabling write-back on a register for `transport.inverter1` does not affect `transport.inverter2` even if they use the same protocol.

### What Happens on Commit

When you click Commit, MPG executes a multi-step write:

**1. Backup** — The current `config.cfg` is archived to the backups table with a timestamp. This can be restored via the UI's Rollback feature.  Backups are stored in the backups folder below the config folder.

**2. `config.cfg` rewrite** — All active, non-dirty settings are written back to disk from the staging database. Your `write_enabled = true` setting is included here.

**3. Override CSV generation** — This is the critical step for write-back. MPG queries the `DeviceProtocolSelection` table for all registers where `user_write_enabled = True` for this device and protocol. It then writes (or updates) a holding registry override file:

``` ini
config/{protocol_name}.holding_registry_map.override.csv
```

The file contains only two columns: `documented name` and `write`. Every register you enabled gets an entry with `write = W`:

```csv
documented name,write
max_charge_current,W
charge_voltage_limit,W
discharge_cutoff_voltage,W
```

Registers where you unchecked the **W** box are removed from the file. If no registers are enabled, the override file is deleted.

**4. Mask and screen file rewrite** — `variable_mask_{transport_name}.txt` and `variable_screen_{transport_name}.txt` are written based on the M and S toggles.

**Why the override lives in `config/`, not `protocols/`:** Override files in `config/` survive software updates because Docker volume mounts and `git pull` do not touch the config directory. The base protocol CSVs in `protocols/` may be updated with new registers or corrections; the override file adds the write permission on top without modifying the original.

### How the MQTT Bridge Uses the Override

After a gateway restart (required to reload the new override file), the MQTT bridge's `init_bridge()` method runs the following logic on connection:

**Step 1 — Check the master write switch.** If the scraper transport's `write_enabled` is not `true`, `init_bridge()` does nothing — no write topics are subscribed. This is the outer gate.

**Step 2 — Load the allowlist.** The bridge calls `_load_override_write_allowlist()`, which reads `config/{protocol_name}.holding_registry_map.override.csv` and builds a set of `documented_name` values. This is the inner gate — the set of registers the user explicitly approved via the UI.

If the override file does not exist or is empty, the allowlist is empty and no write topics are subscribed. The bridge logs:

``` ini
No holding override allowlist found for 'transport.inverter'; MQTT write topics disabled until write selections are committed.
```

**Step 3 — Build write topic subscriptions.** The bridge walks every entry in the scraper's holding register map. For each entry it checks two conditions:

1. The register's `write_mode` (from the CSV) is `WRITE` or `WRITEONLY`.
2. The register's `documented_name` is present in the allowlist loaded from the override file.

Both conditions must be true. A register that is marked `RW` in the CSV but was not enabled in the UI is not subscribed. A register that was enabled in the UI but whose CSV entry is `R` cannot be subscribed (the UI would have prevented enabling it).

Registers that pass both checks are subscribed at:

``` ini
{base_topic}/{device_identifier}/write/{variable_name}
```

The resulting `__write_topics` dictionary maps each subscribed topic string back to its `registry_map_entry` object. This is how inbound MQTT messages are routed to the correct register.

**Step 4 — Handle inbound write messages.** When a message arrives on a subscribed write topic, `client_on_message()` looks it up in `__write_topics`, retrieves the `registry_map_entry`, and calls `_emit_message()`. This fires the `on_message` callback, which the gateway has wired to the scraper's write path. The scraper validates the value against the register's allowed range (if defined) and issues Modbus function code 06 to write the holding register.

### The Full Write-Back Flow, End to End

``` ini
User in Web UI
    │
    │  Checks the W box on "charge_voltage_limit" (holding register 0x24)
    │
    ▼
Protocol Service (toggle_register_field)
    │  Enforces two-gate rule:
    │    Gate 1 — is register writable per protocol CSV? (is_writable_by_protocol)
    │    Gate 2 — update DeviceProtocolSelection.user_write_enabled = True
    │
    ▼
Commit (config_writer.commit_all)
    │
    ├── Writes config.cfg  (includes write_enabled = true)
    │
    ├── Writes config/growatt_v0.14.holding_registry_map.override.csv
    │     documented name,write
    │     charge_voltage_limit,W
    │
    └── Writes variable_mask / variable_screen files
    
    ── Gateway restart required ──

MQTT Bridge (init_bridge, on scraper connect)
    │
    ├── Checks scraper.write_enabled == True          ← Gate 1: master switch
    │
    ├── Loads override CSV → allowlist = {"charge_voltage_limit"}
    │
    ├── Walks holding register map:
    │     "charge_voltage_limit" → write_mode=WRITE ✓, in allowlist ✓
    │       → subscribes: home/inverter/{id}/write/charge_voltage_limit
    │
    └── __write_topics = {"home/inverter/.../write/charge_voltage_limit": <entry>}

Home Assistant or other MQTT client
    │
    │  Publishes: home/inverter/{id}/write/charge_voltage_limit  →  "54.4"
    │
    ▼
MQTT Bridge (client_on_message)
    │  Looks up topic in __write_topics → found
    │  Calls _emit_message(entry, "54.4")
    │
    ▼
Gateway on_message callback
    │  Validates value against register range (0~60 for voltage limit)
    │  Value 54.4 is within range ✓
    │
    ▼
Modbus Scraper (modbus_base / modbus_tcp)
    │  write_enabled == True ✓
    │  Calls write_register(register=0x24, value=544)  ← FC06 Modbus write
    │
    ▼
Physical Device
    Register 0x24 is now set to 54.4V charge voltage limit
```

---

## Variable Mask and Screen

The **M** (mask) and **S** (screen) toggles in the protocol register table control which variables are published to all outputs. These are independent of write-back.

**Mask (allowlist):** When any variable for a device has mask enabled, MPG publishes *only* masked variables. It acts as an explicit include list. If no variables are masked, all variables are published.

**Screen (blocklist):** Screened variables are always excluded from all outputs, regardless of mask settings. Mask is applied first, then screen.

Both lists are written to per-transport text files on commit:

``` ini
config/variable_mask_{transport_name}.txt    (Though you can use any name as long as it matches the config key setting)
config/variable_screen_{transport_name}.txt
```

Mask and screen are mutually exclusive per register — enabling one automatically disables the other in the UI.

---

## Protocol Library

MPG ships with pre-built protocols for the following devices. All protocols are in the `protocols/` directory.

| Protocol Name | Device | Transport |
| --- | --- | --- |
| `apsystems_ecu_sunspec` | APsystems ECU SunSpec gateway | Modbus TCP |
| `deye_sunsynk` | Deye / Sunsynk hybrid inverters | Modbus |
| `deye_sunsynk_hybrid` | Deye / Sunsynk hybrid inverter extended map | Modbus |
| `eg4_18kpv` | EG4 18KPV inverter | Modbus |
| `eg4_3000ehv_v1` | EG4 3000EHV inverter | Modbus RTU |
| `eg4_gridboss_re` | EG4 GridBOSS / related equipment | Modbus RTU |
| `eg4_ll_s` | EG4 LL-S battery | Modbus RTU |
| `eg4_v58` | EG4 6000XP / 12000XP / 18KPV family | Modbus |
| `enphase_iq_gateway_sunspec` | Enphase IQ Gateway SunSpec | Modbus |
| `foxess_h1_lan` | FoxESS H1 LAN / KH / H3-style holding-register map | Modbus TCP |
| `fronius_sunspec` | Fronius SunSpec inverters | Modbus |
| `growatt_2020_v1.24` | Growatt inverter protocol v1.24 | Modbus RTU |
| `growatt_bms_canbus_v1.04` | Growatt BMS CAN bus v1.04 | CAN bus |
| `growatt_bms_rs485_1xsxxp_ess_v2.01` | Growatt BMS RS-485 1xSxxP ESS v2.01 | Modbus RTU |
| `growatt_v0.14` | Growatt SPF / off-grid inverter protocol v0.14 | Modbus RTU |
| `hdhk_16ch_ac_module` | HDHK 16-channel AC power monitor | Modbus RTU |
| `huawei_sun2000` | Huawei SUN2000 inverters | Modbus TCP |
| `next_power_victor_nm_re` | Next Power Victor NM RE | Modbus RTU |
| `pace_bms_v1.3` | PACE BMS RS-485 v1.3 | Modbus RTU |
| `pylon_can` | Pylon / Pylontech low-voltage CAN bus | CAN bus |
| `pylon_rs485_v3.3` | Pylon / Pylontech low-voltage RS-485 v3.3 | Pylon serial |
| `sigenergy_hybrid` | Sigenergy Sigen Hybrid / SigenStor EC individual device data | Modbus TCP |
| `sigenergy_plant` | Sigenergy SigenStor plant-level data (unit 247) | Modbus TCP |
| `sigineer_v0.11` | Sigineer solar inverter / charger v0.11 | Modbus RTU |
| `sma_energy_meter_speedwire` | SMA Energy Meter Speedwire | Modbus RTU |
| `sma_modbus_rtu` | SMA CAN / Modbus RTU map | CAN bus |
| `sma_sunny_home_manager` | SMA Sunny Home Manager | Modbus RTU |
| `sma_sunny_island` | SMA Sunny Island | Modbus RTU |
| `sma_sunny_island_v1` | SMA Sunny Island v1 | Modbus RTU |
| `sma_sunnyboy_tripower` | SMA Sunny Boy / Tripower | Modbus RTU |
| `sma_tripower_storage_hybrid` | SMA Tripower Storage Hybrid | Modbus RTU |
| `sok_sk48v100_pace_bms` | SOK SK48V100 / PACE BMS | Modbus RTU |
| `solaredge_sunspec` | SolarEdge SunSpec inverters | Modbus |
| `solax_hybrid_g4` | SolaX X1/X3 Hybrid G4-style inverters | Modbus TCP |
| `solark_hybrid` | Sol-Ark hybrid inverter | Modbus RTU |
| `solark_v1.1` | Sol-Ark Modbus v1.1 | Modbus RTU |
| `solis_hybrid` | Solis / Ginlong hybrid inverters | Modbus TCP |
| `solis_string` | Solis / Ginlong string inverters | Modbus TCP |
| `srne_2021_v1.96` | SRNE energy-storage inverter v1.96 | Modbus RTU |
| `srne_v1.7` | SRNE energy-storage inverter v1.7 | Modbus RTU |
| `srne_v3.9` | SRNE controller / inverter v3.9 | Modbus RTU |
| `sungrow_hybrid` | Sungrow SH / RS / RT hybrid inverters | Modbus TCP |
| `sungrow_sg` | Sungrow SG-series string inverters | Modbus TCP |
| `victron_bmv_battery_monitor` | Victron BMV battery monitor | Modbus RTU |
| `victron_gx_generic_canbus` | Victron GX generic CAN bus | CAN bus |
| `victron_gx_v3.3` | Victron GX v3.3 | Modbus RTU |
| `victron_mk3usb_vebus` | Victron MK3-USB VE.Bus | Modbus RTU |
| `victron_multiplus_quattro` | Victron MultiPlus / Quattro | Modbus RTU |
| `victron_phoenix_inverter` | Victron Phoenix inverter | Modbus RTU |
| `victron_smartsolar_mppt` | Victron SmartSolar MPPT | Modbus RTU |
| `victron_vedirect_serial` | Victron VE.Direct serial devices | VE.Direct serial |
| `victron_venus_gx_system` | Victron Venus GX system | Modbus RTU |
| `voltronic_bms_2020_03_25` | Voltronic BMS 2020-03-25 | Modbus RTU |
| `voltronic_bms_v1.1` | Voltronic BMS v1.1 | Modbus RTU |

For a community-maintained list of tested devices and firmware versions, see [`documentation/usage/devices_and_protocols.csv`](devices_and_protocols.csv).

For device-specific wiring and installation instructions, see [`documentation/devices/`](../devices/).

To build a protocol for a new device, use the **Analyze** page in the web UI. See the [Live Device Analysis](../../README.md#live-device-analysis) section in the main README for a walkthrough.
