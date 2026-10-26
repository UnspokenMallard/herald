#!/Users/anzes/.herald/.venv/bin/python
"""Anze's messages of the last 30 days, for Mimir's second-brain ingest. Read-only: Mattermost through the
GET-only client (DMs, group messages and @mentions in every channel not listed in `mattermost_exclude`), mail
through a readonly IMAP select and BODY.PEEK (INBOX, read or not). Nothing is marked read, posted or drafted.

Output: ~/.herald/state/messages-feed.json, the contract Mimir's `ingest_sources.messages` reads:
  {"fetched_at": epoch, "messages": [
     {"source": "mattermost", "id", "channel", "channel_type": "D|G|O|P", "author", "ts", "text", "mention", "bot"},
     {"source": "mail", "id", "from", "subject", "ts", "text", "bulk"}]}
The file accumulates: each run fetches from just before the last run, merges by id and drops what is older than
30 days. Exit 0 with a summary; exit 1 when a source cannot be read, so the file keeps the last good read.
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.herald/.venv/bin/python")

import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hermod import inbound, mattermost, store  # noqa: E402

WINDOW_S = 30 * 86400
OVERLAP_S = 3600  # refetch the last hour: a post edited or a mail that landed while the last run read
MAX_POSTS_PER_CHANNEL = 500
MAX_MAIL = 300
TEXT_CHARS = 3000
BULK_SENDER = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|newsletter|bounce", re.I)
H = store.ROOT
ARTIFACT = store.STATE / "messages-feed.json"


def write_atomic(path: Path, text: str) -> None:
    """A reader (Mimir) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def fetch_mattermost(mm, since_ms: int, exclude: list) -> list:
    """Posts newer than since_ms: every DM and group message, and channel posts that @mention the user."""
    me = mm.get("/users/me")
    teams = mm.get("/users/me/teams") or []
    channels: dict = {}
    for team in teams:  # DMs and group messages come back once per team; the dict dedupes them
        for ch in mm.get(f"/users/me/teams/{team['id']}/channels") or []:
            channels[ch["id"]] = ch
    team_name = {t["id"]: t["name"] for t in teams}
    skip = {e.lower() for e in exclude}
    pattern = mattermost.mention_pattern(me)
    found: list = []
    for ch in channels.values():
        team = team_name.get(ch.get("team_id") or "", "")
        if mattermost._excluded(ch, team, skip) or int(ch.get("last_post_at") or 0) <= since_ms:
            continue
        direct = ch.get("type") in ("D", "G")
        listing = mm.get(f"/channels/{ch['id']}/posts?since={since_ms}") or {}
        posts = [p for p in (listing.get("posts") or {}).values()
                 if int(p.get("create_at") or 0) > since_ms and not p.get("delete_at")
                 and not str(p.get("type") or "").startswith("system_") and str(p.get("message") or "").strip()]
        if not direct:
            posts = [p for p in posts if pattern.search(str(p["message"]))]
        posts.sort(key=lambda p: int(p["create_at"]))
        found += [(ch, p) for p in posts[-MAX_POSTS_PER_CHANNEL:]]
    if not found:
        return []
    ids = sorted({p["user_id"] for _, p in found} | {u for ch, _ in found if ch.get("type") == "D"
                                                       for u in ch.get("name", "").split("__")})
    users = {u["id"]: u for u in mm.post("/users/ids", ids) or []}

    def name(uid: str) -> str:
        return users.get(uid, {}).get("username") or "someone"

    out = []
    for ch, p in found:
        kind = ch.get("type") or "O"
        if kind == "D":
            other = [u for u in ch.get("name", "").split("__") if u != me["id"]]
            label = f"dm:@{name(other[0] if other else me['id'])}"
        elif kind == "G":
            label = f"group:{ch.get('display_name') or ch.get('name')}"
        else:
            label = str(ch.get("display_name") or ch.get("name"))
        user = users.get(p["user_id"], {})
        props = p.get("props") or {}
        out.append({"source": "mattermost", "id": p["id"], "channel": label, "channel_type": kind,
                    "author": name(p["user_id"]), "ts": int(p["create_at"]) / 1000, "text": str(p["message"])[:4000],
                    "mention": kind not in ("D", "G"),  # only mention posts get past the filter above
                    "bot": bool(user.get("is_bot") or props.get("from_bot") or props.get("from_webhook"))})
    return out


