"""The phone -> machine command channel, transport only: receive, validate, record, hand over.

Moved from Jarvis's daemon/command.py on 2026-10-17 (Hermod). Everything else here points outward;
this is the one channel back, and `AUTONOMY-LADDER-DESIGN.md` §5 (Jarvis) rules out the obvious
shortcut, a file the daemon reads back as a yes. This channel exists inside that rule rather than
around it, and it grants almost nothing.

The split: **Hermod receives and validates**; **Jarvis executes the verb under its own ladder** and
reports the outcome back (`finish`). Hermod decides nothing about what a verb does.

  * The verb set is closed (`VERBS`). An unknown verb is recorded `unknown` and never handed to
    Jarvis. Adding a verb is a security decision, not a convenience; Jarvis's selftest asserts this
    table against a list of granting words (`FORBIDDEN`) and that only the two run verbs start
    anything. A future edit that adds `approve` or `dispatch` here fails the suite rather than
    shipping.
  * Whoever can publish to the command topic can make Jarvis tell them counts, close an incident,
    mute a push, rebuild a report, or start one of two fixed jobs. They cannot make it write code,
    commit, push, or choose what runs. The topic is separate from the ping topic and carries its own
    token, but the design does not rely on that token holding.
  * One Mimir record per ntfy message (kind `hermod.command`, key = the message id, which is also the
    command's `id` for Jarvis), so a re-poll that sees the same message again is
    a no-op rather than a second execution: `snooze 6` twice is not `snooze 6`. A command is
    **claimed** before it is handed over (pending -> running, a compare-and-set), so a crash between the verb and its
    outcome leaves a `running` row to read, never a silent second run.
  * Anything older than `max_age_s` is read and recorded but not executed: a queued backlog
    delivered after an outage should not fire a burst of snoozes.
  * A poll that fails returns None, never `[]` (the same three-valued rule as every poller here).
  * These writes go to Mimir at once and are never queued in the outbox: a command recorded late would
    run late. With Mimir down the poll reports `failed` and the message is read again next time, while it
    is younger than `max_age_s`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import store
from ._mimir import records as mr
from .store import log

# The complete set. Adding to it is a security decision, not a convenience.
VERBS = ("status", "resolve", "snooze", "digest", "retry-triage", "run-deprehearsal")

# Never accepted. Not a filter the parser consults -- an unknown verb is already refused -- but a
# tripwire on this file's intent: adding one of these means deleting the list that says not to.
FORBIDDEN = (
    "dispatch", "approve", "accept", "merge", "push", "originate", "enable",
    "declare", "widen", "delete", "unregister", "restart", "flip", "spend",
)

KIND = "hermod.command"

# Outcomes Hermod writes itself; Jarvis's are done, failed, refused, no-match.
PENDING, RUNNING = "pending", "running"


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("command") or {})
    section.setdefault("enabled", False)
    section.setdefault("base_url", "http://127.0.0.1:2586")
    # A separate topic from the ping's. Publishing rights to "tell me the
    # counts" and to "close an incident" are not the same right.
    section.setdefault("topic", "jarvis-cmd")
    section.setdefault("timeout_s", 15)
    section.setdefault("max_messages", 20)
    section.setdefault("max_age_s", 3600)
    return section


def token() -> str:
    """Bearer for the command topic. `command_token`, else the ping's token. `secrets/ntfy.json` only,
    no config key, because config.toml is committed."""
    doc = store.secret("ntfy")
    return str((doc or {}).get("command_token") or (doc or {}).get("token") or "")


def fetch(config: Mapping[str, Any] | None = None) -> list[dict] | None:
    """Messages waiting on the command topic, or None if the poll failed.

    **None, never `[]`.** An empty list is a claim that nothing was said, and a broken transport is
    not entitled to make that claim.
    """
    settings = cfg(config)
    since = int(max(0.0, time.time() - float(settings["max_age_s"])))
    url = (f"{str(settings['base_url']).rstrip('/')}/{settings['topic']}"
           f"/json?poll=1&since={since}")
    request = urllib.request.Request(url, method="GET")
    bearer = token()
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=int(settings["timeout_s"])) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("command: poll failed: %s", exc)
        return None

    messages = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if doc.get("event") != "message":
            continue  # ntfy also emits open/keepalive frames
        messages.append(doc)
    return messages[-int(settings["max_messages"]):]


def parse(text: str) -> tuple[str, list[str]] | None:
    """`"resolve loop-crash it stopped"` -> `("resolve", ["loop-crash", ...])`.

    None for anything that is not one of `VERBS`. An unknown verb is not an error to be corrected
    into the nearest match: the whole point is that the accepted set is small and closed, and a fuzzy
    match is how `resolve` becomes `restart` on a phone keyboard.
    """
    words = (text or "").strip().split()
    if not words:
        return None
    verb = words[0].lower()
    if verb not in VERBS:
        return None
    return verb, words[1:]


def _record(message_id: str, sent_at: float | None, verb: str | None, args: list[str],
            outcome: str, detail: str) -> str | None:
    """The message id when this call recorded it, None when it was recorded before."""
    now = time.time()
    data = mr.scrub({"message_id": message_id, "received_at": now, "sent_at": sent_at, "verb": verb,
                     "args": " ".join(args)[:500], "outcome": outcome, "detail": detail[:500]})
    return message_id if mr.put(store.SOURCE, KIND, message_id, data, at=now, if_absent=True, wait=True) else None


def receive_message(message: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict:
    """Validate and record one message. Recorded *before* it is decided whether to act, so a refusal and
    a stale message leave the same evidence a success does. Returns `{"outcome", "verb", "id"}`:
    `pending` means Jarvis should run it; anything else is final."""
    settings = cfg(config)
    message_id = str(message.get("id") or "")
    text = str(message.get("message") or "")
    sent_at = float(message.get("time") or 0) or None

    if not message_id:
        return {"outcome": "refused", "verb": None, "id": None, "detail": "message has no id"}
    parsed = parse(text)
    if parsed is None:
        row = _record(message_id, sent_at, None, [], "unknown", f"not a command: {text[:120]!r}")
        return {"outcome": "duplicate" if row is None else "unknown", "verb": None, "id": row,
                "detail": "unknown verb"}
    verb, args = parsed
    if sent_at and (time.time() - sent_at) > float(settings["max_age_s"]):
        row = _record(message_id, sent_at, verb, args, "stale", "older than max_age_s, recorded but not executed")
        return {"outcome": "duplicate" if row is None else "stale", "verb": verb, "id": row,
                "detail": "too old to execute"}
    row = _record(message_id, sent_at, verb, args, PENDING, "")
    return {"outcome": "duplicate" if row is None else PENDING, "verb": verb, "id": row, "detail": ""}


def receive(config: Mapping[str, Any] | None = None) -> dict:
    """Fetch the topic and record what is on it. `{"state": ..., "results": [...]}`; `state` says
    "nothing was said" apart from "could not ask", because those look identical in a job log and only one
    of them is fine."""
    settings = cfg(config)
    if not settings["enabled"]:
        return {"state": "disabled", "results": []}
    messages = fetch(config)
    if messages is None:
        return {"state": "failed", "results": []}
    try:
        results = [receive_message(m, config) for m in messages]
    except mr.MimirDown as exc:
        log.warning("command: Mimir is unreachable, %d message(s) not recorded: %s", len(messages), exc)
        return {"state": "failed", "results": []}
    return {"state": "ok" if messages else "empty", "results": results}


def _all() -> list[dict]:
    return [{**r["data"], "id": r["key"]} for r in mr.query(kind=KIND, order="desc", limit=500, source=store.SOURCE)]


def claim(only: str | None = None) -> list[dict]:
    """Pending commands, marked running, oldest first: `{"id", "verb", "args": [..]}`. `only` claims one.
    A command another caller claimed first is left out."""
    rows = sorted((c for c in _all() if c["outcome"] == PENDING and only in (None, c["id"])),
                  key=lambda c: (c["received_at"], c["id"]))
    won = [c for c in rows if mr.patch(store.SOURCE, KIND, c["id"], {"outcome": RUNNING}, if_={"outcome": PENDING}, wait=True)]
    return [{"id": c["id"], "verb": c["verb"], "args": (c["args"] or "").split()} for c in won]


def finish(command_id: str, outcome: str, detail: str = "") -> bool:
    """Jarvis's outcome for a command it ran. Only a `running` row can be finished."""
    return bool(mr.patch(store.SOURCE, KIND, str(command_id), mr.scrub({"outcome": outcome, "detail": detail[:500]}),
                         if_={"outcome": RUNNING}, wait=True))


def recent(limit: int = 20) -> list[dict]:
    cols = ("id", "message_id", "received_at", "verb", "args", "outcome", "detail")
    rows = sorted(_all(), key=lambda c: c["received_at"], reverse=True)[:limit]
    return [{c: row.get(c) for c in cols} for row in rows]
