"""Can the phone receive what Jarvis sends? Asked of the ntfy server's own record.

Written 2026-09-15, the second time the phone stopped receiving without anything
here noticing:

  * 2026-09-07 14:54 -> 09-13 13:21. The app's one login for the server
    (`jarviscmd`) could not read `jarvis`, and ntfy logged `subscribers=0` for
    six days while the daemon published every hour.
  * 2026-09-14 08:10 -> still open when this was written. No request from the
    app reached Caddy's log, from home or from mobile, and the 09-15 07:25
    report push recorded "pushed morning-digest.md as 1 message(s)" with nobody
    there to receive it. It was read at first as the app no longer trying. It
    was not: the app had pinned the server's certificate, `tls internal`
    replaces it every ~8h, and every reconnect after a renewal had resumed a
    TLS session, which skips the check. Its first full handshake failed on the
    pin ("Certificate fingerprint mismatch"), and a failed handshake is no line
    in an access log. So no request means not connected -- never "not trying".

Every publish in both succeeded: a server with no one listening answers 200.
Nothing that checks the publish could have seen either. The server can. ntfy
logs a `Server stats` line every minute with its number of live subscriptions,
and Caddy's access log records each of the app's connections -- with the
topics in its path -- when that connection ends.

Three answers, never two, as everywhere in this daemon:

  connected     the newest stats line (under `stats_max_age_s` old) counts a
                subscription
  disconnected  it counts none; `contact_at` is the last moment either log saw
                the phone
  unknown       ntfy.log is missing, unreadable, has no stats line, or its
                newest one is stale -- ntfy is not running, or not logging.
                Never read as connected.

A gap is not a loss. The app asks for what it missed (`?since=<last id>`,
visible in Caddy's log) every time it reconnects, so a message waits in ntfy's
cache and arrives late. Only a gap longer than `alert_after_s` is a finding.

`subscribers` counts subscriptions, not devices: the phone on three topics
reads 3. On 2026-09-13 13:21 it read 2 -- reports and commands, no `jarvis` --
so a count above zero is not evidence that the ping arrives. *Which* topics
comes from the app's newest logged connection (status 101 or 200, User-Agent
`ntfy/`). Only a subscribed connection counts: the server denies by default,
so a forged User-Agent gets 401/403 and is ignored. And a logged connection
describes the CURRENT subscription only while the live count still matches it;
when it does not, the app has re-subscribed since, and the answer waits for
that connection to be logged -- the app reconnects about hourly.

Read-only, tier 0: two log files opened for reading, nothing written.

    python daemon/delivery.py [--json]
    exit 0 receiving, 1 not receiving or not yet known, 2 could not check
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import store

CONNECTED, DISCONNECTED, UNKNOWN = "connected", "disconnected", "unknown"

# `2026/09/15 11:39:29 INFO Server stats (..., subscribers=0, tag=manager, ...)`
# -- one a minute, in local time.
_STATS = re.compile(
    rb"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) INFO Server stats \([^\n]*?\bsubscribers=(\d+)", re.M
)
_SUBSCRIBED = re.compile(
    rb"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) INFO Server stats \([^\n]*?\bsubscribers=[1-9]", re.M
)
# The newest stats line is always in the last few hours of ntfy.log (~250
# bytes a minute). The longer reads happen only when the phone has been gone.
_STATS_TAIL = 64 * 1024
_NEAR = 512 * 1024

_APP_AGENT = "ntfy/"
# 101 is the WebSocket upgrade, 200 a JSON/SSE stream or a poll. 401, 403 and
# 404 are requests that got no subscription -- which is all a forged
# User-Agent can get from a server that denies by default.
_SUBSCRIBED_STATUS = (101, 200)
_ENDPOINTS = ("ws", "json", "sse", "raw")

_REMEDY_SILENT = (
    "On the phone: open the ntfy app and read the subscription's connection error. "
    "'Certificate fingerprint mismatch' means the app pinned a certificate the server has "
    "since replaced - REMAINING-TASKS.md section 14. No error and no ntfy notification "
    "means Android stopped its background connection - set the app's battery usage to "
    "Unrestricted. Nothing sent meanwhile is lost unless it is older than ntfy's "
    "cache-duration (/opt/homebrew/etc/ntfy/server.yml)."
)


def _cfg(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else store.load_config()
    section = dict(cfg.get("delivery") or {})
    section.setdefault("ntfy_log", "/opt/homebrew/var/log/ntfy/ntfy.log")
    section.setdefault("caddy_log", "/opt/homebrew/var/log/caddy/caddy.log")
    section.setdefault("app_host", "ntfy.unspokenmallard.xyz")
    section.setdefault("alert_after_s", 7200)
    section.setdefault("stats_max_age_s", 300)
    section.setdefault("scan_bytes", 4 * 1024 * 1024)
    return section


def required_topics(cfg: Mapping[str, Any]) -> dict[str, str]:
    """Topic -> what stops arriving without it.

    Read from the senders' own sections, so renaming a topic moves the check
    with it instead of leaving it watching the old name.
    """
    need: dict[str, str] = {}
    ping_cfg = cfg.get("ping") or {}
    if ping_cfg.get("enabled"):
        topic = urllib.parse.urlsplit(str(ping_cfg.get("url") or "")).path.strip("/")
        if topic:
            need[topic] = "the counts pings and the replies to phone commands"
    reports_cfg = cfg.get("reports") or {}
    if reports_cfg.get("enabled") and reports_cfg.get("topic"):
        need[str(reports_cfg["topic"])] = "the reports"
    return need


def _listing(items: list[str]) -> str:
    """`a`, `a, and b`: the comma keeps "pings and replies" from reading as the list."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]