def is_bulk(msg) -> bool:
    return bool(msg.get("List-Unsubscribe") or msg.get("List-Id") or BULK_SENDER.search(msg.get("From", ""))
                or str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
                or str(msg.get("Auto-Submitted", "no")).lower() != "no")


def decoded(value: str) -> str:
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001 - a broken header is not a failed run
        return value or ""


def fetch_mail(settings: dict, creds: dict, since: float, known: set) -> list:
    """New INBOX mail since `since`, read or not. Bodies only for non-bulk mail (bulk keeps the subject)."""
    conn = imaplib.IMAP4_SSL(str(settings["gmail_host"]), timeout=int(settings["timeout_s"]))
    try:
        conn.login(str(creds["user"]), str(creds["app_password"]))
        conn.select(str(settings["gmail_mailbox"]), readonly=True)  # readonly: SELECT must not clear \Recent
        typ, data = conn.search(None, "SINCE", datetime.fromtimestamp(since, timezone.utc).strftime("%d-%b-%Y"))
        if typ != "OK":
            raise RuntimeError(f"imap search failed: {typ}")
        out = []
        for num in (data[0] or b"").split()[-MAX_MAIL:]:
            n = num.decode("ascii")
            # BODY.PEEK, never RFC822: a plain fetch sets \Seen and would mark the mail read.
            typ, head = conn.fetch(n, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-ID PRECEDENCE AUTO-SUBMITTED)])")
            if typ != "OK" or not head or not isinstance(head[0], tuple):
                continue
            msg = email.message_from_bytes(head[0][1])
            mid = decoded(msg.get("Message-ID", "")).strip() or f"seq:{n}"
            when = email.utils.parsedate_to_datetime(msg["Date"]).timestamp() if msg.get("Date") else None
            if when is None or when < since or mid in known:
                continue  # known mail keeps its stored row, body included
            row = {"source": "mail", "id": mid, "from": decoded(msg.get("From", ""))[:200], "subject": decoded(msg.get("Subject", ""))[:300],
                   "ts": when, "bulk": is_bulk(msg)}
            row["text"] = row["subject"]
            if not row["bulk"]:
                typ, full = conn.fetch(n, "(BODY.PEEK[])")
                if typ == "OK" and full and isinstance(full[0], tuple):
                    row["text"] = inbound.mail_text(email.message_from_bytes(full[0][1]), TEXT_CHARS) or row["subject"]
            out.append(row)
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def merge(old: list, new: list, floor: float) -> list:
    """Old and new by (source, id), the new row winning, nothing older than floor, oldest first."""
    rows = {(m["source"], m["id"]): m for m in old if m.get("ts", 0) >= floor}
    rows.update({(m["source"], m["id"]): m for m in new if m.get("ts", 0) >= floor})
    return sorted(rows.values(), key=lambda m: m["ts"])


def main() -> int:
    now = time.time()
    try:
        previous = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    old = previous.get("messages") or []
    floor = now - WINDOW_S
    since = max(floor, float(previous.get("fetched_at") or 0) - OVERLAP_S)
    section = inbound.cfg()
    new: list = []
    failed: list = []
    mm = mattermost.client({"inbound": section}) if section.get("mattermost_enabled") else None
    if mm is not None:
        try:
            new += fetch_mattermost(mm, int(since * 1000), list(section.get("mattermost_exclude") or []))
        except Exception as exc:  # noqa: BLE001 - any failure is "cannot tell", not "empty"
            failed.append(f"mattermost: {exc}")
    creds = inbound.gmail_credentials() if section.get("gmail_enabled") else None
    if creds is not None:
        try:
            known = {m["id"] for m in old if m.get("source") == "mail"}
            new += fetch_mail(section, creds, since, known)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"mail: {exc}")
    if failed:
        print("messages-feed: " + "; ".join(failed), file=sys.stderr)
        return 1  # the file keeps the last good read
    rows = merge(old, new, floor)
    write_atomic(ARTIFACT, json.dumps({"fetched_at": now, "messages": rows}, ensure_ascii=False))
    print(f"messages-feed: {len(new)} fetched, {len(rows)} kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
