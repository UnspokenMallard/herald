#!/Users/anzes/.herald/.venv/bin/python
"""Open TickTick tasks of the Work, Personal and Jarvis projects, for the HUD's Tasks tab, its
standup draft and the morning checkpoint. Read-only: nothing here writes to TickTick (the HUD
writes only into the "Jarvis" project).

Uses the OAuth access token the TickTick MCP keeps in ~/.config/ticktick-mcp/credentials.json;
nothing is copied. Output: ~/.herald/state/ticktick-tasks.json, which the job's artifact check reads.
Exit 0 with a summary; exit 1 when TickTick cannot be read (a 401 means the MCP must re-authenticate),
so the job fails loudly and the file keeps the last good read.
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.herald/.venv/bin/python")

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
H = Path(os.environ.get("HERMOD_HOME", HOME / ".herald"))
CREDS = Path(os.environ.get("TICKTICK_CREDS", HOME / ".config" / "ticktick-mcp" / "credentials.json"))
ARTIFACT = H / "state" / "ticktick-tasks.json"
API = os.environ.get("TICKTICK_API", "https://api.ticktick.com/open/v1")
READ = {"work", "personal", "jarvis"}  # project names, emoji stripped, lower case


def write_atomic(path: Path, text: str) -> None:
    """A reader (the HUD) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def plain(name: str) -> str:
    """"💼Work" -> "work"."""
    return re.sub(r"[^\w ]", "", str(name)).replace("_", "").strip().lower()


def get(route: str, token: str):
    req = urllib.request.Request(API + route, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def collect(token: str) -> list[dict]:
    rows = []
    for p in get("/project", token):
        if p.get("closed") or plain(p["name"]) not in READ:
            continue
        for t in get(f"/project/{p['id']}/data", token).get("tasks") or []:
            if t.get("status"):  # 0 = active
                continue
            rows.append({
                "id": t["id"], "projectId": p["id"], "project": plain(p["name"]), "title": t.get("title") or "",
                "content": t.get("content") or "", "priority": t.get("priority") or 0, "due": t.get("dueDate"),
                "url": f"https://ticktick.com/webapp/#p/{p['id']}/tasks/{t['id']}",
            })
    return rows


def main() -> int:
    try:
        token = json.loads(CREDS.read_text(encoding="utf-8")).get("access_token")
        if not token:
            raise RuntimeError("no access token (re-authenticate the TickTick MCP)")
        rows = collect(token)
    except urllib.error.HTTPError as exc:
        hint = " (token expired: re-authenticate the TickTick MCP)" if exc.code == 401 else ""
        print(f"ticktick-tasks: TickTick not read: HTTP {exc.code}{hint}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
        print(f"ticktick-tasks: TickTick not read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    write_atomic(ARTIFACT, json.dumps({"checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": rows}, indent=2) + "\n")
    print(f"ticktick-tasks: {len(rows)} open task(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
