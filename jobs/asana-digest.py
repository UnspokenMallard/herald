#!/Users/anzes/.herald/.venv/bin/python
"""Tasks assigned to Anze in Asana: who assigned each, and whether it is critical
by the rules in the user's CLAUDE.md. Read-only; nothing here writes to Asana.

Critical means any of: overdue, due within 48 hours, a priority field set to a
high value, a tag such as urgent or blocker, or an @mention of him in a comment
within the last 7 days. Each fact is listed, so a judgment can be checked.

Output is a file, never a notification: ~/.jarvis/reports/asana/latest.md and the
artifact ~/.herald/state/asana-digest.json (read by the job's artifact check).
Token: ~/.herald/secrets/asana.json, {"token": "<personal access token>"}.
Exit 0 with a summary; exit 1 when Asana cannot be read, so the job fails loudly.
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.herald/.venv/bin/python")

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
J = HOME / ".jarvis"
H = Path(os.environ.get("HERALD_HOME", HOME / ".herald"))
SECRETS = H / "secrets" / "asana.json"
REPORT = J / "reports" / "asana" / "latest.md"  # a report: Jarvis's digests read it
ARTIFACT = H / "state" / "asana-digest.json"
FIRST_CRITICAL = H / "state" / "asana-first-critical.json"
TASKS = H / "state" / "asana-tasks.json"  # the rows themselves, for the HUD's Today tab
STALE_AFTER = timedelta(days=3)


def write_atomic(path: Path, text: str) -> None:
    """Write to a temp file next to `path`, then rename it over: a reader (the HUD) or a
    run that overlaps this one never sees a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

API = "https://app.asana.com/api/1.0"
TASK_FIELDS = (
    "name,permalink_url,due_on,due_at,completed,"
    "custom_fields.name,custom_fields.display_value,tags.name,"
    "notes,memberships.project.name,memberships.section.name"
)
NOTES_MAX = 6000     # characters of a task's description kept for the HUD's Asana tab
COMMENTS_KEPT = 5    # newest human comments kept per task
HIGH_PRIORITY = {"high", "urgent", "critical", "highest", "blocker"}
URGENT_TAGS = {"urgent", "blocker", "high priority", "high-priority", "asap", "critical"}
SOON = timedelta(hours=48)
MENTION_WINDOW = timedelta(days=7)
MAX_TASKS = 150  # one stories call per task; keeps a run well under the job timeout


def token() -> str:
    doc = json.loads(SECRETS.read_text(encoding="utf-8"))
    value = str(doc.get("token") or "")
    if not value:
        raise RuntimeError(f"{SECRETS.name} has no token")
    return value


def get(path: str, tok: str, params: dict | None = None) -> dict:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        f"{API}{path}{query}",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data")


def due_moment(task: dict, now: datetime) -> datetime | None:
    """The deadline as an aware datetime: due_at when Asana has one, else the
    end of the due_on day in local time."""
    if task.get("due_at"):
        return datetime.fromisoformat(task["due_at"].replace("Z", "+00:00"))
    if task.get("due_on"):
        local_end = datetime.combine(date.fromisoformat(task["due_on"]), datetime.max.time())
        return local_end.astimezone()
    return None


def facts(task: dict, stories: list[dict], me_gid: str, now: datetime) -> dict:
    """Every fact the critical rules read, from one task and its stories."""
    due = due_moment(task, now)
    priority = next(
        (f.get("display_value") for f in task.get("custom_fields") or []
         if "priority" in (f.get("name") or "").lower() and f.get("display_value")),
        None,
    )
    tags = [t["name"] for t in task.get("tags") or [] if t.get("name")]
    mentioned = any(
        f'data-asana-gid="{me_gid}"' in (s.get("html_text") or "")
        and now - datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) <= MENTION_WINDOW
        for s in stories
        if s.get("resource_subtype") == "comment_added"
    )
    assigned = [s for s in stories if s.get("resource_subtype") == "assigned"]
    assigner = (assigned[-1].get("created_by") or {}).get("name") if assigned else None
    memberships = task.get("memberships") or []
    projects = list(dict.fromkeys(m["project"]["name"] for m in memberships if (m.get("project") or {}).get("name")))
    sections = list(dict.fromkeys(m["section"]["name"] for m in memberships if (m.get("section") or {}).get("name")))
    comments = [{"by": (s.get("created_by") or {}).get("name"), "at": s.get("created_at"), "text": (s.get("text") or "")[:1500]}
                for s in stories if s.get("resource_subtype") == "comment_added" and (s.get("text") or "").strip()]
    return {
        "gid": task.get("gid"),
        "projects": projects,
        "sections": sections,
        "notes": (task.get("notes") or "")[:NOTES_MAX],
        "comments": comments[-COMMENTS_KEPT:],
        "name": task.get("name") or "(untitled)",
        "url": task.get("permalink_url") or "",
        "due": due,
        "is_overdue": due is not None and due < now,
        "is_due_soon": due is not None and now <= due <= now + SOON,
        "priority": priority,
        "tags": tags,
        "is_mentioned": mentioned,
        "assigner": assigner,
    }


