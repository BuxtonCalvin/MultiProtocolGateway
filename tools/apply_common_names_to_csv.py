# Description: Provides a developer utility for working with MultiProtocolGateway data or simulations.
# File: apply_common_names_to_csv.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import csv
import json

path = "protocols/eg4_v58.holding_registry_map.csv"
save_path = "protocols/eg4_v58.holding_registry_map.csv"

common_path = "tools/common_names.json"

common_names : dict[str,str] = {}

# Open the file and load the JSON data
with open(common_path, "r") as file:
    common_names = json.load(file)


new_csv : list[dict[str,str]] = []
with open(path, newline="") as csvfile:
    # Create a CSV reader object
    reader = csv.DictReader(csvfile, delimiter=";") #compensate for openoffice
    fieldnames = reader.fieldnames

    # Iterate over each row in the CSV file
    for row in reader:
        if not row["variable name"] and row["documented name"]:
            if row["documented name"] not in common_names:
                print("no friendly name : " + row["documented name"])
                new_csv.append(row)
                continue

            if not row["variable name"].strip(): #if empty, apply name
                row["variable name"] = common_names[row["documented name"]]
                print(row["documented name"] + " -> " + common_names[row["documented name"]])
                new_csv.append(row)
                continue
        new_csv.append(row)

# Write data to the output CSV
with open(save_path, "w", newline="") as outfile:
    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(new_csv)

print("saved to "+save_path)
