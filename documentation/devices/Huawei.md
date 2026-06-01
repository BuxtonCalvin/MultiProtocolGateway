# Huawei SUN2000 - MQTT Integration Guide

> **Supported Models:** SUN2000 residential and commercial inverters with Modbus enabled
> **Protocol:** Huawei Modbus over TCP or RS485
> **Interface:** Smart Dongle, SmartLogger, or RS485
> **Status:** Initial read-only MPG protocol available

---

## Overview

Huawei SUN2000 inverters expose operating state, alarms, PV string data, AC power, grid values, temperature, and energy counters over Modbus. MPG includes an initial read-only `huawei_sun2000` map based on public Huawei SUN2000 Modbus interface definitions.

Huawei publishes several register-definition variants by inverter family, including SUN2000MA, SUN2000MB, SUN2000MC, and SUN2000ME. Validate the exact register table against the target inverter before enabling any write behavior.

## Supported Protocols

| Model Family | Protocol Version | Transport |
| --- | --- | --- |
| SUN2000 residential / commercial family | `huawei_sun2000` | Modbus TCP or RTU |

## MPG Configuration

### Modbus TCP

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = huawei_sun2000
```

### Modbus RTU

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = huawei_sun2000
```

## Notes

- Enable Modbus in FusionSolar / SmartLogger / dongle settings.
- Cascaded inverters may use different unit IDs behind the same dongle.
- The current MPG map is read-only and intentionally omits power-control registers.

## Sources

- Photomate SUN2000 Modbus definition list: <https://photomate.zendesk.com/hc/cs/articles/4573250566941-SUN2000-Inverter-MODBUS-definition-list>
- Huawei SUN2000 public Modbus definition excerpts
- `protocols/huawei/huawei_sun2000.json`
