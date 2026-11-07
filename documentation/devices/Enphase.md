# Enphase — MQTT Integration Guide

> **Supported Products:** IQ8 Microinverters · IQ7 Microinverters · IQ Battery 3T / 5P / 10T · IQ Combiner 4 / 4C · Enpower Smart Switch · Envoy-S · IQ Gateway
> **Protocol:** Local HTTPS REST API (IQ Gateway) · Enphase Enlighten Cloud API · No native Modbus or RS485
> **Interface:** Ethernet / LAN (IQ Gateway) · Cloud API (Enphase Enlighten)
> **Status:** Well-supported — active local API; active community integrations

---

## Table of Contents

1. [Overview & MPG Compatibility Note](#1-overview--mpg-compatibility-note)
2. [Architecture: How Enphase Communicates](#2-architecture-how-enphase-communicates)
3. [Supported Products & Data Access](#3-supported-products--data-access)
4. [Hardware Requirements](#4-hardware-requirements)
   - 4.1 [No Additional Hardware Required](#41-no-additional-hardware-required)
   - 4.2 [Ethernet vs Wi-Fi for IQ Gateway](#42-ethernet-vs-wi-fi-for-iq-gateway)
5. [IQ Gateway Local API Authentication](#5-iq-gateway-local-api-authentication)
   - 5.1 [Firmware 7.x+ — JWT Token Authentication](#51-firmware-7x--jwt-token-authentication)
   - 5.2 [Older Firmware (Pre-7.x) — Digest Authentication](#52-older-firmware-pre-7x--digest-authentication)
6. [Local API Endpoints](#6-local-api-endpoints)
   - 6.1 [Production & Energy](#61-production--energy)
   - 6.2 [Full Power Flow (Real-Time)](#62-full-power-flow-real-time)
   - 6.3 [Battery / Storage](#63-battery--storage)
   - 6.4 [Per-Microinverter Data](#64-per-microinverter-data)
7. [Enphase Enlighten Cloud API](#7-enphase-enlighten-cloud-api)
8. [MPG Integration Path](#8-mpg-integration-path)
   - 8.1 [Choosing an Approach](#81-choosing-an-approach)
   - 8.2 [Modbus TCP with SunSpec](#82-modbus-tcp-with-sunspec)
   - 8.3 [Local REST API](#83-local-rest-api)
9. [MQTT Bridge Options](#9-mqtt-bridge-options)
10. [Known Limitations](#10-known-limitations)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview & MPG Compatibility Note

> ⚠️ **Important:** Enphase has no RS485 or Modbus RTU interface that third parties can use. The IQ Gateway can serve SunSpec over **Modbus TCP (port 502)**, but it is **not enabled by default** and cannot be turned on from the local UI or the app.

- **Enabling it:** Contact Enphase Support and ask them to enable Modbus TCP on your IQ Gateway.
- **Typical use:** Monitoring production and consumption in third-party systems (Victron VRM, evcc, Home Assistant, MPG). Some integrations also use it to limit PV output.
- **Reliability:** Users report that a firmware update can leave port 502 closed until Enphase re-enables it.

Once Modbus TCP is enabled, MPG can read the gateway with the `enphase_iq_gateway_sunspec` protocol. Without it, MPG reads the IQ Gateway's **local HTTPS REST API** (LAN access) with the `enphase_rest` transport and the `enphase_iq_gateway_rest` protocol, covering microinverter production, battery state and power, grid import/export and home consumption without any additional hardware beyond a LAN connection to the IQ Gateway. See [Section 8](#8-mpg-integration-path). The **Enphase Enlighten Cloud API** is not supported.

---

## 2. Architecture: How Enphase Communicates

Enphase uses microinverters (one per solar panel) that communicate over the **AC power line** (PLC — Power Line Communication). All data flows to the central **IQ Gateway** (formerly Envoy-S), which aggregates microinverter data and exposes it via a local web interface and REST API.

``` text
IQ8 Microinverters (per panel)
     │ (AC power line communication — PLC)
     ▼
IQ Gateway (Envoy-S)
     │ Ethernet / Wi-Fi
     ├── Local HTTPS REST API  ──►  Monitoring host / MPG module / MQTT bridge
     └── Enphase Enlighten Cloud  ──►  Cloud API / Mobile App
```

**IQ Gateway roles:**

- Aggregates production data from all microinverters over AC PLC
- Monitors IQ Battery state-of-charge, charge/discharge power
- Communicates with Enpower Smart Switch for grid disconnect control
- Exposes a local HTTPS REST API for home automation and monitoring
- Reports to Enphase Enlighten cloud for remote access and app connectivity

---

## 3. Supported Products & Data Access

| Product | Local API | Cloud API | Per-Device Data | Notes |
| --- | --- | --- | --- | --- |
| IQ8 Microinverters | Via IQ Gateway | Via Enlighten | ✅ Per-panel (delayed) | 5–15 min polling interval per panel |
| IQ7 Microinverters | Via IQ Gateway | Via Enlighten | ✅ Per-panel (delayed) | Same as IQ8 |
| IQ Battery 3T / 5P / 10T | Via IQ Gateway | Via Enlighten | ✅ SOC, power, state | Real-time via livedata endpoint |
| IQ Combiner 4 / 4C | Houses IQ Gateway | N/A | N/A | The Gateway is inside the Combiner |
| Enpower Smart Switch | Via IQ Gateway | Via Enlighten | ✅ Grid relay state | Mode control available via API |
| IQ Gateway (Envoy-S) | ✅ LAN REST API | ✅ | Aggregated system | Primary integration target |

---

## 4. Hardware Requirements

### 4.1 No Additional Hardware Required

Enphase integration requires no RS485 adapters, serial bridges, or special hardware. The IQ Gateway communicates over standard Ethernet or Wi-Fi.

**Prerequisites:**

- IQ Gateway connected to your LAN (Ethernet recommended)
- IQ Gateway IP address (find via router DHCP table or Enphase Enlighten app under Devices)
- Enphase account credentials (required for firmware 7.x+ JWT token generation)

### 4.2 Ethernet vs Wi-Fi for IQ Gateway

**Ethernet is strongly recommended** over Wi-Fi for local API reliability.

- Wi-Fi connections to the IQ Gateway can be intermittent, especially while the Gateway is also managing microinverter PLC communication
- Use a CAT5e/CAT6 patch cable from the IQ Gateway's Ethernet port to a nearby LAN switch
- Assign a **static IP or DHCP reservation** to the IQ Gateway — the local API URL must be a stable address

---

## 5. IQ Gateway Local API Authentication

### 5.1 Firmware 7.x+ — JWT Token Authentication

Firmware 7.x changed the authentication model from simple username/password to **JWT token-based** auth. This is the most common source of integration failures on systems updated after mid-2023.

**Step 1 — Obtain a session ID from Enphase Enlighten:**

``` bash
curl -s \
  -d "user[email]=YOUR_EMAIL&user[password]=YOUR_PASSWORD" \
  "https://enlighten.enphaseenergy.com/login/login.json" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])"
```

**Step 2 — Exchange session ID for a local JWT token:**

``` bash
curl -s \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"SESSION_ID\",\"serial_num\":\"ENVOY_SERIAL\",\"username\":\"YOUR_EMAIL\"}" \
  "https://entrez.enphaseenergy.com/tokens"
```

The response body **is** the JWT token (a long string starting with `eyJ...`). Store it securely — it is valid for **one year**.

**Step 3 — Use the JWT token on all local API calls:**

``` bash
curl -k "https://192.168.1.xxx/api/v1/production" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

> The IQ Gateway uses a **self-signed TLS certificate**. Use `-k` in curl, or configure your HTTP client to skip certificate verification for this host.

**Finding the Envoy serial number:**

- Printed on the label on the IQ Gateway / Envoy-S unit
- Available in the Enlighten app under Devices → IQ Gateway → Serial Number

### 5.2 Older Firmware (Pre-7.x) — Digest Authentication

For inverters running firmware versions prior to 7.x:

``` bash
curl --digest \
  -u "envoy:LAST6DIGITS_OF_SERIAL" \
  "http://192.168.1.xxx/api/v1/production"
```

Username is always `envoy`. Password is the last 6 digits of the Envoy/IQ Gateway serial number.

> Note: Pre-7.x firmware used HTTP (not HTTPS) for the local API.

---

## 6. Local API Endpoints

All endpoints below use HTTPS for firmware 7.x+ (`https://<gateway-ip>/`) and require the JWT Bearer token. For pre-7.x firmware, use HTTP and Digest auth.

### 6.1 Production & Energy

**Endpoint:**

``` text
GET /api/v1/production
```

**Response:**

``` json
{
  "wattHoursToday":    8456,
  "wattHoursSevenDays": 45230,
  "wattHoursLifetime": 3456789,
  "wattsNow":          3420
}
```

**Extended production + consumption:**

``` text
GET /ivp/meters/readings
```

**Response (abbreviated):**

``` json
{
  "0": {
    "eid": 704643584,
    "timestamp": 1700000000,
    "actPower": 3420.5,
    "apparentPower": 3502.1,
    "activePower": 3420.5,
    "voltage": 241.3,
    "current": 14.2,
    "freq": 60.0
  }
}
```

Returns one object per configured meter (production CT, consumption CT, storage).

---

### 6.2 Full Power Flow (Real-Time)

This is the **recommended primary endpoint** for a MPG module — it returns a complete real-time snapshot of all power flows in a single request.

**Endpoint:**

``` text
GET /ivp/livedata/status
```

**Response (abbreviated):**

``` json
{
  "connection": {"mqtt_state": "connected", "prov_state": "configured", "sc_stream": "enabled"},
  "meters": {
    "last_update": 1700000000,
    "soc": 85,
    "pv":      {"agg_p_mw": 3420500},
    "storage": {"agg_p_mw": -500000},
    "grid":    {"agg_p_mw": -1250500},
    "load":    {"agg_p_mw": 1670000}
  }
}
```

> **Field names:** earlier revisions of this page showed a nested `meters.pv.agg.p.avg` shape in watts. Firmware 7.x integrations (for example `pyenphase`, used by Home Assistant) read the flat `agg_p_mw` keys above, which are **milliwatts**, and a top-level `meters.soc`. The shipped protocol map follows the flat shape. Other fields, such as per-phase values, vary with firmware and site configuration; fetch the endpoint with `curl -k` and adjust the map to what your gateway sends.
> **Sign convention:** the gateway's sign convention for grid and battery power has not been confirmed for this guide. Compare against the Enphase app before building dashboards on the values.
> **Live stream:** values on this endpoint are only refreshed while the gateway's live-data stream is on (`connection.sc_stream` is `enabled`). MPG does not switch it on; the `live_stream_state` variable shows the state. If it reads `disabled` the power values will be stale or missing.

**Polling frequency:** This endpoint reflects near-real-time conditions. Polling every 10–30 seconds is appropriate.

---

### 6.3 Battery / Storage

**Battery power and SOC:**

``` text
GET /ivp/ensemble/power
```

**Response:**

``` json
{
  "devices:": [
    {
      "serial_num": "122112345678",
      "dc_switch_open": false,
      "encharge_revision": 2,
      "encharge_capacity": 3360,
      "encharge_rated_power": 1280,
      "temperature": 28,
      "soc": 85,
      "max_available_capacity": 2856,
      "real_power_mw": -500000,
      "apparent_power_mva": 502000
    }
  ]
}
```

**Battery inventory (serial numbers, count, status):**

``` text
GET /ivp/ensemble/inventory
```

**Enpower relay state (grid connect/disconnect):**

``` text
GET /ivp/ensemble/dry_contacts
```

---

### 6.4 Per-Microinverter Data

**Endpoint:**

``` text
GET /api/v1/production/inverters
```

**Response:**

``` json
[
  {
    "serialNumber": "482112345678",
    "lastReportDate": 1700000000,
    "lastReportWatts": 285,
    "maxReportWatts": 349
  },
  ...
]
```

Returns one object per microinverter with serial number, last report timestamp, and last reported power.

> **Polling interval note:** Per-microinverter data updates on the IQ Gateway's PLC polling cycle — typically every 5–15 minutes, not in real time. Use `/ivp/livedata/status` for near-real-time totals. Do not poll `/api/v1/production/inverters` more frequently than every 5 minutes.

---

## 7. Enphase Enlighten Cloud API

For cloud-based access, Enphase provides a REST API with an API key issued per Enlighten account.

**Base URL:**

``` text
https://api.enphaseenergy.com/api/v4/
```

**Authentication:**

``` text
?key=YOUR_API_KEY&access_token=OAUTH_TOKEN
```

OAuth 2.0 tokens are obtained via the Enlighten developer portal. API keys are issued per application registration.

**Key Cloud Endpoints:**

| Endpoint | Description |
| --- | --- |
| `GET /systems/{systemId}/summary` | Site-level production summary |
| `GET /systems/{systemId}/rgm_stats` | Revenue-grade production data (15-min intervals) |
| `GET /systems/{systemId}/battery_summary` | Battery aggregated data |
| `GET /systems/{systemId}/inverters_summary` | Per-microinverter summary |
| `GET /systems/{systemId}/consumption_stats` | Consumption data (if CT installed) |

> **Rate limit:** The Enlighten API is rate-limited to 10 requests per minute per key and 10,000 per month on the free developer tier. For real-time monitoring, the **local IQ Gateway API is strongly preferred** over the cloud API.

---

## 8. MPG Integration Path

MPG has two ways to read an IQ Gateway. They use different transports and protocols, can be run side by side as separate `[transport.*]` sections, and cover different data.

### 8.1 Choosing an Approach

| | Modbus TCP with SunSpec | Local REST API |
| --- | --- | --- |
| Transport | `modbus_tcp` | `enphase_rest` |
| Protocol | `enphase_iq_gateway_sunspec` | `enphase_iq_gateway_rest` |
| Port | 502 | 443 (80 on pre-7.x firmware) |
| Authentication | none | JWT token (7.x+) or digest (older) |
| Availability | Off by default; must be enabled on the gateway | Always present on the LAN |
| Site production | Yes, aggregate (SunSpec inverter model 103) | Yes (`/api/v1/production`, `/ivp/livedata/status`) |
| Grid / consumption | Production CT meter only (SunSpec meter models 201/203) | Grid and load power from `/ivp/livedata/status` |
| Batteries | No | Yes (`/ivp/ensemble/power`, `/ivp/livedata/status`) |
| Per-microinverter | No (aggregate only) | Yes, delayed 5-15 minutes (`/api/v1/production/inverters`) |
| Write / control | Optional PV power limit through DER model 704 (unverified) | None; read-only |
| Maintenance | None | Token renewal once a year |

**Role of each.** SunSpec Modbus is a standards-based, unauthenticated, read-mostly feed of aggregate production and the production meter, and the only route to power-limit control. Use it where it is enabled and aggregate production is enough. The REST API is the route to anything else the gateway knows: batteries, consumption, per-inverter detail. Use it when Modbus TCP is not enabled or when you need that data. Running both gives SunSpec for the metering and control path and REST for the extras, at the cost of two device records.

### 8.2 Modbus TCP with SunSpec

The IQ Gateway acts as a Modbus TCP **server** (port 502, unit ID 1 by default) and MPG connects to it as the client through the standard `modbus_tcp` transport. Nothing needs installing on the gateway except enabling the service.

**1. Enable Modbus TCP on the gateway.** The service is off by default. The comments in the protocol map say it is enabled from the Installer App under Site Communication; [Section 1](#1-overview--mpg-compatibility-note) reports that it cannot be switched on from the local UI or the app and has to be requested from Enphase Support. Which applies depends on firmware and on installer access, so try the first and fall back to the second. A firmware update can leave port 502 closed until it is re-enabled.

**2. Check the port.** From the machine running MPG, `nc -vz 192.168.1.50 502` should connect. A refused connection means the service is still off.

**3. Add the transport:**

``` ini
[transport.enphase_sunspec]
transport = modbus_tcp
protocol_version = enphase_iq_gateway_sunspec
host = 192.168.1.50
port = 502
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = Enphase
device_model = IQ Gateway
```

The settings are those of [Modbus TCP](../usage/transports.md#modbus-tcp). `address` is the Modbus unit ID; the map's comments give 1. Use a static IP or DHCP reservation. The number of simultaneous Modbus clients the gateway accepts is not documented here; if another integration (Victron, evcc, Home Assistant) already reads the same gateway and either one drops out, stop one of them.

**What the map reads** (`protocols/enphase/enphase_iq_gateway_sunspec`):

| Block | Variables | Notes |
| --- | --- | --- |
| SunSpec common model (model 1) | `manufacturer`, `model`, `options`, `version`, `serial_number`, `device_address` | Fixed addresses from 40000; `sunspec_id` should read the SunSpec marker, `0x53756E53` ("SunS"). |
| Inverter model (101 or 103) | `ac_power`, `ac_current_*`, `ac_voltage_*`, `ac_frequency`, `ac_energy_total`, DC values, `inverter_temperature`, `operating_state`, `alarm_1`, `alarm_2` | Aggregate production for the whole site. Scaled by the matching `*_sf` rows. |
| Meter model (201 or 203) | `meter_ac_power`, `meter_ac_current`, `meter_ac_voltage_*`, `meter_ac_frequency`, `meter_energy_export`, `meter_energy_import` | Production CT. Dropped if the gateway reports no meter model. |
| DER model 704 | `der_control_did`, `der_power_limit_pct`, `der_power_limit_enabled` | The only writable rows. Dropped if the gateway has no model 704. |

The gateway does **not** expose per-microinverter registers or battery data over Modbus; use the REST API for those.

**Discovery.** The inverter, meter and 704 rows are written relative to anchors (`[inverter_1_base+14]`). On connect MPG reads the SunSpec model chain from address 40000 and anchors each block where the gateway says it is, so a meter that does not directly follow the inverter block is still found. If discovery returns nothing, the defaults in the protocol JSON apply (inverter at 40069, meter at 40121, model 704 at 704). See [Dynamic Register Discovery](../architecture/dynamic_register_discovery.md).

**Scale factors.** SunSpec values are integers with a separate scale-factor register, which the map reads as `*_sf` rows. Values with a `×SF` unit are scaled by 10 to the power of that register. Not-implemented points read as `0x8000` (int16), `0xFFFF` (uint16) or `0xFFFFFFFF` (acc32).

**Known gaps in the map.**

- Addresses are the Modbus wire address, one less than the vendor's printed register number. The map's header comments explain the shift and which rows were corrected from the SunSpec standard rather than from Enphase documentation.
- The DER 704 offsets are unverified against a gateway. Treat power-limit control as experimental and leave `write_enabled` off until you have read the rows on your own gateway and confirmed them. See [Write Safety System](../usage/transports.md#write-safety-system).
- The map comments mention consumption metering as models 211/213 while the protocol JSON recognizes meter models 201 and 203. If the meter rows are missing on a site with a consumption CT, read the model chain and check which ID the gateway reports.

### 8.3 Local REST API

The REST API is read with the [`enphase_rest`](../usage/transports.md#enphase-rest) transport and the `protocols/enphase/enphase_iq_gateway_rest` protocol:

``` ini
[transport.enphase]
transport = enphase_rest
protocol_version = enphase_iq_gateway_rest
host = 192.168.1.50
token = YOUR_JWT_TOKEN
read_interval = 15
bridge = transport.mqtt
```

Port 443, HTTPS, bearer authentication and no certificate verification are the defaults. For pre-7.x firmware use `use_https = false` and `auth_type = digest`. All settings are listed in the [transport reference](../usage/transports.md#enphase-rest).

The protocol is a [text-keyed](../usage/protocols.md#text-keys-labels-and-obis-codes) map: each row's `register` cell is an endpoint and a JSON path, for example `/ivp/livedata/status#meters.pv.agg_p_mw`. The rows shipped are:

| Endpoint | Variables | Poll |
| --- | --- | --- |
| `/api/v1/production` | `production_power`, `energy_today`, `energy_seven_days`, `energy_lifetime` | every cycle |
| `/ivp/livedata/status` | `pv_power`, `grid_power`, `load_power`, `battery_power`, `battery_soc`, `live_stream_state`, `gateway_mqtt_state` | every cycle |
| `/ivp/ensemble/power` | `battery_count`, `battery_temperature_max`, `battery_capacity` | every cycle |
| `/api/v1/production/inverters` | `inverter_count`, `inverter_power_total` | every 300 s |

The `agg_p_mw` rows apply a `0.001W` unit so they are delivered in watts. The `/ivp/livedata/status` and `/api/v1/production` rows use field names seen in other integrations for firmware 7.x. The `/ivp/ensemble/power` and inverter rows follow the descriptions in this guide and have not been checked against a gateway: in particular the battery list key is written `devices:` (with a colon) in Section 6.3, and the rows must match what your gateway sends. A row whose field is absent is left out and logged once, so a site without a battery simply has no battery variables. Fields that are not in the map, such as `/ivp/meters/readings`, can be added as rows; meter order in that list depends on the site, so check it before relying on an index.

**Token refresh:** The JWT token is valid for one year. Set `enlighten_username` and `enlighten_password` and `enphase_rest` fetches a token through `https://entrez.enphaseenergy.com/tokens` when none is set, renews it `token_refresh_days` (default 14) before it expires, and renews it once if the gateway answers `401`. The renewed token is kept in memory only, so after a restart a new one is fetched. Without the credentials the token in `token` or `token_file` is used until it expires, and the transport refuses to connect once it has.

---

## 9. MQTT Bridge Options

For existing Home Assistant users, the official Enphase Envoy integration is the simplest path. For a standalone MQTT bridge or MPG integration, use the local API directly.

**Home Assistant — Official Enphase Envoy Integration:**

1. Navigate to Settings → Integrations → Add Integration → search **Enphase Envoy**
2. Enter the IQ Gateway's IP address
3. Enter your Enphase account credentials
4. The integration handles JWT token retrieval and annual refresh automatically

Entities created include: solar production (W, Wh), grid consumption (W), net grid import/export, battery SOC and power, and individual microinverter power.

**Python polling script (standalone):**

``` python
import requests
import json

GATEWAY_IP = "192.168.1.xxx"
JWT_TOKEN  = "YOUR_JWT_TOKEN"

headers = {"Authorization": f"Bearer {JWT_TOKEN}"}

response = requests.get(
    f"https://{GATEWAY_IP}/ivp/livedata/status",
    headers=headers,
    verify=False      # self-signed cert
)

data = response.json()
pv_watts   = data["meters"]["pv"]["agg"]["p"]["avg"]
grid_watts = data["meters"]["grid"]["agg"]["p"]["avg"]
batt_soc   = data["meters"]["storage"]["agg"]["soc"]["avg"]

print(f"PV: {pv_watts}W  Grid: {grid_watts}W  Battery SOC: {batt_soc}%")
```

---

## 10. Known Limitations

| Limitation | Description | Workaround |
| --- | --- | --- |
| No Modbus RS485 | Enphase exposes no wired serial interface | Use local HTTPS REST API exclusively |
| JWT auth required (firmware 7.x+) | Tokens expire annually; renewal requires Enphase credentials | Store credentials securely; implement annual token refresh |
| HTTPS with self-signed cert | All local API calls require HTTPS (firmware 7.x+) | Use `-k` flag in curl; disable cert verification in integration |
| Single local session limit | IQ Gateway may throttle or reject rapid-fire connections | Limit polling to one request per 10 seconds |
| Per-microinverter data delays | Individual panel data lags 5–15 minutes | Use `/ivp/livedata/status` for near-real-time totals |
| Live-data stream | `/ivp/livedata/status` is only fresh while the gateway's live stream is on | Check `live_stream_state`; MPG does not enable the stream |
| No local write access to inverter settings | Local API is read-only except for Enpower relay and battery mode | Use Enlighten app or cloud API for settings changes |
| Cloud API rate limit | 10 req/min, 10,000 req/month on free tier | Use local API for real-time monitoring; cloud API for historical data only |

---

## 11. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| `401 Unauthorized` on all endpoints | Wrong or expired JWT token | Re-generate token per Section 5.1 |
| `Connection refused` or timeout | Wrong IP or Gateway not on LAN | Verify IP via Enlighten app; use Ethernet not Wi-Fi |
| SSL/TLS certificate error | Self-signed cert rejection | Use `-k` in curl; set `verify=False` in Python requests; configure integration to skip TLS verification |
| Pre-7.x password rejected | Gateway updated to firmware 7.x | Migrate to JWT token auth per Section 5.1 |
| Data stops updating | IQ Gateway lost LAN connection | Check Ethernet cable; assign static IP; check router DHCP lease |
| Battery sensors missing from API | Battery not commissioned | Check IQ Gateway web UI for battery enrollment and status |
| Per-inverter data always stale | Large array; slow PLC polling cycle | Expected — do not poll `/production/inverters` more than once per 5 minutes |
| Variable missing from the output | The field is not in that gateway's response (no battery, no consumption CT) or the map key does not match | The log reports each missing field once at INFO; compare the row's `register` cell with `curl -k -H "Authorization: Bearer $TOKEN" https://<ip>/<endpoint>` |
| `wattsNow` returns 0 at night | Normal — no production | Use `wattHoursToday` to confirm data is flowing; 0W production at night is correct |
