"""TickTick tasks from actionable signals: the one outward write Hermod makes besides drafts and the phone.

Added 2026-10-23 (Anze): when a Mattermost DM or mention, or a work mail, asks him to do something or
reports a bug for him to fix, Jarvis's triage judges it `actionable` and calls `POST /task`; the task lands
in the work list and the next briefing names it, so a false positive can be deleted. Creating a task is
the only thing this module does: it never completes, edits or deletes one.

One task per message, however often it is asked: a `hermod.task` record keyed like the signal
("<source>:<external_id>") is claimed with an insert-if-absent BEFORE the network call. It outlives the
signal's open row (90 days), so a re-poll, a restart, a cleared-and-returned signal or a second caller
cannot create a second task. A crash between claim and create leaves the claim and no task (a lost task,
never a duplicate); a failed create releases the claim so the next pass retries.

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
from ._mimir import records as mr
from .store import log

CREDS = Path(os.environ.get("TICKTICK_CREDS", Path.home() / ".config" / "ticktick-mcp" / "credentials.json"))
API = os.environ.get("TICKTICK_API", "https://api.ticktick.com/open/v1")
CLAIM = "claiming"
KIND, KEEP_DAYS = "hermod.task", 90


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("tasks") or {})
    section.setdefault("enabled", False)  # an outward write is opt-in, like the briefing
    section.setdefault("project_id", "6a33b2d40fdc3acea80bdb3d")  # the Work list
    section.setdefault("timeout_s", 15)
    return section


def content(row: Mapping[str, Any], note: str) -> str:
    """Who, where, when, the gist and the link. The message text itself stays in Hermod."""
    meta = row.get("meta") or {}
    when = datetime.fromtimestamp(row.get("external_ts") or row["first_seen"]).strftime("%Y-%m-%d")
    lines = [f"From {row.get('actor') or 'someone'}, {meta.get('where') or row['source']}, {when}."]
    if note.strip():
        lines += ["", note.strip()[:600]]
    if row.get("url"):
        lines += ["", str(row["url"])]
    return "\n".join(lines)


def addressed(row: Mapping[str, Any]) -> str:
    """"" when the message is addressed to Anze, else why not. Only a message that names him makes a task
    (Anze, 2026-10-23): a DM, a post that mentions him (the poller already keeps only those in channels), or
    mail with his address in To. A group message, mail he is only copied on or a list, and every other
    signal kind, get none, whatever the triage thought."""
    meta = row.get("meta") or {}
    kind = row.get("kind")
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


def create(signal_id: str, title: str, note: str = "", config: Mapping[str, Any] | None = None) -> dict:
    """`{"created": bool, "task_id", "reason"}`. Never raises for TickTick trouble."""
    settings = cfg(config)
    title = " ".join(str(title).split())[:200]
    if not settings["enabled"]:
        return {"created": False, "task_id": None, "reason": "tasks are off in Hermod's config"}
    if not title:
        return {"created": False, "task_id": None, "reason": "no title"}
    signal_id = str(signal_id)
    try:
        data = mr.get(inbound.SIGNAL, signal_id, source=store.SOURCE)
        if data is None:
            return {"created": False, "task_id": None, "reason": "no such signal"}
        row = {**data, "id": signal_id}
        why = addressed(row)
        if why:
            return {"created": False, "task_id": None, "reason": why}
        if not mr.put(store.SOURCE, KIND, signal_id, {"task_id": CLAIM}, ttl_days=KEEP_DAYS, if_absent=True, wait=True):
            done = mr.get(KIND, signal_id, source=store.SOURCE) or {}
            return {"created": False, "task_id": done.get("task_id"), "reason": "already has a task"}
    except mr.MimirDown as exc:
        return {"created": False, "task_id": None, "reason": f"Mimir is unreachable: {exc}"}
    try:
        task_id = _post(settings, {"title": title, "content": content(row, note), "projectId": settings["project_id"]})
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}" + (" (token expired: re-authenticate the TickTick MCP)" if exc.code == 401 else "")
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    else:
        mr.patch(store.SOURCE, KIND, signal_id, {"task_id": task_id, "task_title": title})
        mr.patch(store.SOURCE, inbound.SIGNAL, signal_id, {"task_id": task_id, "task_title": title})
        log.info("tasks: created %s for signal %s", task_id, signal_id)
        return {"created": True, "task_id": task_id, "reason": ""}
    mr.delete(store.SOURCE, KIND, signal_id)  # release the claim: the next pass retries
    log.warning("tasks: not created for signal %s: %s", signal_id, reason)
    return {"created": False, "task_id": None, "reason": reason}
