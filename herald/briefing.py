"""Briefings: Jarvis telling the user what came in, in its own voice -- the voice and the delivery.

Moved from Jarvis's daemon/briefing.py on 2026-10-17 (Herald). Asked for on 2026-10-01:

    "The notifications should look like a personal assistant named Jarvis is
    trying to get me up to date on what is going on. If there is just a new
    email with no pressing issues, or a new message that classifies as not
    urgent then let me know once. But if there is a new urgent problem then
    each notification should contain the issue till it's resolved. ...
    'Jarvis here, a new issue has come to our attention, there seems to be a
    bug with OAUTH'."

Jarvis judges each message (the brain calls), drafts replies, decides when a briefing is due and
whether the gates and the ladder (`publish-briefing`) allow it, and records what became of each
signal. **Herald says it**: the sentence frames, the opener, the cut to `max_bytes` (urgent lines are
never cut) and the POST to the `jarvis` topic. The brain writes the headlines only.

### Why this is not ping.py

The ping is counts-only by construction, and the 2026-09-13 rule stands: never loosen
`ping.is_content_free()` to carry content. This carries content -- a summary of someone's message and
where the draft is -- so it is its own module, topic and ladder action, on the same self-hosted ntfy
server as the reports (no upstream, no Firebase). While it is enabled in Jarvis the ping stops
counting and naming the signals announced here, or every mail would buzz twice. Asana assignments
keep their own one-line task ping.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import ping, store
from .store import log

_ADDRESS = re.compile(r"<[^<>]*>|[^\s<>]+@[^\s<>]+|https?://\S+")


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("briefing") or {})
    # Off unless config says otherwise: content leaving the machine is opt-in.
    section.setdefault("enabled", False)
    section.setdefault("base_url", "http://127.0.0.1:2586")
    section.setdefault("topic", "jarvis")
    section.setdefault("priority", 3)
    section.setdefault("urgent_priority", 4)
    section.setdefault("max_bytes", 3900)
    section.setdefault("timeout_s", 15)
    return section


def _where(row: Mapping[str, Any]) -> str:
    where = (row.get("meta") or {}).get("where")
    if where:
        return str(where)
    return {"github": "GitHub", "gmail": "mail", "mattermost": "Mattermost"}.get(row["source"], row["source"])


def clean_line(text: str, limit: int = 200) -> str:
    """One line, no address, no link, no control character."""
    text = _ADDRESS.sub("", str(text))
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split()).strip(" -:")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fallback(row: Mapping[str, Any]) -> str:
    """A headline from the signal itself, when the brain gave none."""
    meta = row.get("meta") or {}
    if row["source"] == "gmail" and meta.get("subject"):
        return clean_line(f"{meta['where']}: {meta['subject']}")
    return clean_line(row["title"])


def _draft_note(draft: str | None) -> str:
    if not draft:
        return ""
    if draft.startswith("saved in Gmail"):
        return " I left a reply draft in Gmail."
    if draft.startswith("saved as a Mattermost"):
        return " A reply draft is waiting in Mattermost."
    if "already had a draft" in draft:
        return " You already had a draft there, so I left it alone."
    return " I could not save a reply draft this time."


def _task_note(title: str | None) -> str:
    return f" I added a task: {clean_line(title, 120)}." if title else ""


def _line(group: list[dict]) -> str:
    row = group[-1]
    head = (row["headline"] or fallback(row)).rstrip(".")
    head = head[:1].upper() + head[1:]
    where = _where(row)
    if len(group) > 1:
        where += f", {len(group)} messages"
    return f"• {head} ({where}).{_draft_note(row['draft'])}{_task_note(row.get('task_title'))}"


def _groups(rows: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["thread"] or f"id:{row['id']}", []).append(row)
    return list(groups.values())


def compose(new: list[dict], reminders: list[dict], max_bytes: int = 3900) -> tuple[str, bool, str]:
    """(body, urgent, click URL). Jarvis's voice is here, in code; the brain
    writes the headlines only."""
    urgent_new = _groups([r for r in new if r["critical"] == 1])
    fyi = _groups([r for r in new if r["critical"] != 1])
    still = _groups(reminders)
    parts: list[str] = []
    if urgent_new:
        parts.append(f"{ping.opener()}, a new issue has come to our attention:" if len(urgent_new) == 1
                     else f"{ping.opener()}, {len(urgent_new)} new issues have come to our attention:")
        parts += [_line(g) for g in urgent_new]
        if still:
            parts += ["", "Still open, in case it slipped by:"] + [_line(g) for g in still]
    elif still:
        parts.append(f"{ping.opener()}, a friendly reminder - this still needs you:" if len(still) == 1
                     else f"{ping.opener()}, a friendly reminder - these still need you:")
        parts += [_line(g) for g in still]
    fyi_lines = [_line(g) for g in fyi]
    if fyi_lines:
        parts += (["", "Also, for your information:"] if parts
                  else [f"{ping.opener()}, just keeping you up to date:"])
    # Urgent lines are never cut. Information lines are, from the end, with a
    # pointer to where the rest is.
    body = "\n".join(parts)
    for kept in range(len(fyi_lines), -1, -1):
        tail = fyi_lines[:kept]
        if kept < len(fyi_lines):
            tail.append(f"…and {len(fyi_lines) - kept} more in the morning digest.")
        body = "\n".join(parts + tail)
        if len(body.encode("utf-8")) <= max_bytes:
            break
    first = (urgent_new or still or fyi or [[{"url": ""}]])[0][-1]
    return body, bool(urgent_new), str(first.get("url") or "")


def _post(settings: Mapping[str, Any], body: str, urgent: bool, click: str) -> bool:
    url = f"{str(settings['base_url']).rstrip('/')}/{settings['topic']}"
    headers = {
        "Title": "Jarvis",
        "Priority": str(settings["urgent_priority"] if urgent else settings["priority"]),
        "Tags": "rotating_light" if urgent else "speech_balloon",
        "Content-Type": "text/plain; charset=utf-8",
    }
    # HTTP headers are Latin-1 on the wire; a link that is not ASCII is left out.
    if click and click.isascii():
        headers["Click"] = click
    bearer = ping.token()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=int(settings["timeout_s"])) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("briefing: delivery failed: %s", exc)
        return False


def send(new: list[dict], reminders: list[dict], config: Mapping[str, Any] | None = None) -> dict:
    """Compose and publish one briefing from the rows Jarvis chose. Never raises.

    Returns `{"delivered": bool, "urgent": bool, "bytes": int, "reason": str}`.
    """
    settings = cfg(config)
    if not settings["enabled"]:
        return {"delivered": False, "urgent": False, "bytes": 0, "reason": "[briefing] disabled in Herald's config"}
    body, urgent, click = compose(new, reminders, int(settings["max_bytes"]))
    ok = _post(settings, body, urgent, click)
    return {"delivered": ok, "urgent": urgent, "bytes": len(body.encode("utf-8")),
            "reason": "" if ok else "ntfy did not accept it"}
