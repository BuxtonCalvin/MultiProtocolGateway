# Protocols

A **protocol** is the map between raw register addresses on a hardware device and the named, typed, human-readable variables that MPG publishes to its outputs. Every scraper transport that reads data — Modbus TCP, Modbus RTU, CAN bus — requires a protocol to know what the register values mean and how to decode them.

## Table of Contents

- [Protocol File Structure](#protocol-file-structure)
- [The JSON Descriptor](#the-json-descriptor)
- [The Registry Map CSV](#the-registry-map-csv)
  - [Column Reference](#column-reference)
  - [Data Types](#data-types)
  - [Register Addressing](#register-addressing)
    - [Single Register](#single-register)
    - [Bit and Byte Offsets](#bit-and-byte-offsets)
    - [Multi-Register (32-bit and 64-bit) Values](#multi-register-32-bit-and-64-bit-values)
    - [Concatenated Ranges (ASCII / String)](#concatenated-ranges-ascii--string)
  - [Byte and Word Order](#byte-and-word-order)
  - [Adjustments](#adjustments)
    - [Endian Order](#endian-order)
    - [Value Formulas](#value-formulas)
    - [Bit Flag Codes](#bit-flag-codes)
  - [Write Mode](#write-mode)
  - [Value Ranges and Codes](#value-ranges-and-codes)
  - [Per-Register Read Intervals](#per-register-read-intervals)
  - [Unit Multipliers](#unit-multipliers)
- [How MPG Decodes a Register](#how-mpg-decodes-a-register)
  - [Single 16-bit Register](#single-16-bit-register)
  - [Multi-Register Pairs and Quads](#multi-register-pairs-and-quads)
  - [Register Merge at Scan Time](#register-merge-at-scan-time)
- [Variable Mask and Screen](#variable-mask-and-screen)
- [Protocol Overrides](#protocol-overrides)
- [Custom Protocols](#custom-protocols)
- [Managing Write-Back via the Web UI](#managing-write-back-via-the-web-ui)
  - [Prerequisites](#prerequisites)
  - [Enabling Write-Back on a Register](#enabling-write-back-on-a-register)
  - [What Happens on Commit](#what-happens-on-commit)
  - [The Full Write-Back Flow, End to End](#the-full-write-back-flow-end-to-end)
- [Protocol Library](#protocol-library)

---

## Protocol File Structure

Each protocol lives in its own subdirectory under `protocols/` and consists of up to three files:

``` ini
protocols/
└── eg4/
    ├── eg4_18kpv.json
    ├── eg4_18kpv.input_registry_map.csv
    └── eg4_18kpv.holding_registry_map.csv
```

| File | Purpose |
| --- | --- |
| `{name}.json` | Protocol-wide defaults, baud rate, byte order, and enum code definitions |
| `{name}.input_registry_map.csv` | Read-only (input) registers — polled from the device |
| `{name}.holding_registry_map.csv` | Read/write (holding) registers — polled and optionally written back |
| `{name}.registry_map.csv` | Generic register map for protocols without an input/holding distinction (CAN bus, etc.) |

The four Modbus register types and their properties:

| Type | Address Prefix | Access | Size | Description |
| --- | --- | --- | --- | --- |
| Coil | 0xxxx | Read/Write | 1-bit | Digital ON/OFF output control (e.g. relays) |
| Discrete | 1xxxx | Read-Only | 1-bit | Digital status input (e.g. limit switches) |
| Input Register | 3xxxx | Read-Only | 16-bit | Analog measurement values (e.g. voltage, temperature) |
| Holding Register | 4xxxx | Read/Write | 16-bit | Configuration and setpoint values |

A protocol is activated in `config.cfg` by setting `protocol_version` to the base filename without extension:

```ini
[transport.inverter]
protocol_version = eg4_18kpv
```

The web UI manages this setting and provides a protocol browser to select from all available protocols.

---

## The JSON Descriptor

The `.json` file sets protocol-wide defaults that apply to all registers in the map. All fields are optional.

```json
{
  "transport": "modbus_tcp",
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
| `transport` | Default transport class for this protocol. Overridden in `config.cfg`. |
| `baud` | Default baud rate for RTU connections. |
| `byteorder` | Default byte order for all registers: `big` (most common) or `little`. Individual registers override this via the `adjustments` column. |
| `read_interval` | Default polling interval in seconds. Overridden by the transport's `read_interval` setting. |
| `{documented_name}_codes` | Enum code mappings for flag registers. The key must match a `documented name` column value with `_codes` appended (e.g. `battery_status_codes`). |

---

## The Registry Map CSV

The CSV files are the core of a protocol — one row per logical register or register group. The delimiter may be `,` or `;` (not both). Column names are case-insensitive; underscores and spaces are interchangeable. Both LibreOffice Calc and Excel are compatible.

### Column Reference

| Column | Required | Description |
| --- | --- | --- |
| `register` | Yes | Register address. Supports single addresses, multi-register pairs, bit/byte offsets, and ranges. See [Register Addressing](#register-addressing). |
| `variable name` | No | Friendly name used in MQTT topics, database columns, and all outputs. When blank, `documented name` is used. |
| `documented name` | Yes | Original name from the manufacturer's documentation. Used as the primary key for override matching. |
| `data type` | Yes | How to interpret the raw register bytes. See [Data Types](#data-types). |
| `unit` | No | Unit symbol (e.g. `V`, `A`, `W`, `°C`, `%`). Supports a numeric multiplier prefix — see [Unit Multipliers](#unit-multipliers). |
| `writable` | No | Write permission for this register. See [Write Mode](#write-mode). |
| `values` | No | Valid value range or enum codes. Used for validation and write safety. |
| `adjustments` | No | JSON object specifying endian order, value formulas, or flag code mappings. See [Adjustments](#adjustments). |
| `read interval` | No | Per-register polling override. See [Per-Register Read Intervals](#per-register-read-intervals). |
| `note` | No | Description shown in the protocol editor and analysis tool. |
| `read command` | No | For protocols requiring a command byte before reading. Hex prefix `x` or raw UTF-8. |

### Data Types

| Type | Size | Description |
| --- | --- | --- |
| `USHORT` | 16-bit | Unsigned integer (0–65535). Default for most Modbus registers. |
| `SHORT` | 16-bit | Signed integer (−32768–32767). |
| `UINT` | 32-bit | Unsigned integer spanning two 16-bit registers. |
| `INT` | 32-bit | Signed integer spanning two 16-bit registers. |
| `UINT64` | 64-bit | Unsigned integer spanning four 16-bit registers. |
| `FLOAT32` | 32-bit | IEEE 754 single-precision float spanning two registers. |
| `FLOAT64` | 64-bit | IEEE 754 double-precision float spanning four registers. |
| `BYTE` | 8-bit | Single unsigned byte. |
| `16BIT_FLAGS` | 16-bit | Each bit is an independent flag, labeled `b0`–`b15`. |
| `8BIT_FLAGS` | 8-bit | Eight independent bit flags. |
| `32BIT_FLAGS` | 32-bit | Thirty-two independent bit flags. |
| `#bit` | # bits | Unsigned sub-register value. E.g. `3bit` = 3-bit value (0–7). |
| `#sbit` | # bits | Signed sub-register value. |
| `#smbit` | # bits | Sign-magnitude sub-register value. |
| `ACC32` | 32-bit | Accumulated 32-bit counter spanning two registers. |
| `ASCII` | varies | String value. Two characters per register by default. |
| `ASCII.#` | # chars | Fixed-length string. E.g. `ASCII.7` reads 7 characters. |
| `HEX` | varies | Hexadecimal string representation. |

Common aliases are also accepted: `INT16` → `SHORT`, `UINT16` → `USHORT`, `UINT32` → `UINT`, `INT32` → `INT`.

For multi-register data types (`UINT`, `INT`, `UINT64`, `FLOAT32`, `FLOAT64`, `ACC32`), the register address must use the multi-register format described below.

---

### Register Addressing

#### Single Register

Plain decimal or hex (`0x`-prefixed):

``` ini
17
0x2C
```

#### Bit and Byte Offsets

A bit offset selects one or more bits within a 16-bit register:

``` ini
5.b0          single bit 0 of register 5
5.b3-b7       bits 3 through 7 of register 5
```

A byte offset selects one byte:

``` ini
5.0           byte 0 (low byte) of register 5
5.1           byte 1 (high byte) of register 5
```

#### Multi-Register (32-bit and 64-bit) Values

When a manufacturer encodes a value across more than one physical register, the register address uses an underscore-separated list of addresses in **word order from low significance to high significance**:

``` ini
40_41         UINT32: low word at register 40, high word at register 41
41_40         UINT32: low word at register 41, high word at register 40 (reversed physical layout)
40_50         UINT32: non-contiguous — low word at 40, high word at 50
40_41_42_43   UINT64: four words, lowest at 40, highest at 43
```

The key properties of this format:

- **Token 0 is always the low-significance word**, regardless of which physical address is numerically lower. `41_40` unambiguously means low word = register 41, high word = register 40.
- **Registers need not be contiguous**. `40_50` is valid. MPG reads each address independently.
- **Physical layout in the manufacturer's specification is preserved**. A user reading the spec alongside the CSV will see both addresses accounted for, eliminating confusion about "missing" rows.
- **Word count is self-describing**. Two tokens = 32-bit value, four tokens = 64-bit value. MPG cross-checks this count against the `data type` column and logs a warning if they disagree.
- **The `data type` column sets the decode format** (`UINT`, `INT`, `FLOAT32`, etc.) and is the cross-check for the address token count. The address list is the authoritative source for which registers to read and in what order.

Hex addresses are supported in the same format: `0x28_0x29`.

The endian encoding of the words (how the bytes within each 16-bit register are ordered, and how the words are assembled) is controlled separately by the `adjustments` column — see [Endian Order](#endian-order).

#### Concatenated Ranges (ASCII / String)

For multi-register string or ASCII data where all addresses are consecutive, a range is more concise:

``` ini
7~14          registers 7 through 14 (ascending)
r7~14         registers 14 down to 7 (descending — reversed read order)
```

Range notation implies consecutive ascending addresses and is intended for `ASCII` and `HEX` types. For numeric multi-word values, always use the explicit underscore format instead.

---

### Byte and Word Order

Modbus transmits each 16-bit register in big-endian byte order on the wire. For values spanning more than one register, manufacturers have adopted four distinct encoding conventions defined by two independent axes:

| Canonical name | word_reversed | bytes_reversed | Byte sequence |
| --- | --- | --- | --- |
| `big_endian-ABCD` | False | False | ABCD (default) |
| `big_endian_byte_swap-BADC` | False | True | BADC |
| `little_endian-CDAB` | True | False | CDAB |
| `little_endian_byte_swap-DCBA` | True | True | DCBA |

**`word_reversed`** controls which physical register holds the low-significance word. When `True`, the low word is at the lower register address. This is separate from the address token order in the CSV — the CSV token order defines which register is the low word; `word_reversed` tells the decoder that the manufacturer placed that low word at the lower address.

**`bytes_reversed`** controls the byte order within each individual 16-bit register. When `True`, bytes within each register are in little-endian order.

These two axes are completely independent. The protocol-wide default is `big_endian-ABCD`. Individual registers override it via the `adjustments` column.

---

### Adjustments

The `adjustments` column holds a JSON object that can contain one or more directives. Values must be valid JSON — string values must be quoted, and the outer object must use `{` `}`.

#### Endian Order

```json
{"Register_Endian": "little_endian-CDAB"}
```

Sets the word and byte order for this register. Valid values are the four canonical names from the table above. Overrides the protocol-wide `byteorder` setting for this register only.

For multi-register values, this is the most important field to get right. EG4 18KPV input registers, for example, require `little_endian-CDAB` for all 32-bit energy counters.

#### Value Formulas

Applies a conditional formula to scale or transform the raw register value before publishing:

```json
{"High_Low": "x?(0,1000]->x/1000 x?(1000,2000)->(1000-x)/1000"}
```

The `x` variable holds the raw decoded value. Each condition uses interval notation (`(` exclusive, `]` inclusive). Conditions are evaluated left to right; the first match wins.

#### Bit Flag Codes

Inline flag definitions as an alternative to defining them in the JSON descriptor:

```json
{"b0": "StandBy", "b1": "On", "b2": "Fault"}
```

Multi-bit flags:

```json
{"b0": "StandBy", "b0&b1": "Operational", "b1": "Error"}
```

Numeric value mappings (for `USHORT` or similar types):

```json
{"75": "Error", "50": "Warning", "0": "Normal"}
```

Multiple adjustment directives can be combined in a single JSON object:

```json
{"Register_Endian": "little_endian-CDAB", "High_Low": "x?(0,32768]->x x?(32768,65535)->x-65536"}
```

---

### Write Mode

The `writable` column sets the read/write permission sourced from the manufacturer's specification. This is immutable from the protocol CSV — user write-back is enabled separately through the web UI (see [Managing Write-Back](#managing-write-back-via-the-web-ui)).

| Value | Meaning |
| --- | --- |
| `R` | Read-only. Default if omitted. |
| `RW` | Read and write supported by the device. |
| `W` | Write-only. |
| `RD` | Read disabled — register is skipped during polling. |

---

### Value Ranges and Codes

The `values` column serves two purposes.

**Validation range** — a hyphen or tilde-separated range defines the valid values for write safety:

``` ini
0-100
0~65535
```

**Enum codes** — a JSON object maps raw values to human-readable labels. Any data type can use codes:

```json
{"0": "Idle", "1": "Charging", "2": "Discharging", "3": "Fault"}
```

Codes can also be defined in the JSON descriptor as `{documented_name}_codes`, which is useful when the code table is large or shared across registers.

---

### Per-Register Read Intervals

The `read interval` column overrides the transport's default polling interval for a specific register. The transport's `read_interval` (in seconds) is the minimum resolution.

| Format | Meaning | Example |
| --- | --- | --- |
| `#x` | Transport interval × # | `3x` with `read_interval=5` → 15s |
| `#s` | Fixed seconds | `30s` → every 30 seconds |
| `#ms` | Fixed milliseconds | `500ms` → every 500ms |

A value shorter than the transport interval is silently clamped to the transport interval.

---

### Unit Multipliers

A unit string can include a numeric multiplier prefix that MPG applies automatically to scale the raw register value:

``` ini
0.1V      → raw value × 0.1, published with unit "V"
0.01A     → raw value × 0.01, published with unit "A"
1kWh      → raw value × 1, published with unit "kWh"
```

The multiplier is parsed as a float. A multiplier of zero is treated as 1.0 (no scaling).

---

## How MPG Decodes a Register

Understanding the decode pipeline clarifies why address format, data type, and adjustments must all agree.

### Single 16-bit Register

For a standard `USHORT` or `SHORT` register at address `N`:

1. The Modbus layer reads register `N` → raw 16-bit value.
2. Byte order within the register is applied (almost always big-endian by default).
3. Adjustment formulas are applied if present.
4. The unit multiplier scales the result.
5. The value is published under `variable_name`.

### Multi-Register Pairs and Quads

For a multi-register entry (e.g. `40_41` with `data type = UINT`):

1. `entry_word_count` is determined from the address token count (2 tokens → 2 words). The `data type` is cross-checked; a mismatch logs a WARNING and the address count wins.
2. `calculate_registry_ranges` includes **all addresses in the token list** in the Modbus read request. For non-contiguous pairs like `40_50`, both registers are covered even though they are far apart.
3. The Modbus layer reads the full range covering all addresses; the raw values land in the registry dictionary keyed by address.
4. `_register_words_to_bytes` assembles the bytes using the **explicit address list in token order**: `registry[40]` contributes bytes 0–1, `registry[41]` contributes bytes 2–3. This is independent of which address is numerically lower — token order is the sole authority.
5. The `Register_Endian` adjustment (if present) controls word reversal and byte swap within each word before assembly.
6. The assembled bytes are decoded as the specified `data type` (`UINT` → unsigned 32-bit integer, etc.).
7. Adjustment formulas, unit multiplier, and enum code substitution are applied.
8. The value is published under `variable_name` (e.g. `epv1_all`) as a single logical metric.

### Register Merge at Scan Time

MPG's scanner automatically detects and merges `_l`/`_h` register pairs in raw protocol CSV files before storing them in the database. A pair is detected when:

- Row N has a `variable name` ending in `_l`
- Row N+1 has `variable name` equal to the stem + `_h`
- Both addresses are plain integers

The merge produces a single database row with:

- `variable_name` = the stem (e.g. `echg_all`)
- `register` = `"{low_addr}_{high_addr}"` (e.g. `"40_41"`)
- `data_type` = `UINT` (inferred or inherited)
- `adjustments` = inherited or user-supplied (this is where `Register_Endian` is set)

The `_l` and `_h` rows in the original CSV are a naming convention that signals to the scanner that a pair exists. Once merged, neither suffix appears in any output, database, or mask file. The runtime decoder never looks for `_l`/`_h` names — it reads the address token list and data type exclusively.

This means a manufacturer CSV that uses a different pair-naming convention (or no naming convention at all) requires manual entry of the `low_high` address format directly in the `register` column.

---

## Variable Mask and Screen

The **M** (mask) and **S** (screen) toggles in the protocol register table control which variables are published to all outputs — databases, MQTT, InfluxDB — independently of write-back.

**Mask (allowlist):** When any variable for a device has mask enabled, MPG publishes *only* masked variables. It acts as an explicit include list. If no variables are masked, all variables are published.

**Screen (blocklist):** Screened variables are always excluded from all outputs, regardless of mask settings. The mask is applied first, then the screen.

Both lists are written to per-transport text files on commit:

``` ini
config/variable_mask_{transport_name}.txt
config/variable_screen_{transport_name}.txt
```

These files contain one logical variable name per line (the stem name, without `_l` or `_h` suffixes). Mask and screen are mutually exclusive per register — enabling one automatically disables the other in the UI.

---

## Protocol Overrides

User edits made in the protocol editor (field values such as `note`, `adjustments`, `unit`, `variable name`) are written to an override CSV on commit:

``` ini
config/{protocol_name}.{registry_type}_registry_map.override.csv
```

At startup, the runtime loads the base protocol CSV first, then merges the override on top — any row whose `documented name` matches is updated with the override values. This preserves the original protocol file while allowing user customization that survives protocol library updates.

---

## Custom Protocols

To create a new protocol:

1. Create a folder under `protocols/` named after the manufacturer.
2. Create a `.json` descriptor (optional but recommended).
3. Create one or more CSV registry map files following the column layout above.
4. Place the files in the manufacturer folder. The scanner discovers them automatically on the next scan.
5. In the web UI, use the **Create Protocol** button for a guided walkthrough, or use the **Analyze** page to read registers live from a connected device and build the map interactively.

CSV delimiter can be `,` or `;`. Column headers are case-insensitive. An empty `variable name` column causes MPG to use `documented name` as the variable name for that row.

---

## Managing Write-Back via the Web UI

Write-back allows MPG to push values from an external source (MQTT, Home Assistant, etc.) back to a device holding register.

### Prerequisites

- The register must have `writable = RW` or `W` in the protocol CSV (Gate 1 — the protocol permits writing).
- The user must enable the **W** toggle for that register in the scraper's protocol table (Gate 2 — the user explicitly opts in).
- The transport must have `write_enabled = true` in `config.cfg`.

Both gates must be satisfied. A register that is read-only per the protocol cannot be write-enabled in the UI regardless of configuration.

### Enabling Write-Back on a Register

1. Open the Scrapers menu and select the scraper device.
2. In the Protocol Metrics section, find the holding register to enable.
3. Check the **W** box. The UI enforces the two-gate rule and disables the checkbox for read-only registers.
4. Click **Commit All Changes**.

### What Happens on Commit

``` ini
config_writer.commit_all():
├── Writes config.cfg
├── Writes config/{device_name}.writable.csv
│     documented name,write
│     charge_voltage_limit,W
└── Writes variable_mask_{transport_name}.txt / variable_screen_{transport_name}.txt files
```

A gateway restart is required after commit for write-back to take effect.

### The Full Write-Back Flow, End to End

``` ini
User in Web UI
    │  Checks the W box on "charge_voltage_limit" (holding register 0x24)
    ▼
Protocol Service (toggle_register_field)
    │  Gate 1 — is_writable_by_protocol: write_mode_protocol ∈ {RW, W, WO}
    │  Gate 2 — DeviceProtocolSelection.user_write_enabled = True
    ▼
Commit (config_writer.commit_all)
    ├── Writes config.cfg
    ├── Writes eg4_18kpv.holding_registry_map.override.csv
    │     documented name,write
    │     charge_voltage_limit,W
    └── Writes variable_mask / variable_screen files

── Gateway restart required ──

MQTT Bridge (init_bridge, on scraper connect)
    ├── Checks scraper.write_enabled == True
    ├── Loads override CSV → allowlist = {"charge_voltage_limit"}
    ├── Walks holding register map:
    │     "charge_voltage_limit" → write_mode=WRITE ✓, in allowlist ✓
    │       → subscribes: home/inverter/{id}/write/charge_voltage_limit
    └── __write_topics = {"home/inverter/.../write/charge_voltage_limit": <entry>}

Home Assistant or other MQTT client
    │  Publishes: home/inverter/{id}/write/charge_voltage_limit → "54.4"
    ▼
MQTT Bridge (client_on_message)
    │  Looks up topic in __write_topics → found
    │  Calls _emit_message(entry, "54.4")
    ▼
Gateway on_message callback
    │  Validates value against register range (0~60 for voltage limit)
    │  54.4 is within range ✓
    ▼
Modbus Scraper
    │  write_enabled == True ✓
    │  Calls write_register(register=0x24, value=544)  ← FC06 Modbus write
    ▼
Physical Device
    Register 0x24 is now set to 54.4V
```

---

## Protocol Library

MPG ships with pre-built protocols for the following devices. All protocols are in the `protocols/` directory.

| Protocol Name | Device | Transport |
| --- | --- | --- |
| `apsystems_ecu_sunspec` | APsystems ECU SunSpec gateway | Modbus TCP |
| `deye_sunsynk` | Deye / Sunsynk hybrid inverters | Modbus |
| `deye_sunsynk_hybrid` | Deye / Sunsynk hybrid inverter extended map | Modbus |
| `eg4_18kpv` | EG4 18KPV inverter | Modbus TCP |
| `eg4_3000ehv_v1` | EG4 3000EHV inverter | Modbus RTU |
| `eg4_gridboss_re` | EG4 GridBOSS / related equipment | Modbus RTU |
| `eg4_ll_s` | EG4 LL-S battery | Modbus RTU |
| `eg4_v58` | EG4 6000XP / 12000XP / 18KPV family | Modbus |
| `enphase_iq_gateway_sunspec` | Enphase IQ Gateway SunSpec | Modbus TCP |
| `foxess_h1_lan` | FoxESS H1 LAN / KH / H3-style holding-register map | Modbus TCP |
| `fronius_sunspec` | Fronius SunSpec inverters | Modbus TCP |
| `growatt_2020_v1.24` | Growatt inverter protocol v1.24 | Modbus RTU |
| `growatt_bms_canbus_v1.04` | Growatt BMS CAN bus v1.04 | CAN bus |
| `growatt_bms_rs485_1xsxxp_ess_v2.01` | Growatt BMS RS-485 ESS v2.01 | Modbus RTU |
| `growatt_v0.14` | Growatt SPF / off-grid inverter v0.14 | Modbus RTU |
| `hdhk_16ch_ac_module` | HDHK 16-channel AC power monitor | Modbus RTU |
| `huawei_sun2000` | Huawei SUN2000 inverters | Modbus TCP |
| `next_power_victor_nm_re` | Next Power Victor NM RE | Modbus RTU |
| `pace_bms_v1.3` | PACE BMS RS-485 v1.3 | Modbus RTU |
| `pylon_can` | Pylon / Pylontech low-voltage CAN bus | CAN bus |
| `pylon_rs485_v3.3` | Pylon / Pylontech low-voltage RS-485 v3.3 | Pylon serial |
| `sigenergy_hybrid` | Sigenergy SigenStor hybrid / EC individual device data | Modbus TCP |
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
| `solaredge_sunspec` | SolarEdge SunSpec inverters | Modbus TCP |
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

For device-specific wiring and installation instructions, see [`documentation/devices/`](../devices/).

To build a protocol for a new device, use the **Analyze** page in the web UI to read registers live from a connected device and build the map interactively.
