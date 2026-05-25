# Description: Provides protocol maintenance tooling for MultiProtocolGateway register data.
# File: _export_mismatch_md.py
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

import json
from pathlib import Path
from collections import defaultdict

d = json.loads(
    Path(__file__).with_name("_register_code_mismatch_report.json").read_text(encoding="utf-8")
)
out = Path(__file__).with_name("_mismatch_summary.md")
lines = [
    "# Register / codes mismatch summary",
    "",
    f"- **Matched:** {d['matched_count']}",
    f"- **JSON `_codes` without CSV register:** {len(d['json_codes_without_csv_register'])}",
    f"- **CSV fault/alarm rows without JSON `_codes`:** {len(d['csv_fault_alarm_without_json_codes'])}",
    "",
    "## JSON codes without matching CSV register",
    "",
    "| Protocol | JSON key | Similar CSV (reg:name) |",
    "| --- | --- | --- |",
]
for x in d["json_codes_without_csv_register"]:
    sim = x.get("similar_registers") or []
    regs = "; ".join(
        f"{s['register']}:{s.get('doc') or s.get('var')}" for s in sim[:3]
    )
    lines.append(f"| {x['protocol']} | `{x['json_key']}` | {regs or '—'} |")

lines += [
    "",
    "## CSV fault/alarm registers without JSON codes",
    "",
]
by = defaultdict(list)
for x in d["csv_fault_alarm_without_json_codes"]:
    by[x["protocol"]].append(x)
for p in sorted(by):
    lines.append(f"### {p} ({len(by[p])})")
    lines.append("")
    lines.append("| Register | Normalized name | Suggested JSON key |")
    lines.append("| --- | --- | --- |")
    for x in by[p]:
        lines.append(f"| {x['register']} | `{x['name']}` | `{x['name']}_codes` |")
    lines.append("")

out.write_text("\n".join(lines), encoding="utf-8")
print("wrote", out)
