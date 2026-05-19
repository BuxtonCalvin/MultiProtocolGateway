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
