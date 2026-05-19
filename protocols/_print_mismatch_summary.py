import json
from pathlib import Path

d = json.loads(
    Path(__file__).with_name("_register_code_mismatch_report.json").read_text(encoding="utf-8")
)
print("=== JSON codes without CSV register (%d) ===" % len(d["json_codes_without_csv_register"]))
for x in d["json_codes_without_csv_register"]:
    sim = x.get("similar_registers") or []
    reg = sim[0]["register"] if sim else "-"
    names = ", ".join({s.get("doc") or s.get("var") for s in sim if s.get("doc") or s.get("var")})
    extra = f" near: {names}" if names else ""
    print(f"  {x['protocol']}: {x['json_key']} (reg hint {reg}{extra})")

print()
print("=== CSV fault/alarm without JSON codes (%d) ===" % len(d["csv_fault_alarm_without_json_codes"]))
by_proto = {}
for x in d["csv_fault_alarm_without_json_codes"]:
    by_proto.setdefault(x["protocol"], []).append(x)
for proto in sorted(by_proto):
    print(f"\n-- {proto} ({len(by_proto[proto])}) --")
    for x in by_proto[proto][:25]:
        print(f"  reg {x['register']}: {x['name']} [{x['csv']}]")
    if len(by_proto[proto]) > 25:
        print(f"  ... +{len(by_proto[proto]) - 25} more")
