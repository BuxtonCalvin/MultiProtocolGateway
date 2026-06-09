# Sigenergy SigenStor / Sigen Hybrid - MQTT Integration Guide

> **Supported Models:** SigenStor EC SP/TP, Sigen Hybrid SP/TP, Sigen PV Max SP/TP and related Sigenergy systems
> **Protocol:** Sigenergy Modbus over TCP or RS485
> **Interface:** Ethernet/WLAN/RS485, model and commissioning dependent
> **Status:** Initial read-only MPG protocols available

---

## Overview

Sigenergy systems expose plant, inverter, PV, battery, grid, alarm, and optional DC charger telemetry through the Sigenergy Modbus protocol. MPG includes two initial read-only maps:

- `sigenergy_plant` for plant-level data read from slave/unit address `247`
- `sigenergy_hybrid` for individual Sigen Hybrid / SigenStor EC device data read from slave/unit addresses `1-246`

The maps intentionally omit remote EMS and write/control registers. Use them for monitoring first, then validate against a live scan before considering any write behavior.

## Supported Protocols

| Model Family | Protocol Version | Transport | Unit ID |
| --- | --- | --- | --- |
| SigenStor EC plant view | `sigenergy_plant` | Modbus TCP or RTU | `247` |
| Sigen Hybrid SP/TP | `sigenergy_hybrid` | Modbus TCP or RTU | `1-246` |
| Sigen PV Max SP/TP | `sigenergy_hybrid` | Modbus TCP or RTU | `1-246` |

## Modbus Setup

### Modbus TCP

Sigenergy's TCP server uses port `502`. Enable Modbus TCP in the Sigenergy installer/app settings before connecting MPG.

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 247
protocol_version = sigenergy_plant
```

For a specific hybrid inverter or SigenStor EC device, use the device slave address instead:

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = sigenergy_hybrid
```

### Modbus RTU

Default RTU settings from the Sigenergy Modbus protocol are `9600` baud, 8 data bits, no parity, and 1 stop bit.

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 247
protocol_version = sigenergy_plant
```

Use `sigenergy_hybrid` with the individual device unit ID for inverter-level telemetry.

## Wiring Notes

For RS485, use a shielded twisted pair and confirm the exact connector pinout for the installed gateway/controller. Public integration notes identify RS485 pin 15 as `RS485 1_A+` and pin 16 as `RS485 1_B-` on supported SigenStor connections.

``` text
RS485 adapter A+  ->  Sigenergy RS485 A+
RS485 adapter B-  ->  Sigenergy RS485 B-
GND/shield        ->  Ground/shield, if specified by the installation manual
```

If MPG reports consistent Modbus timeouts, verify the unit ID and swap A/B at the adapter end.

## Enabling Modbus

Modbus access may be disabled by default. Public integration guides note that Remote EMS scheduling and Modbus TCP server options must be enabled in the Sigenergy commissioning app/settings before third-party polling works.

Keep Modbus TCP on a trusted local network. Do not expose port `502` to the internet.

## Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Plant data does not respond | Wrong unit ID | Use unit ID `247` with `sigenergy_plant` |
| Inverter data does not respond | Wrong protocol or unit ID | Use `sigenergy_hybrid` with device unit `1-246` |
| TCP connection refused | Modbus TCP disabled or wrong host | Enable Modbus TCP and verify the inverter/gateway IP |
| RTU timeouts | A/B reversed, wrong baud, or wrong unit | Use 9600 8N1 and verify RS485 wiring |
| Register values appear shifted | Address-base mismatch or firmware variant | Run a live scan and adjust the map for that firmware if needed |

## Sources

- Sigenergy Modbus Protocol V1.7, release 2024-04-09: <https://pdf.tritec.info/pdf/produkte/Sigenergy_Modbus_Protocol_20240409_EN.pdf>
- Sunergy Sigenergy integration notes: <https://docs.dashboard.sunergy.nl/brands/sigenergy/sigstor.html>
- Eniris SmartgridOne Sigenergy notes: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Sigenergy>
- `protocols/sigenergy/sigenergy_plant.json`
- `protocols/sigenergy/sigenergy_hybrid.json`
