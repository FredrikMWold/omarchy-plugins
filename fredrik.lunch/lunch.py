#!/usr/bin/env python3

import json
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API_URL = "https://unisonfm.kanpla.dk/api/internal/load/frontend"
SCHOOL_ID = "pfLXR4JNzx35EO7sZtET"
MODULE_ID = "RFN4SQFJkqt2ln2x8Rls"
TIMEZONE = ZoneInfo("Europe/Oslo")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def cache_path() -> Path:
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    )
    return cache_home / "fredrik-lunch" / "menu.json"


def read_cache(today: str) -> dict | None:
    try:
        payload = json.loads(cache_path().read_text())
        if (
            payload.get("date") == today
            and isinstance(payload.get("items"), list)
            and isinstance(payload.get("weeks"), list)
        ):
            payload["stale"] = True
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(dir=path.parent, prefix=".lunch-")
    try:
        with os.fdopen(fd, "w") as temporary:
            json.dump(payload, temporary, ensure_ascii=False)
            temporary.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def write_cache(payload: dict) -> None:
    write_json(cache_path(), payload)


def date_key(value: date) -> str:
    return str(
        int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())
    )


def date_label(value: date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]}"


def week_label(start: date, today: date) -> str:
    end = start + timedelta(days=4)
    if start.month == end.month:
        span = f"{start.day}-{end.day} {MONTHS[start.month - 1]}"
    else:
        span = f"{date_label(start)}-{date_label(end)}"
    return f"This week - {span}" if start <= today <= end else span


def default_week_start(today: date) -> date:
    current_monday = today - timedelta(days=today.weekday())
    return (
        current_monday if today.weekday() < 5 else current_monday + timedelta(days=7)
    )


def fetch_menu(today_text: str) -> dict:
    body = json.dumps(
        {"schoolId": SCHOOL_ID, "language": "no", "url": "app"}
    ).encode()
    request = Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "kanpla-pre-compression": "false",
        },
        method="POST",
    )

    with urlopen(request, timeout=20) as response:
        data = json.load(response)

    today = datetime.fromisoformat(today_text).date()
    offers = data.get("offers", {}).get(MODULE_ID, {})
    items_by_date: dict[str, list[dict[str, str]]] = {}

    for product in offers.get("items", []):
        label = str(product.get("name") or product.get("category") or "Lunch").strip()
        for key, availability in product.get("dates", {}).items():
            menu = availability.get("menu")
            if (
                availability.get("available") is not True
                or not isinstance(menu, dict)
            ):
                continue
            name = str(menu.get("name") or "").strip()
            if name:
                items_by_date.setdefault(key, []).append(
                    {"label": label, "name": name}
                )

    offer_period = data.get("offerPeriod", [])
    if len(offer_period) == 2:
        first_date = datetime.fromtimestamp(
            offer_period[0], tz=timezone.utc
        ).date()
        last_date = datetime.fromtimestamp(
            offer_period[1], tz=timezone.utc
        ).date()
    else:
        first_date = today - timedelta(weeks=4)
        last_date = today + timedelta(weeks=8)

    first_monday = first_date - timedelta(days=first_date.weekday())
    last_monday = last_date - timedelta(days=last_date.weekday())
    current_monday = today - timedelta(days=today.weekday())
    default_monday = default_week_start(today)
    weeks = []
    week_start = first_monday

    while week_start <= last_monday:
        days = []
        for weekday_index, weekday in enumerate(WEEKDAYS):
            value = week_start + timedelta(days=weekday_index)
            days.append(
                {
                    "date": value.isoformat(),
                    "weekday": weekday,
                    "dateLabel": date_label(value),
                    "isToday": value == today,
                    "items": items_by_date.get(date_key(value), []),
                }
            )
        weeks.append(
            {
                "start": week_start.isoformat(),
                "label": week_label(week_start, today),
                "isCurrent": week_start == current_monday,
                "isDefault": week_start == default_monday,
                "days": days,
            }
        )
        week_start += timedelta(days=7)

    items = items_by_date.get(date_key(today), [])
    text = "\n".join(f"{item['label']}: {item['name']}" for item in items)
    return {
        "date": today_text,
        "location": data.get("school", {}).get(
            "name", "Hinna Park Kanalpiren Kantine"
        ),
        "items": items,
        "text": text,
        "weeks": weeks,
        "defaultWeekStart": default_monday.isoformat(),
        "stale": False,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    today = datetime.now(TIMEZONE).date().isoformat()

    try:
        payload = fetch_menu(today)
        try:
            write_cache(payload)
        except OSError:
            pass
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError) as error:
        cached = read_cache(today)
        if cached:
            print(json.dumps(cached, ensure_ascii=False))
            return 0
        print(
            json.dumps(
                {"error": f"Could not load today's lunch menu: {error}"},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