# --------------------------------------------------------------------------
# Reading the logs
# --------------------------------------------------------------------------


def _tail(path: Path, max_bytes: int) -> tuple[bytes, int, bool]:
    """The end of a log: (data, offset of its first whole line, whole file?).

    One read into one buffer, searched in place. The daemon's RSS is on a soak
    gate, and megabytes split into thousands of line objects is the shape of
    the step corpus-capture used to leave (2026-09-13); one buffer goes whole.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - int(max_bytes))
        fh.seek(start)
        data = fh.read(size - start)
    first = 0
    if start:
        cut = data.find(b"\n")
        first = cut + 1 if cut >= 0 else len(data)
    return data, first, start == 0


def _local_ts(stamp: bytes) -> float:
    """ntfy logs local time, with no zone."""
    return datetime.strptime(stamp.decode("ascii"), "%Y/%m/%d %H:%M:%S").timestamp()


def _newest_stats(path: Path) -> tuple[float, int] | None:
    data, first, _whole = _tail(path, _STATS_TAIL)
    last = None
    for last in _STATS.finditer(data, first):
        pass
    return None if last is None else (_local_ts(last.group(1)), int(last.group(2)))


def _last_subscribed(path: Path, scan_bytes: int) -> tuple[float | None, float | None]:
    """The newest minute ntfy counted a subscription -- else how far back was read."""
    limit = max(_NEAR, int(scan_bytes))
    for size in (_NEAR, limit):
        data, first, whole = _tail(path, size)
        last = None
        for last in _SUBSCRIBED.finditer(data, first):
            pass
        if last is not None:
            return _local_ts(last.group(1)), None
        if whole or size >= limit:
            oldest = _STATS.search(data, first)
            return None, (_local_ts(oldest.group(1)) if oldest else None)
    return None, None


def _parse_request(line: bytes, host: str) -> dict | None:
    """One Caddy access entry, if it is the ntfy app holding a subscription."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    request = entry.get("request")
    if not isinstance(request, dict) or request.get("host") != host:
        return None
    if entry.get("status") not in _SUBSCRIBED_STATUS:
        return None
    agents = (request.get("headers") or {}).get("User-Agent") or []
    if not any(str(agent).startswith(_APP_AGENT) for agent in agents):
        return None
    path, _sep, query = str(request.get("uri") or "").partition("?")
    segments = path.strip("/").split("/")
    if len(segments) != 2 or segments[1] not in _ENDPOINTS:
        return None
    topics = [topic for topic in segments[0].split(",") if topic]
    if not topics:
        return None
    closed_at = float(entry.get("ts") or 0.0)
    return {
        "topics": topics,
        "closed_at": closed_at,
        "opened_at": closed_at - float(entry.get("duration") or 0.0),
        "mode": "poll" if "poll=1" in query.split("&") else "stream",
        "agent": str(agents[0]),
    }


