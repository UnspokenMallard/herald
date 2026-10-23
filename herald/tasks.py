"""TickTick tasks from actionable signals: the one outward write Herald makes besides drafts and the phone.

Added 2026-10-23 (Anze): when a Mattermost DM or mention, or a work mail, asks him to do something or
reports a bug for him to fix, Jarvis's triage judges it `actionable` and calls `POST /task`; the task lands
in the work list and the next briefing names it, so a false positive can be deleted. Creating a task is
the only thing this module does: it never completes, edits or deletes one.

One task per message, however often it is asked: `signals.task_id` is claimed with a compare-and-set
BEFORE the network call, and the claim is looked up across every row with the same (source, external_id),
so a re-poll, a restart, a cleared-and-returned row or a second caller cannot create a second task. A
crash between claim and create leaves the claim and no task (a lost task, never a duplicate); a failed
create releases the claim so the next pass retries.

Uses the OAuth access token the TickTick MCP keeps in ~/.config/ticktick-mcp/credentials.json (as
jobs/ticktick-tasks.py does); nothing is copied. A 401 means the MCP must re-authenticate.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import inbound, store
from .store import log

CREDS = Path(os.environ.get("TICKTICK_CREDS", Path.home() / ".config" / "ticktick-mcp" / "credentials.json"))
API = os.environ.get("TICKTICK_API", "https://api.ticktick.com/open/v1")
CLAIM = "claiming"


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("tasks") or {})
    section.setdefault("enabled", False)  # an outward write is opt-in, like the briefing
    section.setdefault("project_id", "6a33b2d40fdc3acea80bdb3d")  # the Work list
    section.setdefault("timeout_s", 15)
    return section


def content(row: Mapping[str, Any], note: str) -> str:
    """Who, where, when, the gist and the link. The message text itself stays in Herald."""
    meta = json.loads(row["meta"]) if row["meta"] else {}
    when = datetime.fromtimestamp(row["external_ts"] or row["first_seen"]).strftime("%Y-%m-%d")
    lines = [f"From {row['actor'] or 'someone'}, {meta.get('where') or row['source']}, {when}."]
    if note.strip():
        lines += ["", note.strip()[:600]]
    if row["url"]:
        lines += ["", str(row["url"])]
    return "\n".join(lines)


def addressed(row: Mapping[str, Any]) -> str:
    """"" when the message is addressed to Anze, else why not. Only a message that names him makes a task
    (Anze, 2026-10-23): a DM, a post that mentions him (the poller already keeps only those in channels), or
    mail with his address in To. A group message, mail he is only copied on or a list, and every other
    signal kind, get none, whatever the triage thought."""
    meta = json.loads(row["meta"]) if row["meta"] else {}
    kind = row["kind"]
    if kind == "mattermost-mention" or (kind == "mattermost-dm" and meta.get("channel_type") == "D"):
        return ""
    if kind == "mail":
        mine = str((store.secret("gmail") or {}).get("user") or "").lower()
        if mine and mine in str(meta.get("to") or "").lower():
            return ""
        return "not addressed to Anze (his address is not in To)"
    return f"{kind or 'this signal'} is not addressed to Anze"


def _post(settings: Mapping[str, Any], payload: dict) -> str:
    token = json.loads(CREDS.read_text(encoding="utf-8")).get("access_token")
    if not token:
        raise RuntimeError("no TickTick access token (re-authenticate the TickTick MCP)")
    request = urllib.request.Request(
        API + "/task", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=float(settings["timeout_s"])) as resp:
        return str(json.load(resp)["id"])


def create(signal_id: int, title: str, note: str = "", config: Mapping[str, Any] | None = None) -> dict:
    """`{"created": bool, "task_id", "reason"}`. Never raises for TickTick trouble."""
    settings = cfg(config)
    title = " ".join(str(title).split())[:200]
    if not settings["enabled"]:
        return {"created": False, "task_id": None, "reason": "tasks are off in Herald's config"}
    if not title:
        return {"created": False, "task_id": None, "reason": "no title"}
    inbound.ensure_schema()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id, source, external_id, actor, url, meta, external_ts, first_seen, kind FROM signals WHERE id = ?",
            (int(signal_id),)).fetchone()
        if row is None:
            return {"created": False, "task_id": None, "reason": "no such signal"}
        keys = ("id", "source", "external_id", "actor", "url", "meta", "external_ts", "first_seen", "kind")
        row = dict(zip(keys, row))
        why = addressed(row)
        if why:
            return {"created": False, "task_id": None, "reason": why}
        done = conn.execute(
            "SELECT task_id FROM signals WHERE source = ? AND external_id = ? AND task_id IS NOT NULL LIMIT 1",
            (row["source"], row["external_id"])).fetchone()
        if done:
            return {"created": False, "task_id": done[0], "reason": "already has a task"}
        claimed = conn.execute("UPDATE signals SET task_id = ? WHERE id = ? AND task_id IS NULL",
                               (CLAIM, row["id"])).rowcount
    if not claimed:
        return {"created": False, "task_id": None, "reason": "already has a task"}
    try:
        task_id = _post(settings, {"title": title, "content": content(row, note), "projectId": settings["project_id"]})
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}" + (" (token expired: re-authenticate the TickTick MCP)" if exc.code == 401 else "")
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    else:
        with store.connect() as conn:
            conn.execute("UPDATE signals SET task_id = ?, task_title = ? WHERE id = ?", (task_id, title, row["id"]))
        log.info("tasks: created %s for signal %s", task_id, row["id"])
        return {"created": True, "task_id": task_id, "reason": ""}
    with store.connect() as conn:  # release the claim: the next pass retries
        conn.execute("UPDATE signals SET task_id = NULL WHERE id = ? AND task_id = ?", (row["id"], CLAIM))
    log.warning("tasks: not created for signal %s: %s", row["id"], reason)
    return {"created": False, "task_id": None, "reason": reason}
