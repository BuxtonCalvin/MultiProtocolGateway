# SolaX - MQTT Integration Guide

> **Supported Models:** X1/X3 Hybrid G4-style energy storage inverters
> **Protocol:** SolaX Modbus TCP or RTU
> **Interface:** WiFi/LAN dongle or RS485 COM port
> **Status:** Initial read-only MPG protocol available

---

## Overview

SolaX hybrid inverters expose PV, grid, battery, feed-in, and energy counters through Modbus. MPG includes an initial `solax_hybrid_g4` map for common X1/X3 Hybrid G4-style telemetry.

## Supported Protocols

| Model Family | Protocol Version | Transport |
| --- | --- | --- |
| X1/X3 Hybrid G4 / related energy-storage inverters | `solax_hybrid_g4` | Modbus TCP or RTU |

## MPG Configuration

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = solax_hybrid_g4
```

For RS485:

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = solax_hybrid_g4
```

## Notes

- Some SolaX generations use different maps. Confirm against the V3.21 protocol or the exact inverter manual.
- The current MPG map uses input registers and is read-only.
- Enable local Modbus TCP on supported WiFi/LAN dongles before polling.

## Sources

- SolaX Modbus TCP/RTU protocol V3.21 reference: <https://manuals.plus/m/25f44e0b15f5ce7d4a314c1aa9a75b39e759c63363a3c73090868682fff859bd>
- SolaX Modbus TCP knowledge base: <https://kb.solaxpower.com/solution/detail/ff8080818407e2a701840a22dec20032>
- `protocols/solax/solax_hybrid_g4.json`