def _app_requests(path: Path, host: str, scan_bytes: int) -> list[dict]:
    """The app's subscribed requests at the end of Caddy's log, newest first.

    Caddy writes a request when it ENDS, so a WebSocket shows up when it
    closes. Stops at the newest stream: the checks need that one and nothing
    older.
    """
    data, first, _whole = _tail(path, scan_bytes)
    marker = host.encode("ascii")
    found: list[dict] = []
    end = len(data)
    while end > first:
        hit = data.rfind(marker, first, end)
        if hit < 0:
            break
        start = max(first, data.rfind(b"\n", first, hit) + 1)
        stop = data.find(b"\n", hit)
        request = _parse_request(data[start:stop if stop >= 0 else len(data)], host)
        if request is not None:
            found.append(request)
            if request["mode"] == "stream":
                break
        end = start
    return found


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------


def _ago(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _unknown(out: dict, why: str, ntfy_log: str) -> dict:
    message = f"cannot tell whether the phone can receive: {why}"
    out.update(state=UNKNOWN, healthy=None, detail=message, findings=[("delivery:unknown", message)],
               remedy=f"Check the ntfy container (docker ps -f name=ntfy) and the end of {ntfy_log}. "
                      "Until this reads again, assume nothing reaches the phone.")
    return out


def _topics_verdict(out: dict, conn: dict, need: dict[str, str], host: str, head: str,
                    live_count: int | None) -> dict:
    """Judge the app's subscriptions from its newest logged connection.

    `live_count` is ntfy's count now, when connected. The logged connection
    describes the current subscription only while that count still fits it;
    otherwise the app has re-subscribed since, and the verdict waits.
    """
    out["app"] = conn
    topics = list(conn["topics"])
    missing = [topic for topic in need if topic not in topics]
    fits = live_count is None or live_count == len(topics) or (not missing and live_count > len(topics))
    if not fits:
        out["detail"] = (
            f"{head}; the app has re-subscribed since its last logged connection "
            f"({', '.join(topics)}), so which topics waits for the next one"
        )
        return out
    out["topics"] = topics
    if missing:
        lost = _listing([need[topic] for topic in missing])
        message = f"the phone's ntfy app is not subscribed to {', '.join(missing)}, so {lost} never arrive"
        out["missing"] = missing
        out["findings"].append(("delivery:topics", message))
        out["healthy"] = False
        out["detail"] = f"{head}; {message}"
        out["remedy"] = (
            f"In the ntfy app on the phone, subscribe to {', '.join(missing)} (lowercase) "
            f"on https://{host} - REMAINING-TASKS.md section 13 has the steps."
        )
        return out
    out["healthy"] = True
    out["detail"] = (
        f"{head}; the app's connection logged {_when(conn['closed_at'])} was subscribed to "
        f"{', '.join(topics)}"
    )
    return out


def _judge(out: dict, settings: dict, need: dict[str, str], now: float) -> dict:
    ntfy_log = str(settings["ntfy_log"])
    host = str(settings["app_host"])
    allowance = float(settings["alert_after_s"])
    scan = int(settings["scan_bytes"])
    try:
        newest = _newest_stats(Path(ntfy_log))
    except OSError as exc:
        return _unknown(out, f"{ntfy_log} is unreadable ({exc.strerror or exc})", ntfy_log)
    if newest is None:
        return _unknown(out, f"{ntfy_log} has no 'Server stats' line near its end", ntfy_log)
    stats_at, subscribers = newest
    out["stats_at"], out["subscribers"] = stats_at, subscribers
    if now - stats_at > float(settings["stats_max_age_s"]):
        return _unknown(
            out,
            f"ntfy's newest stats line is {_ago(now - stats_at)} old, and it writes one a minute - "
            "it is not running, or not logging",
            ntfy_log,
        )

    caddy_log = str(settings["caddy_log"])
    try:
        requests = _app_requests(Path(caddy_log), host, scan)
        unread = ""
    except OSError as exc:
        requests, unread = [], f"{caddy_log} is unreadable ({exc.strerror or exc})"
    streams = [r for r in requests if r["mode"] == "stream"]

    if subscribers > 0:
        out["state"], out["contact_at"] = CONNECTED, stats_at
        head = f"phone connected: ntfy counts {subscribers} subscription(s)"
        if not streams:
            why = unread or f"no connection of the app in the last {scan // (1024 * 1024)} MB of caddy.log"
            out["detail"] = f"{head}; which topics is not known - {why}"
            return out
        return _topics_verdict(out, streams[0], need, host, head, live_count=subscribers)

    out["state"] = DISCONNECTED
    last_seen, read_back_to = _last_subscribed(Path(ntfy_log), scan)
    seen = [t for t in (last_seen, requests[0]["closed_at"] if requests else None) if t]
    contact = max(seen) if seen else None
    out["contact_at"] = contact
    if contact is None or now - contact >= allowance:
        stops = _listing(list(dict.fromkeys(need.values()))) if need else "the pushes"
        if contact is None:
            out["read_back_to"] = read_back_to
            since = (f"at any point since {_when(read_back_to)}, as far back as ntfy.log was read"
                     if read_back_to else "anywhere in the part of ntfy.log that was read")
        else:
            since = f"since {_when(contact)} ({_ago(now - contact)})"
        message = f"no device has been connected to ntfy {since}: {stops} are not reaching the phone"
        out["findings"].append(("delivery:disconnected", message))
        out.update(healthy=False, detail=message, remedy=_REMEDY_SILENT)
        return out

    head = (f"no device connected right now; the phone was in contact {_ago(now - contact)} ago, "
            f"inside the {_ago(allowance)} allowance")
    if requests and requests[0]["mode"] == "stream":
        return _topics_verdict(out, requests[0], need, host, head, live_count=None)
    out["detail"] = f"{head}; which topics is not known - {'the app is polling' if requests else unread or 'only ntfy.log saw it'}"
    return out


def phone_state(cfg: Mapping[str, Any] | None = None, now: float | None = None) -> dict:
    """Everything a surface renders: state, evidence, findings, remedy.

    Never raises. Whatever stops the check is reported as UNKNOWN with its
    reason: a report that crashes on a missing log is one that stops being
    read, and one that skips it reads as healthy.
    """
    full = cfg if cfg is not None else store.load_config()
    settings = _cfg(full)
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {
        "checked_at": now, "state": UNKNOWN, "subscribers": None, "stats_at": None,
        "contact_at": None, "read_back_to": None, "app": None, "topics": None,
        "missing": [], "healthy": None, "findings": [], "detail": "", "remedy": "",
    }
    try:
        return _judge(out, settings, required_topics(full), now)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return _unknown(out, f"the check itself failed: {type(exc).__name__}: {exc}",
                        str(settings["ntfy_log"]))


def headline(state: Mapping[str, Any]) -> str:
    if state["state"] == UNKNOWN:
        return "COULD NOT CHECK"
    if state["findings"]:
        return "NOT RECEIVING"
    return "receiving" if state["healthy"] else "not yet known"


def push_note(cfg: Mapping[str, Any] | None = None) -> str:
    """What a push result line adds: whether anyone was there to receive it.

    "pushed" alone was the whole record of the 2026-09-15 morning report, which
    went to a server with no subscriber.
    """
    try:
        settings = _cfg(cfg)
        newest = _newest_stats(Path(str(settings["ntfy_log"])))
    except Exception as exc:  # noqa: BLE001 - a note must never fail the push it describes
        return f"; could not tell whether a device received it ({type(exc).__name__})"
    if newest is None or time.time() - newest[0] > float(settings["stats_max_age_s"]):
        return "; could not tell whether a device received it (no fresh ntfy stats line)"
    if newest[1] == 0:
        return "; ntfy had 0 subscribers - no device received it live, it waits in ntfy's cache"
    return f"; ntfy had {newest[1]} subscription(s)"


def _cli(argv: list[str]) -> int:
    state = phone_state()
    if "--json" in argv:
        print(json.dumps(state, indent=2))
    else:
        print(f"{headline(state)}: {state['detail']}")
        if state["remedy"]:
            print(f"  -> {state['remedy']}")
    if state["state"] == UNKNOWN:
        return 2
    return 0 if state["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