def critical_reasons(f: dict) -> list[str]:
    reasons = []
    if f["is_overdue"]:
        reasons.append("overdue")
    if f["is_due_soon"]:
        reasons.append("due within 48h")
    if f["priority"] and f["priority"].lower() in HIGH_PRIORITY:
        reasons.append(f"priority {f['priority']}")
    urgent = [t for t in f["tags"] if t.lower() in URGENT_TAGS]
    if urgent:
        reasons.append(f"tag {', '.join(urgent)}")
    if f["is_mentioned"]:
        reasons.append("@mentioned")
    return reasons


def collect(tok: str, me_gid: str, workspaces: list[dict], now: datetime) -> tuple[list[dict], int]:
    """The checked tasks, and how many open ones were past the cap."""
    rows: list[dict] = []
    skipped = 0
    for ws in workspaces:
        tasks = get("/tasks", tok, {
            "assignee": me_gid, "workspace": ws["gid"],
            "completed_since": "now", "opt_fields": TASK_FIELDS, "limit": 100,
        }) or []
        skipped += max(0, len(tasks) - MAX_TASKS)
        for task in tasks[:MAX_TASKS]:
            stories = get(f"/tasks/{task['gid']}/stories", tok, {
                "opt_fields": "resource_subtype,created_at,created_by.name,html_text,text",
            }) or []
            f = facts(task, stories, me_gid, now)
            f["workspace"] = ws["name"]
            f["reasons"] = critical_reasons(f)
            rows.append(f)
    rows.sort(key=lambda f: (not f["reasons"], f["due"] or datetime.max.replace(tzinfo=now.tzinfo)))
    return rows, skipped


def track_first_critical(rows: list[dict], now: datetime) -> dict[str, str]:
    """Remember when each task first became critical; forget tasks that stopped.

    Keyed by permalink (the name when there is none). Returns key -> ISO time.
    """
    try:
        known = json.loads(FIRST_CRITICAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    now_critical = {(f["url"] or f["name"]) for f in rows if f["reasons"]}
    updated = {k: v for k, v in known.items() if k in now_critical}
    for key in now_critical:
        updated.setdefault(key, now.isoformat(timespec="seconds"))
    FIRST_CRITICAL.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(FIRST_CRITICAL, json.dumps(updated, indent=2) + "\n")
    return updated


def render(rows: list[dict], skipped: int, now: datetime, first: dict[str, str]) -> str:
    critical = [f for f in rows if f["reasons"]]
    total = len(rows) + skipped
    lines = [
        f"# Asana assigned tasks - {now.astimezone().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"{len(critical)} critical of {len(rows)} checked. Rules: CLAUDE.md (Jarvis inbound, Asana).",
    ]
    if skipped:
        lines.append(f"{skipped} more open tasks past the {MAX_TASKS}-task cap were not checked ({total} open).")
    stale = []
    for f in critical:
        since = datetime.fromisoformat(first.get(f["url"] or f["name"], now.isoformat(timespec="seconds")))
        if now - since >= STALE_AFTER:
            stale.append((f, since))
    if stale:
        lines += ["", f"## Still critical after {STALE_AFTER.days} days", ""]
        for f, since in stale:
            lines.append(f"- {f['name']} ({f['workspace']}) - critical since {since.astimezone():%Y-%m-%d}; "
                         f"assigned by {f['assigner'] or 'unknown'}")
    lines.append("")
    for f in rows:
        mark = "CRITICAL" if f["reasons"] else "normal"
        due = f["due"].astimezone().strftime("%Y-%m-%d %H:%M") if f["due"] else "no due date"
        by = f["assigner"] or "assigner unknown"
        why = f"; {', '.join(f['reasons'])}" if f["reasons"] else ""
        lines.append(f"- **{mark}** {f['name']} ({f['workspace']}) - assigned by {by}; due {due}{why}")
        if f["url"]:
            lines.append(f"  {f['url']}")
    if not rows:
        lines.append("No open tasks assigned to you.")
    return "\n".join(lines) + "\n"


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        tok = token()
        me = get("/users/me", tok, {"opt_fields": "gid,name,workspaces.gid,workspaces.name"})
        rows, skipped = collect(tok, me["gid"], me.get("workspaces") or [], now)
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
        print(f"asana-digest: Asana not read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    first = track_first_critical(rows, now)
    write_atomic(REPORT, render(rows, skipped, now, first))
    write_atomic(ARTIFACT, json.dumps({
        "checked_at": now.isoformat(timespec="seconds"),
        "open": len(rows) + skipped,
        "checked": len(rows),
        "critical": sum(1 for f in rows if f["reasons"]),
        "critical_3d": sum(1 for k, v in first.items() if now - datetime.fromisoformat(v) >= STALE_AFTER),
    }, indent=2) + "\n")
    write_atomic(TASKS, json.dumps({
        "checked_at": now.isoformat(timespec="seconds"),
        "tasks": [{
            "gid": f["gid"], "name": f["name"], "url": f["url"], "workspace": f["workspace"], "assigner": f["assigner"],
            "due": f["due"].isoformat(timespec="minutes") if f["due"] else None, "reasons": f["reasons"],
            "projects": f["projects"], "sections": f["sections"], "priority": f["priority"], "tags": f["tags"],
            "notes": f["notes"], "comments": f["comments"],
        } for f in rows],
    }, indent=2) + "\n")
    print(f"asana-digest: {sum(1 for f in rows if f['reasons'])} critical of {len(rows)} checked, {skipped} past cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
