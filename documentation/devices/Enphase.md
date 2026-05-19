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
9. [MQTT Bridge Options](#9-mqtt-bridge-options)
10. [Known Limitations](#10-known-limitations)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview & MPG Compatibility Note

> ⚠️ **Important:** Enphase does **not** use RS485, Modbus RTU, or Modbus TCP. There is no wired serial interface accessible to third parties. MPG cannot connect to an Enphase system via its standard Modbus transports.

All monitoring integration with Enphase is through the **IQ Gateway's local HTTPS REST API** (LAN access) or the **Enphase Enlighten Cloud API**. A custom MPG protocol module polling the local IQ Gateway API would be required to bring Enphase data into the MPG/MQTT pipeline.

> **MPG module note:** A `enphase_iq_gateway_v1` protocol module polling `/ivp/livedata/status` on the local IQ Gateway would provide comprehensive real-time data for all Enphase systems — microinverter production, battery SOC and power, grid import/export, and home consumption — without any additional hardware beyond a LAN connection to the IQ Gateway.

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
  "connection": {"mqtt_state": "connected", "prov_state": "configured"},
  "meters": {
    "pv": {
      "agg": {
        "p": {"avg": 3420.5, "min": 3415.0, "max": 3426.0},
        "q": {"avg": 82.3},
        "v": {"avg": 241.3},
        "i": {"avg": 14.19}
      }
    },
    "storage": {
      "agg": {
        "p":   {"avg": -500.0},
        "soc": {"avg": 85.4}
      }
    },
    "grid": {
      "agg": {
        "p": {"avg": -1250.5}
      }
    },
    "load": {
      "agg": {
        "p": {"avg": 1670.0}
      }
    }
  }
}
```

> **Sign convention:** Negative `grid.p` = exporting to grid. Negative `storage.p` = battery discharging.

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

Since Enphase does not support Modbus, a dedicated HTTP protocol module is required for MPG integration.

**Proposed module parameters:**

``` ini
transport = http
protocol_version = enphase_iq_gateway_v1
host = 192.168.1.xxx
port = 443
tls = true
tls_verify = false
auth_type = bearer
token = YOUR_JWT_TOKEN
poll_endpoint = /ivp/livedata/status
poll_interval = 15
```

**Data fields available from `/ivp/livedata/status`:**

| Field Path | Description | Unit |
| --- | --- | --- |
| `meters.pv.agg.p.avg` | Solar PV production | W |
| `meters.grid.agg.p.avg` | Grid power (negative = export) | W |
| `meters.load.agg.p.avg` | Home consumption | W |
| `meters.storage.agg.p.avg` | Battery power (negative = discharge) | W |
| `meters.storage.agg.soc.avg` | Battery state of charge | % |
| `meters.pv.agg.v.avg` | AC voltage | V |
| `meters.pv.agg.i.avg` | AC current | A |

**Token refresh:** The JWT token is valid for one year. Implement a token refresh mechanism that re-authenticates via `https://entrez.enphaseenergy.com/tokens` before expiry.

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
| `wattsNow` returns 0 at night | Normal — no production | Use `wattHoursToday` to confirm data is flowing; 0W production at night is correct |
