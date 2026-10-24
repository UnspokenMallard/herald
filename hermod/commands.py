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
  * One row per ntfy message (`message_id` unique), so a re-poll that sees the same message again is
    a no-op rather than a second execution: `snooze 6` twice is not `snooze 6`. A command is
    **claimed** before it is handed over (pending -> running), so a crash between the verb and its
    outcome leaves a `running` row to read, never a silent second run.
  * Anything older than `max_age_s` is read and recorded but not executed: a queued backlog
    delivered after an outage should not fire a burst of snoozes.
  * A poll that fails returns None, never `[]` (the same three-valued rule as every poller here).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import store
from .store import log

# The complete set. Adding to it is a security decision, not a convenience.
VERBS = ("status", "resolve", "snooze", "digest", "retry-triage", "run-deprehearsal")

# Never accepted. Not a filter the parser consults -- an unknown verb is already refused -- but a
# tripwire on this file's intent: adding one of these means deleting the list that says not to.
FORBIDDEN = (
    "dispatch", "approve", "accept", "merge", "push", "originate", "enable",
    "declare", "widen", "delete", "unregister", "restart", "flip", "spend",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL,
    received_at   REAL NOT NULL,
    sent_at       REAL,
    verb          TEXT,
    args          TEXT,
    outcome       TEXT NOT NULL,
    detail        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_commands_message ON commands(message_id);
CREATE INDEX IF NOT EXISTS ix_commands_received ON commands(received_at);
"""

# Outcomes Hermod writes itself; Jarvis's are done, failed, refused, no-match.
PENDING, RUNNING = "pending", "running"


def ensure_schema() -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)


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
            outcome: str, detail: str) -> int | None:
    with store.connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO commands(message_id, received_at, sent_at, verb, "
            "args, outcome, detail) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (message_id, time.time(), sent_at, verb, " ".join(args)[:500], outcome, detail[:500]),
        )
        return cur.lastrowid if cur.rowcount else None


def receive_message(message: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict:
    """Validate and record one message. Recorded *before* it is decided whether to act, so a refusal and
    a stale message leave the same evidence a success does. Returns `{"outcome", "verb", "id"}`:
    `pending` means Jarvis should run it; anything else is final."""
    ensure_schema()
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
    ensure_schema()
    messages = fetch(config)
    if messages is None:
        return {"state": "failed", "results": []}
    return {"state": "ok" if messages else "empty", "results": [receive_message(m, config) for m in messages]}


def claim(only: int | None = None) -> list[dict]:
    """Pending commands, marked running, oldest first: `{"id", "verb", "args": [..]}`. `only` claims one."""
    ensure_schema()
    with store.connect() as conn:
        rows = conn.execute("SELECT id, verb, args FROM commands WHERE outcome = ? AND (? IS NULL OR id = ?) "
                            "ORDER BY received_at, id", (PENDING, only, only)).fetchall()
        for row in rows:
            conn.execute("UPDATE commands SET outcome = ? WHERE id = ? AND outcome = ?", (RUNNING, row[0], PENDING))
    return [{"id": r[0], "verb": r[1], "args": (r[2] or "").split()} for r in rows]


def finish(command_id: int, outcome: str, detail: str = "") -> bool:
    """Jarvis's outcome for a command it ran. Only a `running` row can be finished."""
    ensure_schema()
    with store.connect() as conn:
        return conn.execute("UPDATE commands SET outcome = ?, detail = ? WHERE id = ? AND outcome = ?",
                            (outcome, detail[:500], int(command_id), RUNNING)).rowcount > 0


def recent(limit: int = 20) -> list[dict]:
    ensure_schema()
    cols = ("id", "message_id", "received_at", "verb", "args", "outcome", "detail")
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, message_id, received_at, verb, args, outcome, detail "
            "FROM commands ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]
