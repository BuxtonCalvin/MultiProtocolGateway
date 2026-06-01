# Solis / Ginlong - MQTT Integration Guide

> **Supported Models:** Solis string and hybrid inverters with Modbus enabled
> **Protocol:** Solis Modbus TCP or RTU
> **Interface:** S2-WL-ST logger, RS485, or model-specific Modbus interface
> **Status:** Initial read-only MPG protocols available

---

## Overview

Solis inverters expose telemetry through Modbus register blocks. MPG includes separate maps for string and hybrid inverter families because their register ranges differ substantially.

## Supported Protocols

| Model Family | Protocol Version | Notes |
| --- | --- | --- |
| Solis string inverters | `solis_string` | 2xxx, 3xxx, and 36xxx style ranges |
| Solis hybrid inverters | `solis_hybrid` | 33xxx, 34xxx, 35xxx, 43xxx, and 90xxx style ranges |

## MPG Configuration

### Modbus TCP Logger

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = solis_hybrid
```

Use `solis_string` instead for string-only inverters.

### RS485

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = solis_string
```

## Notes

- The Solis S2-WL-ST logger supports local Modbus TCP reads.
- Solis write/control registers are firmware and grid-code sensitive; MPG maps them read-only for now.
- If values are shifted or missing, confirm the generation-specific Ginlong/Solis protocol document.

## Sources

- Solis Modbus documentation: <https://solis-modbus.readthedocs.io/en/latest/sensors.html>
- Solis S2-WL-ST Modbus TCP guide: <https://usservice.solisinverters.com/support/solutions/articles/73000670200-modbus-tcp-communication-guide-for-s2-wl-st-wi-fi-lan-logger>
- `protocols/solis/solis_string.json`
- `protocols/solis/solis_hybrid.json`
