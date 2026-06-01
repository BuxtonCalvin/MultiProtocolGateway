# FoxESS - MQTT Integration Guide

> **Supported Models:** H1 LAN / KH / H3-style Modbus maps
> **Protocol:** FoxESS Modbus TCP or RTU, model dependent
> **Interface:** LAN adapter or AUX/RS485 connection
> **Status:** Initial read-only MPG protocol available

---

## Overview

FoxESS inverters use several model- and firmware-dependent Modbus maps. MPG includes `foxess_h1_lan`, a conservative read-only map for H1 LAN / KH pre-133 / H3 set style holding-register layouts.

## Supported Protocols

| Model Family | Protocol Version | Notes |
| --- | --- | --- |
| H1 LAN / KH pre-133 / H3 set style maps | `foxess_h1_lan` | Verify exact inverter profile before use |

## MPG Configuration

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 247
protocol_version = foxess_h1_lan
```

Use the unit ID expected by your adapter/inverter. Some FoxESS adapters default to a non-1 slave ID.

## Notes

- FoxESS register maps differ by H1, H3, KH, firmware generation, and AUX vs LAN connection.
- The current MPG map is intentionally read-only and avoids remote-control registers.
- If a range causes timeouts, remove that block or create a model-specific protocol variant.

## Sources

- FoxESS Modbus register-map reference: <https://deepwiki.com/nathanmarlor/foxess_modbus/10.3-modbus-register-map>
- FoxESS community register-map notes
- `protocols/foxess/foxess_h1_lan.json`
