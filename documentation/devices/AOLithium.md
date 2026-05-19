# AOLithium Battery — MQTT Integration Guide

> **Supported Models:** AOLithium 48V server rack battery series
> **Protocol:** Voltronic RS485 (Modbus RTU) · Victron GX Generic CAN · SMA Sunny Island CAN
> **Interface:** RS485A RJ45 port · CAN RJ45 port
> **Status:** Confirmed working

---

## Table of Contents

1. [Overview](#1-overview)
2. [Protocol Selection & DIP Switch Settings](#2-protocol-selection--dip-switch-settings)
   - 2.1 [Voltronic RS485 Mode](#21-voltronic-rs485-mode)
   - 2.2 [SMA / Victron CAN Bus Mode](#22-sma--victron-can-bus-mode)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [RS485 Mode Hardware](#31-rs485-mode-hardware)
   - 3.2 [CAN Bus Mode Hardware](#32-can-bus-mode-hardware)
   - 3.3 [Option: Waveshare USB to RS485 (Industrial Grade)](#33-option-waveshare-usb-to-rs485-industrial-grade)
   - 3.4 [Option: Waveshare RS485 to Ethernet/Wi-Fi Bridge](#34-option-waveshare-rs485-to-ethernetwi-fi-bridge)
4. [Port Identification & Pinout](#4-port-identification--pinout)
   - 4.1 [RS485A Port Pinout](#41-rs485a-port-pinout)
   - 4.2 [CAN Bus Port Pinout](#42-can-bus-port-pinout)
5. [Multi-Battery (Parallel) Configuration](#5-multi-battery-parallel-configuration)
6. [Inverter Compatibility](#6-inverter-compatibility)
7. [AOLithium BMS Local API Access](#7-aolithium-bms-local-api-access)
8. [MPG Configuration](#8-mpg-configuration)
9. [Known Issues & Caveats](#9-known-issues--caveats)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

AOLithium batteries support multiple communication protocols, selectable via hardware DIP switches on the BMS board. Protocol selection determines which inverter types the battery can communicate with and which physical port carries monitoring data.

> **Important:** AOLithium's printed manual contains documented inaccuracies regarding RS485 port pin assignments. Always verify against Section 4 of this guide rather than relying solely on the printed manual.

**Available communication paths:**

| Mode | Protocol | Physical Port | Compatible Inverters |
| --- | --- | --- | --- |
| RS485 | Voltronic BMS 2020 | RS485A RJ45 | Voltronic, MPP Solar, LuxPower-based |
| CAN Bus | Victron GX Generic / SMA Sunny Island | CAN RJ45 | Victron, SMA, Deye, Sunsynk, LuxPower |

---

## 2. Protocol Selection & DIP Switch Settings

The AOLithium BMS uses DIP switches to select the active communication protocol.

> **The battery must be fully power-cycled after any DIP switch change** for the new setting to take effect. The BMS does not hot-reload protocol changes.

### 2.1 Voltronic RS485 Mode

Used for communication with Voltronic-based inverters (MPP Solar, Axpert, Growatt RS485, etc.).

**DIP switch settings:**

| DIP Switch | Position | Notes |
| --- | --- | --- |
| 1 | ON | Activates RS485 Voltronic mode |
| 2–6 | OFF | Leave off for single-battery RS485 |

**Inverter setting required:**

- Set battery type to `LI-b` in the Voltronic inverter menu (not `PYL`)

> Community note: Only the directly connected (master) battery's aggregated data is available over RS485. Slave battery data is not included in the RS485 data stream. For full multi-battery visibility, use CAN bus mode instead.

---

### 2.2 SMA / Victron CAN Bus Mode

Used for communication with Victron GX (Cerbo GX / Venus OS) and SMA Sunny Island inverters.

**DIP switch settings:**

| Target System | DIP 5 | DIP 6 | Protocol Used |
| --- | --- | --- | --- |
| Victron GX (Cerbo, Venus) | OFF | OFF | `victron_gx_generic_canbus` |
| SMA Sunny Island | OFF | OFF | `victron_gx_generic_canbus` or `sma_sunny_island_v1` |
| Deye / Sunsynk / LuxPower CAN | ON | ON | Factory default CAN mode |

> **Protocol preference:** `victron_gx_generic_canbus` is preferred over `sma_sunny_island_v1` — it provides more data fields. Use `sma_sunny_island_v1` only if the generic CAN protocol fails to establish communication.

---

## 3. Hardware Requirements

### 3.1 RS485 Mode Hardware

**What you need:**

- USB to RS485 adapter with RJ45 connector or screw terminals
- RJ45 Ethernet cable (CAT5e or CAT6) — custom pinout (see Section 4.1)

**Suitable adapters:**

| Adapter | Notes |
| --- | --- |
| Generic CH340-based USB RS485 | Adequate for single battery |
| **Waveshare USB TO RS485 (B)** | Industrial grade, isolated — preferred for permanent installs |
| DSD TECH SH-U11F | Isolated, includes termination resistor |

---

### 3.2 CAN Bus Mode Hardware

**What you need:**

- USB CAN bus adapter
- RJ45 Ethernet cable (one end cut) to access CAN-H and CAN-L wires

**Compatible CAN adapters:**

| Adapter | Notes |
| --- | --- |
| **Waveshare USB-CAN-A** | CANable-compatible, open-source firmware, Linux/macOS/Windows |
| PEAK PCAN-USB | Industrial grade, all platforms |
| Generic CANable 1.0 | Community use, lower cost |

**For Victron integration:** Connect the battery CAN port directly to a VE.Can port on the Cerbo GX or Venus GX using a VE.Can RJ45 cable (or a custom cable per Section 4.2).

---

### 3.3 Option: Waveshare USB to RS485 (Industrial Grade)

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, rail-mount | Single battery RS485 monitoring |
| **USB TO RS485/422** | Dual-protocol, TVS protection, 120Ω termination | Long cable runs |

Built-in isolation is particularly important in battery environments with large DC switching currents.

---

### 3.4 Option: Waveshare RS485 to Ethernet/Wi-Fi Bridge

For network-connected RS485 monitoring without a locally co-located host.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus/MQTT gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Wire RS485A pin 8 (A+) and pin 7 (B−) from battery to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set baud rate to 9600

---

## 4. Port Identification & Pinout

### 4.1 RS485A Port Pinout

The RS485A port is used for RS485 / Voltronic communication. Connect the inverter-side cable to this port on the **master battery only**.

> ⚠️ **Manual discrepancy:** AOLithium's printed manual incorrectly identifies the GND pin as Pin 6 on some revisions. Community testing confirms Pin 3 is the correct GND pin. Use the table below rather than the printed manual.

**RS485A RJ45 Pinout (community verified):**

| RJ45 Pin | Signal | Notes |
| --- | --- | --- |
| 7 | RS485 B− | |
| 8 | RS485 A+ | |
| 3 | GND | ⚠️ Manual may incorrectly say Pin 6 — use Pin 3 |

**Wiring to USB RS485 adapter:**

``` text
RS485A Pin 8 (A+)  →  Adapter A+
RS485A Pin 7 (B−)  →  Adapter B−
RS485A Pin 3 (GND) →  Adapter GND (recommended)
```

**Voltronic inverter RS485 cable wiring:**

``` text
Battery Pin 8 (A+)   →  Inverter RS485 A
Battery Pin 7 (B−)   →  Inverter RS485 B
Battery Pin 3 (GND)  →  Inverter GND (optional)
```

> **Note:** The included AOLithium communication cable has labeled ends. Always plug the "Battery" end into the battery and the "Inverter" end into the inverter. The cable is directional and is not reversible — plugging it in backwards will prevent communication.

---

### 4.2 CAN Bus Port Pinout

The CAN port is a shared-function RJ45. When DIP switches are set for CAN mode, the following pins are active:

| RJ45 Pin | Signal |
| --- | --- |
| 1 | CAN-H |
| 2 | CAN-L |

**For Victron Cerbo GX / Venus GX:**

Use a standard RJ45 cable from the battery CAN port directly to a VE.Can port on the GX device.

**For SMA Sunny Island:**

Refer to the SMA Sunny Island BMS cable pinout in SMA documentation for the correct terminal mapping.

---

## 5. Multi-Battery (Parallel) Configuration

AOLithium supports multiple batteries in parallel. Protocol and DIP switch address assignments vary by communication mode.

### RS485 Mode (Voltronic)

- RS485 only reads the directly connected master battery
- Slave battery data is **not** included in the RS485 data stream
- Switch to CAN bus mode for full multi-battery visibility

### CAN Bus Mode (Recommended for Multi-Battery)

- Connect batteries together using the inter-battery CAN cables (typically included)
- Assign each battery a unique DIP switch address

**DIP switch addressing (binary, DIP 1–4):**

| DIP 1 | DIP 2 | DIP 3 | DIP 4 | Address |
| --- | --- | --- | --- | --- |
| ON | OFF | OFF | OFF | Master / Address 1 |
| OFF | ON | OFF | OFF | Slave / Address 2 |
| ON | ON | OFF | OFF | Slave / Address 3 |
| OFF | OFF | ON | OFF | Slave / Address 4 |

Only the master battery connects to the inverter or monitoring host via CAN or RS485.

---

## 6. Inverter Compatibility

| Inverter Type | Protocol | DIP Setting | Cable Type |
| --- | --- | --- | --- |
| Voltronic (Axpert, MPP Solar) | `voltronic_bms_2020_03_25` | DIP1 ON | RS485 custom RJ45 |
| Growatt (RS485 mode) | `voltronic_bms_2020_03_25` | DIP1 ON | RS485 custom RJ45 |
| Victron (Cerbo GX, Venus GX) | `victron_gx_generic_canbus` | DIP5 OFF, DIP6 OFF | CAN RJ45 |
| SMA Sunny Island | `victron_gx_generic_canbus` | DIP5 OFF, DIP6 OFF | CAN RJ45 |
| Deye / Sunsynk / LuxPower | CAN factory default | DIP5 ON, DIP6 ON | CAN RJ45 |

---

## 7. AOLithium BMS Local API Access

Some AOLithium battery generations include a Bluetooth monitoring module or, in commercial rack configurations, an Ethernet / RS232 port on the BMS. These interfaces provide additional data access paths beyond the RS485A Modbus interface.

### Bluetooth Monitoring

AOLithium's companion mobile app communicates with the BMS via Bluetooth on supported models. The app displays per-cell voltage, temperature, SOC, cycle count, and fault status.

Community reverse-engineering of PACE-based BMS Bluetooth protocols may be applicable to AOLithium units sharing that hardware:

``` python
# pybms or similar library — verify compatibility with your firmware
from bms.pace import PaceBms

bms = PaceBms(port="/dev/ttyUSB0", baudrate=9600, address=1)
data = bms.read_all()
print(f"SOC: {data['soc']}%")
print(f"Cell voltages: {data['cell_voltages']}")
```

### RS232 / Ethernet Interface (Commercial Rack Models)

Some AOLithium commercial rack battery configurations (typically 5kWh+ systems) include an RS232 or Ethernet port on the BMS management board.

**RS232 local access:**

``` text
Baud: 9600
Protocol: Varies by firmware — may use PACE proprietary or Modbus RTU
```

**Ethernet / HTTP interface (where available):**

``` text
http://<bms-ip>/status
```

Use your browser's developer tools on the BMS web interface to identify the JSON endpoint and response schema for your specific firmware version.

**Common response fields:**

``` json
{
  "soc": 85,
  "voltage": 51.2,
  "current": -10.5,
  "temperature": 28,
  "cell_voltages": [3.201, 3.202, 3.200, 3.199],
  "cycles": 42,
  "status": "discharging"
}
```

> **MPG module note:** A `aolithium_bms_http_v1` protocol module polling the local HTTP JSON endpoint on AOLithium commercial rack units with Ethernet BMS modules would provide per-cell data and full pack telemetry without RS485 cabling — particularly valuable for monitoring individual cell voltages in multi-battery installations.

---

## 8. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### RS485 / Voltronic Mode

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = voltronic_bms_2020_03_25
```

### SMA / Victron CAN Bus Mode

CAN bus mode is handled by the GX device (Victron) or inverter (SMA) directly — MPG is not used in the CAN bus data path. See the Victron guide for integrating CAN-connected battery data via Venus OS.

``` ini
protocol_version = victron_gx_generic_canbus
```

> Alternative (limited data): `sma_sunny_island_v1`

---

## 9. Known Issues & Caveats

| Issue | Description |
| --- | --- |
| Manual pin error | Printed manual incorrectly identifies GND as Pin 6 in some revisions. Use Pin 3 (community verified). |
| RS485 single-battery limitation | Only the master battery reports data over RS485. This is a protocol limitation, not a hardware fault. |
| Cable directionality | The included communication cable is not reversible. Plugging it in backwards will prevent communication with no error indication. |
| Restart required after DIP change | DIP switch changes only take effect after a full battery power-cycle. Toggle the BMS power button OFF, wait 10 seconds, then restart. |

---

## 10. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response from BMS | DIP switch set to wrong mode | Verify DIP position matches desired protocol (RS485 vs. CAN) |
| Only master battery data visible | RS485 protocol limitation | Expected behavior — switch to CAN bus mode for full multi-battery visibility |
| Inverter shows error F61 | Wrong RS485 cable direction | Use labeled cable ends; verify cable is not reversed |
| Victron GX does not recognize battery | Wrong CAN DIP settings | Set DIP5 OFF, DIP6 OFF for Victron / SMA CAN mode |
| Battery does not change protocol after DIP change | BMS requires full power cycle | Power off BMS completely, wait 10 seconds, then restart |
| GND pin connection fails | Manual pin reference mismatch | Use Pin 3 for GND — community verified; ignore Pin 6 reference in some manual revisions |
