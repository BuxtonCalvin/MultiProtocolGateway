import csv
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUBDOMAIN: str = "hub"
DOMAIN: str = "docker.com"
HOST: str = f"{SUBDOMAIN}.{DOMAIN}"
REPO: str = "buxtoncalvin/multiprotocolgateway"
PATH_PREFIX: str = "/v2/repositories/"
API_URL: str = f"https://{HOST}{PATH_PREFIX}{REPO}/"

TOOLS_DIR: Path = Path(__file__).resolve().parent
CSV_FILE: Path = TOOLS_DIR / "docker-pull-history.csv"
BADGE_JSON_FILE: Path = TOOLS_DIR / "monthly-pulls-badge.json"
FIELDNAMES: list[str] = ["date", "total_pulls", "daily_new_pulls"]


def get_pull_count() -> int:
    req = urllib.request.Request(API_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        data: dict[str, object] = json.loads(response.read().decode("utf-8"))
    return int(str(data["pull_count"]))


def main() -> None:
    current_pulls: int = get_pull_count()
    now_utc: datetime = datetime.now(timezone.utc)
    today: str = now_utc.strftime("%Y-%m-%d")
    current_month_prefix: str = now_utc.strftime("%Y-%m")

    rows: list[dict[str, str]] = []
    if CSV_FILE.exists():
        with CSV_FILE.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    # Update today's row if already run today; otherwise append a new row
    if rows and rows[-1]["date"] == today:
        prev_pulls: int = int(rows[-2]["total_pulls"]) if len(rows) > 1 else current_pulls
        daily_new: int = max(0, current_pulls - prev_pulls)
        rows[-1] = {
            "date": today,
            "total_pulls": str(current_pulls),
            "daily_new_pulls": str(daily_new),
        }
    else:
        prev_pulls = int(rows[-1]["total_pulls"]) if rows else current_pulls
        daily_new = max(0, current_pulls - prev_pulls)
        rows.append(
            {
                "date": today,
                "total_pulls": str(current_pulls),
                "daily_new_pulls": str(daily_new),
            }
        )

    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter[str] = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # Sum daily_new_pulls for the current YYYY-MM month
    monthly_total: int = sum(
        int(row["daily_new_pulls"])
        for row in rows
        if row["date"].startswith(current_month_prefix)
    )

    # Write Shields.io endpoint JSON
    badge_payload: dict[str, object] = {
        "schemaVersion": 1,
        "label": "pulls this month",
        "message": f"{monthly_total:,}",
        "color": "blue",
    }
    BADGE_JSON_FILE.write_text(json.dumps(badge_payload, indent=2) + "\n", encoding="utf-8")

    print(f"Logged {today}: {current_pulls} total (+{daily_new} new, {monthly_total} this month)")


if __name__ == "__main__":
    main()
