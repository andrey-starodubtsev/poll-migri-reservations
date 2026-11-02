#!/usr/bin/env python3
"""
Check Migri reservation availability.

Reads configuration from .config (JSON) in the current directory.
Prints a message if an earlier appointment is available than the one
already reserved (or any appointment, if none is reserved yet).

Config format:
{
  "service_category": "Residence permit",     // English name (case-insensitive)
  "service": "1. Work",                       // English name (case-insensitive)
  "office": "Helsinki service point (Malmi)", // English name, or "all"
  "people": 1,                                // number of applicants (1–6)
  "family": false,                            // true if applicants are family members
  "reserved_time": "2026-10-15T09:00:00"      // ISO 8601, optional
}
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE_URL = "https://migri.vihta.com/public/migri"
WEEKS_TO_SEARCH = 52  # Search up to one year ahead


def _strip_comments(text: str) -> str:
    """Remove // line comments from JSONC text, preserving strings."""
    result = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < len(text):
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            result.append(ch)
        else:
            if ch == '"':
                in_string = True
                result.append(ch)
            elif ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
                # Skip until end of line
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            else:
                result.append(ch)
        i += 1
    return "".join(result)


def load_config(path: Path) -> dict:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        sys.exit(f"Error: config file not found at {path}")
    try:
        return json.loads(_strip_comments(raw))
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid JSON in config file: {e}")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://migri.vihta.com/public/migri/",
        "Accept": "application/json, text/plain, */*",
    })
    r = s.get(f"{BASE_URL}/api/sessions?language=fi")
    r.raise_for_status()
    s.headers["Vihta-Session"] = r.json()["id"]
    return s


def find_by_name(items: list, name: str, key: str = "name") -> dict | None:
    needle = name.strip().lower()
    for item in items:
        # Try English l10n name first, then default name
        en_name = ""
        l10n = item.get("l10n", {})
        for entry in l10n.get("names", []):
            if entry.get("key") == "en":
                en_name = entry.get("value", "")
                break
        if en_name.strip().lower() == needle:
            return item
        if item.get(key, "").strip().lower() == needle:
            return item
    return None


def get_categories(s: requests.Session) -> list:
    r = s.get(f"{BASE_URL}/api/servicecategories")
    r.raise_for_status()
    return r.json().get("serviceCategories", [])


def get_services(s: requests.Session, category_id: str) -> list:
    r = s.get(f"{BASE_URL}/api/servicecategories/{category_id}/services")
    r.raise_for_status()
    return r.json().get("services", [])


def get_offices(s: requests.Session) -> list:
    r = s.get(f"{BASE_URL}/api/offices")
    r.raise_for_status()
    data = r.json()
    # Response can be {"offices": [...]} or a plain list
    if isinstance(data, dict):
        return data.get("offices", [])
    return data


def get_available_times(
    s: requests.Session,
    office_id: str,
    service_id: str,
    year: int,
    week: int,
    people: int = 1,
    family: bool = False,
) -> list[datetime]:
    if people == 1:
        mode = "SINGLE"
    elif family:
        mode = "FAMILY"
    else:
        mode = "GROUP"

    payload = {
        "serviceSelections": [{"values": [service_id]}] * people,
        "extraServices": [],
    }
    params = {"end_hours": 24, "mode": mode, "start_hours": 0}
    r = s.post(
        f"{BASE_URL}/api/scheduling/offices/{office_id}/{year}/w{week}",
        json=payload,
        params=params,
    )
    if r.status_code != 200:
        return []
    daily = r.json().get("dailyTimesByOffice", [])
    times = []
    for day_slots in daily:
        for slot in day_slots:
            ts = slot.get("startTimestamp")
            if ts:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                times.append(dt)
    return times


def find_earliest_available(
    s: requests.Session,
    office_id: str,
    service_id: str,
    cutoff: datetime | None,
    people: int = 1,
    family: bool = False,
) -> datetime | None:
    now = datetime.now(timezone.utc)

    for offset in range(WEEKS_TO_SEARCH):
        target = now + timedelta(weeks=offset)
        iso = target.isocalendar()
        year, week = iso.year, iso.week

        times = get_available_times(s, office_id, service_id, year, week, people, family)
        for t in sorted(times):
            if t <= now:
                continue
            if cutoff is None or t < cutoff:
                return t

    return None


