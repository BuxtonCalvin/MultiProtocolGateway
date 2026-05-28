# Description: Provides protocol maintenance tooling for MultiProtocolGateway register data.
# File: _gen_match_report.py
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
import itertools
import json
import re
from collections import defaultdict
from pathlib import Path

protocols = Path(__file__).parent

def norm(s):
    return (s or "").strip().lower().replace(" ", "_").replace("__", "_")

def read_csv_rows(path):
    """Match MPG protocol_settings.load__registry header normalization."""
    with open(path, newline="", encoding="latin-1") as f:
        delimiter = ";"
        first_row = next(f).lower().replace("_", " ")
        if first_row.count(";") < first_row.count(","):
            delimiter = ","
        first_row = re.sub(
            r"\s+" + re.escape(delimiter) + "|" + re.escape(delimiter) + r"\s+",
            delimiter,
            first_row,
        )
        reader = csv.DictReader(itertools.chain([first_row], f), delimiter=delimiter)
        yield from reader
def base_name(p):
    n = p.stem
    for suf in [".holding_registry_map", ".input_registry_map", ".registry_map", ".override"]:
        if n.endswith(suf):
            return n[: -len(suf)]
    return n

def load_regs(paths):
    regs = {}
    for path in paths:
        for row in read_csv_rows(path):
            reg = (row.get("register") or "").strip()
            if not reg or reg.startswith("#"):
                continue
            doc = norm(row.get("documented name"))
            var = norm(row.get("variable name")) or doc
            key = doc or var
            if not key:
                continue
            regs[key] = {
                "register": reg,
                "doc": doc,
                "var": var,
                "file": path.name,
                "data_type": (row.get("data type") or "").strip(),
                "writable": (row.get("writable") or "").strip(),
                "values": (row.get("values") or "").strip(),
                "note": (row.get("note") or "").strip()[:120],
            }
    return regs

groups = defaultdict(list)
for p in protocols.rglob("*.csv"):
    if "_test" in str(p):
        continue
    groups[(p.parent.name, base_name(p))].append(p)

json_no_reg = []
csv_no_codes = []
matched = []

for (folder, base), paths in sorted(groups.items()):
    jp = protocols / folder / f"{base}.json"
    if not jp.exists():
        continue
    data = json.loads(jp.read_text(encoding="utf-8"))
    code_bases = {norm(k[:-6]): k for k in data if k.endswith("_codes")}
    regs = load_regs(paths)
    hit = set()
    for name, r in regs.items():
        for n in (r["doc"], r["var"]):
            if n and n in code_bases:
                hit.add(n)
                matched.append(
                    {
                        "protocol": f"{folder}/{base}",
                        "code_base": n,
                        "register": r["register"],
                        "csv": r["file"],
                    }
                )
                break
        else:
            n = r["doc"] or r["var"]
            vals = r["values"]
            if re.match(r"^(faultcode|warningcode)_(e|w)\d+$", n):
                continue
            if vals and (vals.startswith("{") or "=" in vals):
                continue
            if any(
                k in n
                for k in [
                    "fault",
                    "alarm",
                    "warning",
                    "event_flag",
                    "faultcode",
                    "warningcode",
                ]
            ):
                if not vals.startswith("{"):
                    csv_no_codes.append(
                        {
                            "protocol": f"{folder}/{base}",
                            "register": r["register"],
                            "name": n,
                            "csv": r["file"],
                            "note": r["note"],
                        }
                    )
    # Aggregate faultcode_e000 bit rows -> faultcode_codes
    aggregate_bits = defaultdict(list)
    for r in regs.values():
        m = re.match(r"^(faultcode|warningcode)_(e|w)(\d+)$", r["doc"] or r["var"])
        if m:
            aggregate_bits[norm(f"{m.group(1)}_codes")].append(r)

    for cb, jk in code_bases.items():
        if cb in hit:
            continue
        agg_key = norm(jk)
        if agg_key in aggregate_bits:
            hit.add(cb)
            matched.append(
                {
                    "protocol": f"{folder}/{base}",
                    "code_base": cb,
                    "match_type": "aggregate_bit_registers",
                    "register_count": len(aggregate_bits[agg_key]),
                    "registers_sample": [x["register"] for x in aggregate_bits[agg_key][:3]],
                }
            )
            continue
        sim = [
            {"register": r["register"], "doc": r["doc"], "var": r["var"]}
            for r in regs.values()
            if cb in (r["doc"], r["var"])
            or r["doc"] in cb
            or r["var"] in cb
            or cb.replace("_", "") in (r["doc"] + r["var"]).replace("_", "")
        ]
        json_no_reg.append(
            {
                "protocol": f"{folder}/{base}",
                "json_key": jk,
                "code_base": cb,
                "suggested_json_key": f"{cb}_codes",
                "similar_registers": sim[:10],
            }
        )

out: Path = protocols / "_register_code_mismatch_report.json"
out.write_text(
    json.dumps(
        {
            "matched_count": len(matched),
            "json_codes_without_csv_register": json_no_reg,
            "csv_fault_alarm_without_json_codes": csv_no_codes,
            "matched_sample": matched[:50],
        },
        indent=2,
    ),
    encoding="utf-8",
)
print("wrote", out)
print("matched", len(matched))
print("json orphan codes", len(json_no_reg))
print("csv needs codes", len(csv_no_codes))
