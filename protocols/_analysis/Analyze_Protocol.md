# 🧭 OVERVIEW

The method functions as a **self-identification engine** for Modbus devices (like inverters) by following these core steps:

* **Scan:** Performs a dense Modbus scan of `INPUT` and `HOLDING` registers.
* **Snapshot:** Saves a raw snapshot, including nulls for missing registers.
* **Decode:** Processes the snapshot using protocol map as described in the \<protocol\>.json files.
* **Score:** Evaluates each protocol based on how well values fit expected ranges and types.
* **Output:** Generates decoded JSON files and a protocol score table for auto-detection.

---

## How to get started

### 1.  Set up analysis folder

* Copy the target comparison inverters json files from their protocols/folders, and place them in the analysis folder.
* Make sure that "analyze_protocol = true" and "analyze_protocol_save_load = true" in the config.cgf folder.
* Start PPG.  The analysis files will save into the three sub-folders below the analysis folder.

## 🔢 Step-by-Step Execution

### 1. Initialize Logging and Imports

* Logs the analyzer start event.
* **Imports:** `Path`, `json`, `datetime`, `typing`, and `dataclass`.
* Defines all helper functions locally.

### 2. Define Helper Tools (Scoped to Method)

* **Data Structures:**
  * `RegisterRef`: A normalized representation of a whole register, high/low bytes, or bitfields.
* **Parsers and Decoders:**
  * `parse_register_expr()`: Converts map expressions (e.g., `11.b7`, `9.2`).
  * `extract_value()`: Pulls specific values from the dense raw map.
  * `decode_ascii()`: Reconstructs strings from byte sequences.
  * `unique_key()`: Prevents variable name collisions.
  * `evaluate_score()`: Assigns points based on type, range, and ASCII validity.

### 3. Build Output Folder Structure

* **Creates the following hierarchy if missing:**
  * `analysis/`
    * `raw_scans/`  Stores the raw scanned data from the inverter.
    * `decoded/`    Stores the scanned data that is joined to registry descriptions that correspond to the json file
    * `scores/`     Gives a comparison score of registries as they apply to the data retrieved.

### 4. Load Protocol Definitions

* **For each `*.json` protocol file:**
  1. Instantiate `protocol_settings(name)`.
  2. Store in `protocols` dictionary.
  3. Track `max INPUT` and `max HOLDING` registers to determine scan range.

* **If Cache Exists:** Loads `input_dense.json` and `holding_dense.json`.
* **Else:** Calls `read_modbus_registers(start=0, end=max_register)`.
  * Returns a dense map: `{ "0": 123, "1": null }`.
  * Saves raw JSON as the "source of truth."

### 6. Initialize Protocol Scoring

* Creates `protocol_scores: Dict[str, int]`.
* Accumulates scores for `input`, `holding`, and `total`.

### 7. Decode Each Protocol Against Raw Snapshot

* **For each protocol:**
  * **Auto Create containers:** `input_decoded` and `holding_decoded`.
  * **Process registry types:**
    * Reset naming helpers and ASCII groups.
    * Iterate map entries: Generate unique name, parse `RegisterRef`, and extract/score value.
    * Decode ASCII groups: Sort by register, extract bytes, and convert to string.

### 8. Compute Protocol Score

* `protocol_scores[name] = input_score + holding_score`
* Measures the mathematical "fit" of the raw device data against the protocol template.

### 9. Save Decoded JSON Per Protocol

* **Creates files in `analysis/decoded/` containing:**
  * Protocol name and registry type.
  * Timestamp.
  * Decoded `values` dictionary.

### 10. Save Score Summary

* Writes `analysis/scores/protocol_scores.json`.
* Example: `{"growatt_v1": 142, "eg4_18kpv": 38}`

### 11. Log Ranked Results

* Sorts protocols by score and logs the winner.
* Enables **automatic protocol detection**.
