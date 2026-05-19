# Sigineer Power — MQTT Integration Guide

> **Supported Models:** Sigineer Power inverter/charger series
> **Protocol:** Modbus RTU over USB
> **Interface:** USB-A or USB-B
> **Status:** Confirmed working

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [Option A — USB Cable (Direct)](#31-option-a--usb-cable-direct)
   - 3.2 [Option B — Waveshare USB to RS485 (RS485 Models)](#32-option-b--waveshare-usb-to-rs485-rs485-models)
   - 3.3 [Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#33-option-c--waveshare-rs485-to-ethernetwi-fi-bridge)
4. [Connection Steps](#4-connection-steps)
5. [Sigineer Local API Access](#5-sigineer-local-api-access)
6. [MPG Configuration](#6-mpg-configuration)
7. [Home Assistant Cards](#7-home-assistant-cards)
   - 7.1 [Voltage Card](#71-voltage-card)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Overview

Sigineer Power inverter/chargers expose a Modbus RTU interface via USB, enabling monitoring of battery voltage, output voltage, grid voltage, bus voltage, and other key parameters. This guide covers hardware connection options, API access, and MPG configuration for publishing data to an MQTT broker.

Sigineer's communication interface is straightforward — a USB cable to a monitoring host is all that is required for most deployments.

---

## 2. Supported Models & Protocol Versions

| Protocol Version | Interface | Notes |
| --- | --- | --- |
| `sigineer_v0.11` | USB-A or USB-B | All current Sigineer inverter/charger models |

> If your model does not respond to `sigineer_v0.11`, verify the USB cable supports data (not charge-only) and check that the correct device path is configured.

---

## 3. Hardware Requirements

### 3.1 Option A — USB Cable (Direct)

The simplest and most common approach for Sigineer inverters.

**What you need:**

- USB-A to USB-B cable, **or** USB-A to USB-A cable (depending on inverter model)
- Linux/Raspberry Pi host with an available USB port

---

### 3.2 Option B — Waveshare USB to RS485 (RS485 Models)

If your Sigineer model exposes an RS485 terminal or RJ45 port in addition to or instead of USB, a Waveshare industrial RS485 adapter provides improved isolation and reliability.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, industrial rail case | Single inverter, direct USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge protection, 120Ω termination | Long cable runs, noisy environments |

**Key advantages:**

- Built-in power and signal isolation
- TVS transient voltage suppression
- Wide driver support on Linux, macOS, Raspberry Pi OS, Windows

---

### 3.3 Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For installations where running a USB cable to the monitoring host is not practical.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus/MQTT gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; electrically isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |
| **RS232/485 TO WIFI ETH (B)** | Combined RS232/485; Wi-Fi + Ethernet | Mixed-interface environments |

**Setup:**

1. Connect RS485 A/B wires from inverter to bridge screw terminals
2. Configure IP via VirCom software (Windows); default IP: `192.168.1.200`
3. Set TCP Client mode pointing at your MPG host
4. Set baud rate to 9600

---

## 4. Connection Steps

**Connection:**

1. Power off the Sigineer inverter or ensure it is in a safe maintenance state before connecting
2. Connect the USB cable from the inverter's USB port to your monitoring host
3. Verify the device is detected:

    ``` bash
    dmesg | grep tty
    ```

    Expected output:

    ``` text
    usb X-X: cp210x converter now attached to ttyUSB0
    ```

4. Note the device path (e.g., `/dev/ttyUSB0`) for MPG configuration
5. Proceed to Section 6 for MPG configuration

---

## 5. Sigineer Local API Access

Sigineer inverters running firmware with a network monitoring module may expose a **local web interface** on a LAN IP address. This varies by model — not all Sigineer units include a network monitoring card.

### Checking for a Network Interface

Check whether your Sigineer model includes a plug-in monitoring card (often sold as an optional accessory). If a monitoring card is installed:

1. Check your router's DHCP table for a new device joining the network after the inverter powers on
2. Assign a static IP or DHCP reservation if a monitoring IP appears

### Local Web Interface

``` text
http://<inverter-ip>/
```

The web dashboard typically displays real-time voltage, current, load, and battery parameters.

### JSON Data Endpoint

Use your browser's developer tools (Network tab) while loading the Sigineer web dashboard to identify the specific JSON endpoint and response schema for your firmware version. Endpoint paths are firmware-dependent and not publicly documented.

**Typical response fields:**

``` json
{
  "batteryVoltage": 48.5,
  "outputVoltage": 120.1,
  "gridVoltage": 0.0,
  "busVoltage": 390.2,
  "loadPercent": 35,
  "outputPower": 850,
  "batteryChargeCurrent": 0.0
}
```

> **MPG module note:** A `sigineer_http_v1` protocol module polling the local HTTP JSON endpoint would provide an alternative integration path for Sigineer models with an installed network monitoring card, without requiring a USB cable connection to the monitoring host.

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = sigineer_v0.11
```

> Adjust `port` to match the actual device path on your system. Run `dmesg | grep tty` after connecting to confirm the assigned path.

---

## 7. Home Assistant Cards

The following example card can be added to your Home Assistant dashboard. Adjust entity names to match your configured MQTT topic structure.

### 7.1 Voltage Card

Displays battery voltage, output voltage, bus voltage, and grid voltage as gauges.

![Sigineer Voltage Card](https://github.com/BuxtonCalvin/MultiProtocolGateway/assets/2180145/55900744-6aaf-4b44-bf3e-46976fdffce2)

<details>
<summary>Card YAML Code</summary>

``` yaml
type: horizontal-stack
cards:
  - type: gauge
    needle: false
    name: Battery
    entity: sensor.sigineer_battery_voltage
  - type: gauge
    entity: sensor.sigineer_output_voltage
    name: Output
  - type: gauge
    needle: false
    entity: sensor.sigineer_bus_voltage
    name: Bus
  - type: gauge
    entity: sensor.sigineer_grid_voltage
    name: Grid
    severity:
      green: 110
      yellow: 105
      red: 0
```

</details>

> **Entity name note:** The prefix `sigineer_` must match the `device_name` or topic prefix configured in your MPG setup. Adjust accordingly if you used a different device name.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Device not detected (`/dev/ttyUSB*` missing) | Cable is charge-only (no data lines) | Replace with a known data-capable USB cable; verify with `dmesg` |
| No Modbus response | Baud rate mismatch | Default baud is 9600; verify in MPG config |
| Intermittent connection drops | USB port instability | Try a different USB port on the host; use a powered USB hub |
| Correct port but no data | Protocol version mismatch | Confirm `sigineer_v0.11` is specified exactly in MPG config |
| Entity values look wrong in Home Assistant | MQTT topic mismatch | Verify entity names match your MPG MQTT topic configuration |
