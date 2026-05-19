# MultiProtocolGateway — Device Integration Index

> **Project:** MultiProtocolGateway (MPG) — RS485 / CAN / Modbus to MQTT  
> **Repository:** <https://github.com/BuxtonCalvin/MultiProtocolGateway>  
> **Wiki / Config Examples:** <https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki>

---

## Table of Contents

1. [Supported Devices](#supported-devices)
2. [Common Hardware Reference](#common-hardware-reference)
3. [Quick Start](#quick-start)
4. [General Wiring Tips](#general-wiring-tips)
5. [A/B Wire Reversal](#ab-wire-reversal)
6. [Protocol & Interface Comparison](#protocol--interface-comparison)

---

## Supported Devices

### Inverters

| Manufacturer | Key Models | Interface | Protocol | Guide |
| --- | --- | --- | --- | --- |
| [Deye / Sunsynk](Deye_Sunsynk.md) | SUN-xK-SG04LP3, SG05LP3, Sunsynk 5–16kW | RS485 RJ45 / Solarman TCP 8899 | Modbus RTU | [Deye_Sunsynk.md](Deye_Sunsynk.md) |
| [EG4](EG4.md) | 6000XP, 12000XP, 18KPV, 3000EHV | RS485 RJ45 | Modbus RTU | [EG4.md](EG4.md) |
| [Enphase](Enphase.md) | IQ7/IQ8 Microinverters, IQ Battery, IQ Gateway | Ethernet (LAN REST API) | HTTP JSON API | [Enphase.md](Enphase.md) |
| [Fronius](Fronius.md) | Primo GEN24, Symo GEN24, Symo, Primo | Ethernet / RS485 (GEN24) | Modbus TCP / JSON API | [Fronius.md](Fronius.md) |
| [Growatt](Growatt.md) | SPF 5000, 6000, 12000T DVM-US MPV | USB / RS485 | Modbus RTU | [Growatt.md](Growatt.md) |
| [Sigineer](Sigineer.md) | Sigineer inverter/charger series | USB | Modbus RTU | [Sigineer.md](Sigineer.md) |
| [SMA](SMA.md) | Sunny Boy, Sunny Tripower, Sunny Island | Ethernet / RS485 (add-on) | Modbus TCP | [SMA.md](SMA.md) |
| [Sol-Ark](SolArk.md) | 5K, 8K, 12K, 15K-2P | RS485 RJ45 / terminal block | Modbus RTU | [SolArk.md](SolArk.md) |
| [SolarEdge](SolarEdge.md) | HD-Wave SE series, StorEdge, Home Hub | Ethernet / RS485 terminal block | Modbus TCP / RTU (SunSpec) | [SolarEdge.md](SolarEdge.md) |
| [Victron Energy](Victron.md) | MultiPlus, MultiPlus-II, Quattro, Cerbo GX | Ethernet / VE.Bus / VE.Direct | MQTT native (Venus OS) | [Victron.md](Victron.md) |

### Batteries

| Manufacturer | Key Models | Interface | Protocol | Guide |
| --- | --- | --- | --- | --- |
| [AOLithium](AOLithium.md) | AOLithium 48V rack batteries | RS485A RJ45 / CAN RJ45 | Voltronic RS485 / Victron CAN | [AOLithium.md](AOLithium.md) |
| [SOK / PACE BMS](SOK.md) | SOK 48V 100AH, Jakiper 48V 100AH | RS485A RJ45 | PACE BMS Modbus RTU | [SOK.md](SOK.md) |

---

## Common Hardware Reference

### USB to RS485 Adapters (Direct Connection)

| Product | Chipset | Isolation | Protection | Best For |
| --- | --- | --- | --- | --- |
| Generic CH340 USB RS485 | CH340 | ❌ | ❌ | Budget, temporary setups |
| EG4-branded USB RS485 | Varies | ❌ | ❌ | EG4 plug-and-play |
| **DSD TECH SH-U11F** | FTDI | ✅ | TVS, fuse, GND | Sol-Ark, EG4, Deye/Sunsynk — widely endorsed |
| **Waveshare USB TO RS485 (B)** | FT232RNL | ✅ | TVS, fuse | All RS485 devices — recommended for permanent installs |
| **Waveshare USB TO RS485/422** | FT232RNL | ✅ | TVS, 120Ω | Dual-protocol, long cable runs |

### RS485 to Network Serial Bridges (Waveshare)

| Product | Interface | PoE | Isolation | Best For |
| --- | --- | --- | --- | --- |
| **RS485 TO ETH (B)** | Ethernet | ❌ | ✅ | Wired LAN, single inverter |
| **RS485 TO POE ETH (B)** | Ethernet | ✅ | ✅ | Single-cable clean installs |
| **RS485 TO WIFI ETH** | Wi-Fi + Ethernet | ❌ | Partial | Wireless environments |
| **RS232/485 TO WIFI ETH (B)** | Wi-Fi + Ethernet | Optional | ✅ | Mixed RS232/RS485 environments |

> All Waveshare bridges support Modbus TCP gateway mode. Initial IP configuration requires VirCom (Windows). Default IP: `192.168.1.200`.

### CAN Bus Adapters (For Battery BMS)

| Product | Compatibility | Notes |
| --- | --- | --- |
| **Waveshare USB-CAN-A** | CANable-compatible, Linux/macOS/Windows | Open-source firmware; good choice |
| **PEAK PCAN-USB** | Industrial grade, all platforms | Premium option |
| Generic CANable 1.0 | Linux/macOS | Community use, lower cost |

### Victron-Specific Hardware

| Product | Purpose |
| --- | --- |
| **Victron MK3-USB** | VE.Bus (MultiPlus/Quattro) to USB — required, never bypass |
| **Victron VE.Direct to USB** | Per-device cable for MPPT/BMV to USB host |
| **Cerbo GX** | Central GX hub with built-in MQTT; recommended for new Victron installs |

---

## Quick Start

1. Identify your device in the table above and open its guide
2. Select a hardware interface option:
   - **No hardware needed**: Enphase (LAN API), Victron with Cerbo GX already installed
   - **USB cable only**: Growatt SPF, Sigineer
   - **USB + RS485 adapter**: EG4, Deye/Sunsynk, Sol-Ark, SOK, AOLithium, Growatt (RS485 path)
   - **Ethernet only**: SMA, SolarEdge (Modbus TCP), Fronius, Victron (via GX), Enphase
3. Wire per the pinout table in the guide
4. Follow the [Modbus RTU to MQTT configuration example](https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt) for RS485 devices
5. Set `protocol_version` per the device guide
6. Start the gateway and verify MQTT messages are publishing

---

## General Wiring Tips

- **Always use shielded cable** for RS485 runs longer than 3 meters; standard CAT5e/CAT6 is adequate for shorter runs
- **Termination resistors** (120Ω) should be placed at each physical end of an RS485 bus — most quality adapters include built-in termination
- **Electrical isolation** is strongly recommended in high-power environments — use isolated adapters (Waveshare, DSD SH-U11F)
- **Ferrule crimp connectors** dramatically improve reliability at screw terminals compared to bare wire ends
- **DHCP reservation or static IP** for all network-connected devices (SMA, SolarEdge, Fronius, Victron GX, Enphase IQ Gateway) prevents integration breakage when IPs change
- **Long RS485 runs** (>30m): reduce baud to 9600 bps; use shielded twisted-pair; ensure 120Ω termination at both ends

---

## A/B Wire Reversal

Reversed A/B wiring is the single most common RS485 error across all brands. If you see consistent `ModbusIOException` timeouts with no response:

1. **Swap the A and B wires** at the adapter end (not the inverter end)
2. Re-test

This resolves the majority of "no response" issues and should always be the first debugging step. Especially common with Raspberry Pi RS485 HATs (orientation often inverted vs. USB adapters) and custom-crimped RJ45 cables.

---

## Protocol & Interface Comparison

| Brand | RS485 Direct | Modbus TCP (LAN) | Native MQTT | Local REST API |
| --- | --- | --- | --- | --- |
| AOLithium | ✅ RS485 + CAN | ❌ | ❌ | ❌ |
| Deye / Sunsynk | ✅ | ❌ (bridge needed) | ❌ | ❌ |
| EG4 | ✅ | ❌ | ❌ | ❌ |
| Enphase | ❌ | ❌ | ❌ | ✅ JSON (IQ Gateway) |
| Fronius | ✅ (GEN24 only) | ✅ | ❌ | ✅ Solar API |
| Growatt | ✅ USB + RS485 | ❌ | ❌ | ❌ |
| Sigineer | ✅ USB | ❌ | ❌ | ❌ |
| SMA | ❌ (add-on only) | ✅ | ❌ | ❌ |
| Sol-Ark | ✅ | ❌ | ❌ | ❌ |
| SolarEdge | ✅ | ✅ (port 1502) | ❌ | ❌ |
| SOK / PACE BMS | ✅ | ❌ | ❌ | ❌ |
| Victron | ❌ (MK3-USB required) | ✅ (via GX) | ✅ Venus OS | ✅ VRM API |
