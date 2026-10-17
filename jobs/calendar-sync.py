#!/Users/anzes/.herald/.venv/bin/python
"""Busy calendar events for the next 24h, for the focus guard (FOCUS-GUARD-DESIGN.md).

Reads each Google Calendar "secret address in iCal format" listed in secrets/calendar.json:

    {"me": ["anze@example.com"], "feeds": [{"name": "work", "url": "https://calendar.google.com/..."}]}

and writes state/calendar.json with the busy events from now to +24h. Skipped: all-day events,
events marked free (TRANSP:TRANSPARENT), cancelled ones, and ones an address in "me" declined.
ping.in_meeting() reads the file. Stdlib only, like the daemon. The URL is a read credential for
the whole calendar; it lives in secrets/ and is never logged.

Exit 0 when every feed was read, 1 when one failed (the file still holds what was read). With no
secrets file it writes an empty event list and exits 0: the guard is simply off.
Usage: calendar-sync.py
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.herald/.venv/bin/python")

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERALD = Path(os.environ.get("HERALD_HOME", Path.home() / ".herald"))
SECRETS = HERALD / "secrets" / "calendar.json"
ARTIFACT = HERALD / "state" / "calendar.json"
HORIZON_S = 24 * 3600
_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def unfold(text: str) -> list[str]:
    """RFC 5545 lines: a line starting with a space or tab continues the previous one."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def parse_line(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), dict(p.split("=", 1) for p in params if "=" in p), value


def parse_dt(value: str, params: dict) -> datetime | None:
    """An aware datetime, or None for an all-day DATE value."""
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return None
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tz = params.get("TZID")
    return naive.replace(tzinfo=ZoneInfo(tz)) if tz else naive.astimezone()


def events(text: str) -> list[dict]:
    out, cur = [], None
    for line in unfold(text):
        name, params, value = parse_line(line)
        if name == "BEGIN" and value == "VEVENT":
            cur = {"exdates": set(), "attendees": []}
        elif name == "END" and value == "VEVENT" and cur is not None:
            out.append(cur)
            cur = None
        elif cur is not None:
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                cur[name] = parse_dt(value, params)
                cur[name + "_allday"] = cur[name] is None
            elif name == "EXDATE":
                for v in value.split(","):
                    dt = parse_dt(v, params)
                    if dt:
                        cur["exdates"].add(dt.astimezone(timezone.utc))
            elif name == "ATTENDEE":
                cur["attendees"].append((value.lower().removeprefix("mailto:"), params.get("PARTSTAT", "")))
            elif name in ("SUMMARY", "UID", "RRULE", "TRANSP", "STATUS"):
                cur[name] = value
    return out


def _rule(rrule: str) -> dict:
    return dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)


def occurrences(start: datetime, rrule: str | None, until: datetime) -> list[datetime]:
    """Start times up to `until`. DAILY and WEEKLY (with BYDAY, INTERVAL, COUNT, UNTIL) are
    expanded fully, MONTHLY and YEARLY on the same day of the month or year."""
    # shortcut: MONTHLY;BYDAY=2TU-style rules and BYSETPOS are not expanded (only the first
    # occurrence counts); add them if a recurring meeting ever goes missing from calendar.json.
    if not rrule:
        return [start]
    r = _rule(rrule)
    freq, interval = r.get("FREQ"), int(r.get("INTERVAL", "1"))
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [start]
    count = int(r["COUNT"]) if "COUNT" in r else None
    end = until
    if "UNTIL" in r:
        u = r["UNTIL"]
        last = (datetime.strptime(u, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if u.endswith("Z")
                else datetime.strptime(u[:8], "%Y%m%d").replace(tzinfo=start.tzinfo) + timedelta(days=1))
        end = min(end, last)
    days = [d[-2:] for d in r.get("BYDAY", "").split(",") if d] or [_DAYS[start.weekday()]]
    out, n, day = [], 0, start
    while day <= end and (count is None or n < count):
        delta = (day.date() - start.date()).days
        if freq == "DAILY":
            hit = delta % interval == 0
        elif freq == "WEEKLY":
            week = (delta + start.weekday()) // 7  # weeks counted from DTSTART's own week (Monday start)
            hit = week % interval == 0 and _DAYS[day.weekday()] in days
        elif freq == "MONTHLY":
            months = (day.year - start.year) * 12 + day.month - start.month
            hit = (day.day == start.day and months % interval == 0 and "BYDAY" not in r) or delta == 0
        else:
            hit = (day.month, day.day) == (start.month, start.day) and (day.year - start.year) % interval == 0
        if hit:
            n += 1
            out.append(day)
        # Step in local wall-clock days so a meeting stays at 10:00 across a DST change.
        day = datetime.combine(day.date() + timedelta(days=1), start.timetz())
    return out


def busy(evs: list[dict], me: set[str], now: float, horizon_s: float = HORIZON_S) -> list[dict]:
    lo = datetime.fromtimestamp(now, timezone.utc)
    hi = lo + timedelta(seconds=horizon_s)
    overrides = {(e.get("UID"), e["RECURRENCE-ID"].astimezone(timezone.utc)) for e in evs if e.get("RECURRENCE-ID")}
    out = []
    for e in evs:
        start, end_ = e.get("DTSTART"), e.get("DTEND")
        if start is None or e.get("DTSTART_allday"):
            continue
        if e.get("TRANSP") == "TRANSPARENT" or e.get("STATUS") == "CANCELLED":
            continue
        if any(addr in me and stat.upper() == "DECLINED" for addr, stat in e["attendees"]):
            continue
        length = (end_ - start) if end_ else timedelta(hours=1)
        rrule = None if e.get("RECURRENCE-ID") else e.get("RRULE")
        for occ in occurrences(start, rrule, hi):
            utc = occ.astimezone(timezone.utc)
            if utc in e["exdates"] or (rrule and (e.get("UID"), utc) in overrides):
                continue
            if utc + length <= lo or utc >= hi:
                continue
            out.append({"start": utc.timestamp(), "end": (utc + length).timestamp(), "title": e.get("SUMMARY", ""),
                        "attendees": len(e["attendees"])})
    return sorted(out, key=lambda x: x["start"])


def main() -> int:
    now = time.time()
    try:
        secrets = json.loads(SECRETS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        secrets = {"feeds": []}
    except (OSError, ValueError) as exc:
        print(f"calendar: {SECRETS.name} unreadable: {exc}", file=sys.stderr)
        return 1
    me = {a.lower() for a in secrets.get("me") or []}
    found, failed = [], []
    for feed in secrets.get("feeds") or []:
        try:
            with urllib.request.urlopen(feed["url"], timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
            for ev in busy(events(text), me, now):
                found.append({**ev, "feed": feed.get("name", "")})
        except Exception as exc:  # noqa: BLE001 - one bad feed must not hide the others
            failed.append(f"{feed.get('name', '?')}: {type(exc).__name__}")  # never the URL
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARTIFACT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": now, "events": sorted(found, key=lambda x: x["start"]),
                               "failed": failed, "feeds": len(secrets.get("feeds") or [])}, indent=2))
    tmp.replace(ARTIFACT)
    if failed:
        print("calendar: feed(s) failed: " + "; ".join(failed), file=sys.stderr)
        return 1
    print(f"calendar: {len(found)} busy event(s) in the next 24h from {len(secrets.get('feeds') or [])} feed(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