def main():
    config_path = Path(".config")
    cfg = load_config(config_path)

    required = ["service_category", "service"]
    for key in required:
        if key not in cfg:
            sys.exit(f"Error: '{key}' missing from config")

    cat_name = cfg["service_category"]
    svc_name = cfg["service"]
    office_filter = cfg.get("office", "all")
    reserved_str = cfg.get("reserved_time")

    people = cfg.get("people", 1)
    if not isinstance(people, int) or not (1 <= people <= 6):
        sys.exit("Error: 'people' must be an integer between 1 and 6")

    family = cfg.get("family", False)
    if not isinstance(family, bool):
        sys.exit("Error: 'family' must be true or false")
    if family and people < 2:
        sys.exit("Error: 'family' can only be true when 'people' is 2 or more")

    reserved_time = None
    if reserved_str:
        try:
            reserved_time = datetime.fromisoformat(reserved_str)
            if reserved_time.tzinfo is None:
                reserved_time = reserved_time.replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"Error: 'reserved_time' is not a valid ISO 8601 datetime: {reserved_str!r}")

    print(f"Connecting to Migri reservation system...")
    try:
        s = make_session()
    except requests.RequestException as e:
        sys.exit(f"Error: could not reach Migri API: {e}")

    print(f"Looking up service category: {cat_name!r}")
    categories = get_categories(s)
    category = find_by_name(categories, cat_name)
    if not category:
        available = [c.get("name") for c in categories]
        sys.exit(
            f"Error: service category {cat_name!r} not found.\n"
            f"Available: {', '.join(available)}"
        )

    print(f"Looking up service: {svc_name!r}")
    services = get_services(s, category["id"])
    service = find_by_name(services, svc_name)
    if not service:
        available = [sv.get("name") for sv in services]
        sys.exit(
            f"Error: service {svc_name!r} not found in category {cat_name!r}.\n"
            f"Available: {', '.join(available)}"
        )

    print(f"Loading offices...")
    all_offices = get_offices(s)

    if office_filter.strip().lower() == "all":
        offices_to_check = all_offices
    else:
        office = find_by_name(all_offices, office_filter)
        if not office:
            available = [o.get("name") for o in all_offices]
            sys.exit(
                f"Error: office {office_filter!r} not found.\n"
                f"Available: {', '.join(available)}"
            )
        offices_to_check = [office]

    if reserved_time:
        local_reserved = reserved_time.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        print(f"Currently reserved: {local_reserved}")
    else:
        print("No reservation yet — looking for any available time")

    mode_desc = "FAMILY" if (family and people > 1) else ("GROUP" if people > 1 else "SINGLE")
    print(f"People: {people}  Mode: {mode_desc}")

    found_any = False
    for office in offices_to_check:
        office_name = office.get("name", office["id"])
        print(f"\nChecking: {office_name}")
        earliest = find_earliest_available(
            s, office["id"], service["id"], reserved_time, people, family
        )
        if earliest:
            found_any = True
            local_dt = earliest.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            if reserved_time:
                print(
                    f"  ✓ Earlier time available at {office_name}:\n"
                    f"    {local_dt}\n"
                    f"    Book at: https://migri.vihta.com/public/migri/#/reservation"
                )
            else:
                print(
                    f"  ✓ Time available at {office_name}:\n"
                    f"    {local_dt}\n"
                    f"    Book at: https://migri.vihta.com/public/migri/#/reservation"
                )
        else:
            if reserved_time:
                print(f"  – No earlier time found (checked {WEEKS_TO_SEARCH} weeks ahead)")
            else:
                print(f"  – No available times found (checked {WEEKS_TO_SEARCH} weeks ahead)")

    if not found_any:
        if reserved_time:
            print("\nNo earlier times found at any checked office.")
        else:
            print("\nNo available times found at any checked office.")


if __name__ == "__main__":
    main()
