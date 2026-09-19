# InfluxDB Metrics Edit — Editing or Deleting Historical Metric Values

## Edit Overview

The **InfluxDB → Metrics Edit 1.x** / **Metrics Edit 3.x** admin screens let an administrator correct or remove specific metric *values* — for one device, over a chosen date/time range — on either an InfluxDB v1 (`influxdb_out`) or InfluxDB v3 (`influxdb3_out`) bridge. Both nav items lead to the same screen; which one(s) appear depends on which InfluxDB bridge version(s) are actually configured and connected.

This is the InfluxDB counterpart of [TimescaleDB's Metrics Edit screen](../TimeScaleDB/timescaledb.md#45-metrics-edit--editing-or-deleting-historical-metric-values), built for the same purpose — fixing a bad sensor reading, clearing data captured during a known test or outage — but InfluxDB v1 and v3 use fundamentally different query engines (InfluxQL vs. SQL/DataFusion) and neither one supports a SQL-style `UPDATE`, so "editing" a value works differently here than it does against TimescaleDB. **Read the "How Editing and Deleting Actually Work" section below before using this screen** — the two actions don't behave the way they would against a relational database, and the differences matter.

## Using the Metrics Edit Screen

1. Open **InfluxDB → Metrics Edit 1.x** or **Metrics Edit 3.x** from the admin menu (only the version(s) with a connected bridge are shown).
2. Pick a **measurement** on the left.
3. Pick the **device** whose data you want to edit.
4. Pick the **field** to target — exactly one field per edit; see "Why only one field at a time" below.
5. Pick a **start** and **end** date/time for the range to affect.
6. Choose an **action**:
   - **Delete value(s)** — see the version-specific warning below; both versions remove the *entire point/row*, not just the selected field.
   - **Set value** — overwrites the selected field with a replacement value you enter.
7. Click **Preview** to see how many points/rows match and a sample of their current values before changing anything.
8. Click **Add to Staged Changes**.
9. Use the existing **Commit All Changes** button in the header to apply every staged edit — InfluxDB edits, TimescaleDB Metrics Edit changes, and TimescaleDB column deletions are all applied together by that one button. Staged edits can be reviewed and individually removed before committing, or abandoned entirely with **Discard Changes**.

## Why Only One Field at a Time

Unlike a wide TimescaleDB table, an InfluxDB measurement has no single declared schema an admin edits column-by-column — every field is looked up independently, and a **Set value** edit only ever makes sense applied to one field's own type. Letting an admin pick several fields of different types (say, one integer field and one boolean field) for a single replacement value would produce an ambiguous, easy-to-misuse result, so the Field picker is a single dropdown rather than a checklist — the same design TimescaleDB's Metrics Edit screen uses for the same reason.

## How Editing and Deleting Actually Work

Neither InfluxDB v1 nor InfluxDB v3 (Core or Enterprise) supports an `UPDATE` statement. Correcting a value means **re-writing a point/row** at the exact same series identity (tags) and exact same timestamp, containing only the corrected field — both storage engines merge a new write into what's already stored at that (series, timestamp) key on a per-field basis, so every other field already recorded at that timestamp is left untouched. This screen queries the current matching points/rows first (to capture each one's exact tag set and timestamp), then re-writes just the one field.

Deleting is where the two versions diverge:

| | InfluxDB v1 | InfluxDB v3 |
| --- | --- | --- |
| **Mechanism** | InfluxQL `DELETE FROM ... WHERE <tags/time>` | InfluxDB 3 **Enterprise** row-delete request (`influxdb3 delete rows` / `POST /api/v3/row_delete_requests`) |
| **Scope** | Whole **point** (every field) at each matching timestamp — InfluxQL's `DELETE` has no field predicate | Whole **row** (every field) at each matching timestamp — the row-delete predicate supports tag equality only, no field predicate |
| **Timing** | Synchronous — applied by the time the request returns | **Asynchronous** — the request is only *accepted*; the compactor applies it later, by default within 24 hours (server-configurable), and the targeted rows remain queryable until then |
| **Availability** | Any InfluxDB v1 server | **InfluxDB 3 Enterprise only**, and only after the storage engine has been upgraded (`--use-pacha-tree` / `--upgrade-pacha-tree`) — InfluxDB 3 Core has no delete capability at all and rejects the request outright |
| **Permissions** | Whatever the configured bridge token already allows | Additionally requires `db:<database>:delete` permission on the token used |

Because of this, **"Delete value(s)" always removes every field at each matching timestamp for the selected device — never just the field shown in the Field picker.** The screen shows a warning to this effect whenever "Delete value(s)" is selected, worded per version, and the staged-changes list marks a v3 delete as *"(whole row, async)"* so it isn't mistaken for something that already happened the moment it was committed. If a v3 server isn't Enterprise, or hasn't had the storage engine upgrade, committing a staged delete fails with a clear error rather than silently doing nothing.

## Value Type Validation

A replacement value entered for **Set Value** is checked against the field's reported type before it is even staged:

- **InfluxDB v1** fields are checked against the type InfluxQL's `SHOW FIELD KEYS` reports (`float`, `integer`, `string`, or `boolean`).
- **InfluxDB v3** fields are checked against the Arrow type `information_schema.columns` reports (e.g. `Float64`, `Int64`, `Utf8`, `Boolean`).

An invalid value — text into an integer field, an unrecognized boolean spelling, a non-whole number into an integer field — is rejected immediately, with a clear error, rather than only surfacing when Commit All Changes is pressed.

## Staging and Commit

Like every other admin screen in this app, nothing is written to InfluxDB (or queued for asynchronous deletion) until **Commit All Changes** is pressed. Staged InfluxDB edits — v1 and v3 together — share one staged-changes list, independent from TimescaleDB's own staged column deletions and Metrics Edit changes; all of them are applied by the same "Commit All Changes" press and cleared together by "Discard Changes."
