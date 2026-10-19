"""Inbound signals: the things waiting on the user that are not on this machine.

Moved from Jarvis's daemon/inbound.py on 2026-10-17 (Herald): the pollers, the secrets they read, the
`signals` table (state/herald.db) and what clears a row. What stays in Jarvis is judgement -- is a task
critical, does a message need a reply, what to say -- which the brain and the local model do and which
Jarvis writes back with `update()`. The text below is the original.

Every event source this daemon has ever had is local -- the spool, three watched
directories, its own job table, its own repositories. That is a complete picture
of what *the daemon* is doing and no picture at all of what *the user* is being
asked for. A review request sitting on GitHub for four days is invisible here,
and no amount of better local reporting will find it.

This module is the read-only half of fixing that: pollers that fetch "what is
waiting on you" from an outside service and record it, so the one question the
morning digest answers -- **what needs me** -- covers more than this box.

### Signals are not events

The obvious wiring is to enqueue each notification as an event. Wrong shape: the
queue is a work queue with retries, dedup, a worker and a stuck detector, sized
for a handful of jobs an hour. A busy GitHub day would put a hundred rows
through it that no handler can act on, and drown the detectors that exist to
notice a wedged worker.

A signal is a *fact about the outside world*, not a unit of work. It gets its
own table and no handler.

### They clear by disappearing, and only on a successful poll

Same principle as `review_items` clearing on git and `incidents` closing on a
resolver: **observe, never ask**. Read the notification on GitHub and the next
poll no longer returns it, so it clears here. Nothing to tick, and this daemon
never has state the user is expected to maintain by hand.

The dangerous edge is obvious once stated: a poll that fails and returns nothing
looks exactly like a poll that found nothing, and would clear every open signal
at once. So a source returns `None` for "could not fetch" and a list for "this
is the complete current set", and **clearing only ever happens on a list**.
Third time this rule appears in this codebase; it is the one that keeps being
worth writing down.

### Nothing here acts

Pollers read. They do not reply, close, merge, archive, mark as read, or write
anything to the service they poll. Every source runs on a read-scoped
credential where the service offers one. That keeps this whole module tier 0
under the autonomy ladder -- it writes only under `~/.jarvis` -- and means the
worst failure available to it is a stale row.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from . import mattermost, store
from .store import log

# source name -> async fetcher. Returns the COMPLETE current set of open
# signals, or None if it could not fetch. A list cut short by a limit is returned as a
# `store.Partial`: a plain partial list is indistinguishable from things having been dealt with.
Fetcher = Callable[[Mapping[str, Any]], Awaitable["list[dict] | None"]]
SOURCES: dict[str, Fetcher] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    kind         TEXT,
    title        TEXT NOT NULL,
    url          TEXT,
    actor        TEXT,
    external_ts  REAL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    cleared_at   REAL,
    cleared_by   TEXT,
    project      TEXT,
    task_gid     TEXT,
    critical     INTEGER,
    notified_at  REAL,
    body         TEXT,
    meta         TEXT,
    thread       TEXT,
    needs_reply  INTEGER,
    lang         TEXT,
    headline     TEXT,
    draft        TEXT,
    reminded_at  REAL
);
-- One OPEN signal per (source, external_id). Cleared rows are unconstrained, so
-- a notification that comes back is a new row and the history of both is kept.
CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_open
    ON signals(source, external_id) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_signals_first_seen ON signals(first_seen);
"""

# The columns Jarvis may write back after judging a signal. `critical` is NULL until judged
# (the brain's "urgent" for mail and chat too), `notified_at` NULL until announced, `headline` is the
# brain's one-line summary, `thread` groups posts judged together, `draft` is what became of the
# reply draft, `reminded_at` the last briefing that repeated it. The row's identity, its text and
# `cleared_*` are never Jarvis's to change.
JUDGEMENT_COLUMNS = ("critical", "notified_at", "thread", "needs_reply", "lang", "headline", "draft",
                     "reminded_at")


def ensure_schema() -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("inbound") or {})
    section.setdefault("enabled", True)
    # Off individually rather than by deleting the source, so turning one off is
    # a config edit that stays in git next to the reason.
    section.setdefault("github_enabled", True)
    # Measured 2026-09-05 on the first live poll: `/notifications` returned
    # **327** rows, and all but a handful were `subscribed` -- activity on
    # threads in a watched repository, which is news, not a thing waiting on
    # anyone. Feeding that into a "what needs me" report would have destroyed
    # the report on day one.
    #
    # These four are the reasons where GitHub is saying a *person asked for the
    # user specifically*. Widen it deliberately, in config, next to a reason --
    # never because a source looks quiet.
    section.setdefault(
        "github_reasons", ["review_requested", "assign", "mention", "team_mention"]
    )
    section.setdefault("gmail_enabled", False)
    section.setdefault("gmail_host", "imap.gmail.com")
    section.setdefault("gmail_mailbox", "INBOX")
    # UNSEEN alone would make every unread newsletter a thing "waiting on you".
    # The default is the narrow one; widen it deliberately.
    section.setdefault("gmail_search", "UNSEEN")
    section.setdefault("gmail_max", 25)
    # Mattermost (2026-10-01): DMs, group messages and posts that mention the
    # user. Off until configured, like Gmail.
    section.setdefault("mattermost_enabled", False)
    section.setdefault("mattermost_url", "")
    section.setdefault("mattermost_exclude", [])
    section.setdefault("mattermost_broadcast_mentions", False)
    section.setdefault("mattermost_max_per_channel", 30)
    section.setdefault("timeout_s", 60)
    return section

def source(name: str) -> Callable[[Fetcher], Fetcher]:
    def wrap(fn: Fetcher) -> Fetcher:
        SOURCES[name] = fn
        return fn

    return wrap


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def record(source_name: str, items: list[dict]) -> tuple[int, int]:
    """Upsert the current set and clear anything no longer in it.

    Returns `(new, cleared)`. Only ever called with a real fetch result -- see
    `poll_one`, which refuses to call it on a failure. A `store.Partial` (a truncated fetch) is
    upserted but clears nothing: the signals it does not name may simply be past the limit.
    """
    ensure_schema()
    now = time.time()
    seen_ids = {str(i["external_id"]) for i in items}
    new = 0

    with store.connect() as conn:
        for item in items:
            ext = str(item["external_id"])
            row = conn.execute(
                "SELECT id FROM signals WHERE source = ? AND external_id = ? "
                "AND cleared_at IS NULL",
                (source_name, ext),
            ).fetchone()
            if row:
                # kind and project too: a parser that learns to read a message
                # (Asana, 2026-09-29) re-labels the rows already open.
                conn.execute(
                    "UPDATE signals SET last_seen = ?, title = ?, url = ?, kind = ?, "
                    "project = ?, task_gid = ? WHERE id = ?",
                    (now, str(item.get("title", ""))[:300], item.get("url"), item.get("kind"),
                     item.get("project"), item.get("task_gid"), row[0]),
                )
                continue
            meta = item.get("meta")
            conn.execute(
                "INSERT INTO signals(source, external_id, kind, title, url, actor, "
                "external_ts, first_seen, last_seen, project, task_gid, body, meta) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_name, ext, item.get("kind"), str(item.get("title", ""))[:300],
                 item.get("url"), item.get("actor"), item.get("external_ts"), now, now,
                 item.get("project"), item.get("task_gid"), item.get("body"),
                 json.dumps(meta) if meta is not None else None),
            )
            new += 1

        open_rows = conn.execute(
            "SELECT id, external_id FROM signals WHERE source = ? AND cleared_at IS NULL",
            (source_name,),
        ).fetchall()
        gone = [] if isinstance(items, store.Partial) else [r[0] for r in open_rows if r[1] not in seen_ids]
        for signal_id in gone:
            # The message text goes with the signal: nothing reads it once the
            # user has seen it, and the row is kept for 30 days.
            conn.execute(
                "UPDATE signals SET cleared_at = ?, cleared_by = 'gone', body = NULL, meta = NULL "
                "WHERE id = ?",
                (now, signal_id),
            )
    return new, len(gone)


def waiting(source_name: str | None = None) -> list[dict]:
    ensure_schema()
    sql = ("SELECT id, source, external_id, kind, title, url, actor, external_ts, "
           "first_seen, last_seen, project, critical FROM signals WHERE cleared_at IS NULL")
    args: list[Any] = []
    if source_name:
        sql += " AND source = ?"
        args.append(source_name)
    sql += " ORDER BY first_seen"
    cols = ("id", "source", "external_id", "kind", "title", "url", "actor",
            "external_ts", "first_seen", "last_seen", "project", "critical")
    with store.connect() as conn:
        return [dict(zip(cols, r)) for r in conn.execute(sql, args).fetchall()]


def prune(retain_days: int = 30) -> int:
    """Cleared signals only. An open one survives at any age."""
    ensure_schema()
    cutoff = time.time() - retain_days * 86400
    with store.connect() as conn:
        return conn.execute(
            "DELETE FROM signals WHERE cleared_at IS NOT NULL AND cleared_at < ?",
            (cutoff,),
        ).rowcount

def open_rows() -> list[dict]:
    """Every open signal with the text and meta the briefing reads, `meta` parsed, Asana tasks left out
    (they have their own ping)."""
    ensure_schema()
    cols = ("id", "source", "kind", "title", "url", "actor", "external_ts", "first_seen", "body", "meta",
            "thread", "critical", "needs_reply", "lang", "headline", "draft", "notified_at", "reminded_at")
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM signals WHERE cleared_at IS NULL "
            "AND (kind IS NULL OR kind != 'asana-task') ORDER BY first_seen, id").fetchall()
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        try:
            item["meta"] = json.loads(item["meta"]) if item["meta"] else {}
        except ValueError:
            item["meta"] = {}
        out.append(item)
    return out


def pending_tasks() -> list[dict]:
    """Open Asana tasks whose per-task ping has not gone out."""
    ensure_schema()
    cols = ("id", "title", "project", "task_gid", "critical")
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, project, task_gid, critical FROM signals "
            "WHERE kind = 'asana-task' AND cleared_at IS NULL AND notified_at IS NULL "
            "ORDER BY first_seen"
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def update(ids: list[int], **fields: Any) -> int:
    """Write Jarvis's judgement onto signals. Only `JUDGEMENT_COLUMNS`; anything else is refused."""
    bad = set(fields) - set(JUDGEMENT_COLUMNS)
    if bad:
        raise ValueError(f"not a judgement column: {sorted(bad)}")
    if not fields or not ids:
        return 0
    sets = ", ".join(f"{name} = ?" for name in fields)
    ensure_schema()
    with store.connect() as conn:
        for signal_id in ids:
            conn.execute(f"UPDATE signals SET {sets} WHERE id = ?", (*fields.values(), int(signal_id)))
    return len(ids)


def adopt_announced(before: float) -> int:
    """Once, when the briefing is first switched on: mail and GitHub signals already open and first
    seen before `before` were named by the ping, so they are marked announced, not briefed again."""
    ensure_schema()
    with store.connect() as conn:
        return conn.execute(
            "UPDATE signals SET notified_at = first_seen WHERE cleared_at IS NULL "
            "AND notified_at IS NULL AND source IN ('gmail', 'github') AND kind != 'asana-task' "
            "AND first_seen < ?", (before,)).rowcount




async def _run(argv: list[str], timeout_s: int) -> tuple[int, str]:
    """A subprocess in a thread (the loops must keep beating). Exit 124 on timeout, 127 if missing."""
    def go() -> tuple[int, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)
        return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)

    return await asyncio.to_thread(go)


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def _html_url(api_url: str, repo: str) -> str:
    """Turn an API subject URL into one a browser can open.

    `https://api.github.com/repos/O/R/pulls/12` -> `https://github.com/O/R/pull/12`.
    Falls back to the repository page rather than guessing, because a wrong link
    in the one report the user reads each morning is worse than a coarse one.
    """
    repo_url = f"https://github.com/{repo}" if repo else ""
    if not api_url or "api.github.com/repos/" not in api_url:
        return repo_url
    tail = api_url.split("api.github.com/repos/", 1)[1]
    parts = tail.split("/")
    if len(parts) < 4:
        return repo_url
    owner, name, kind, number = parts[0], parts[1], parts[2], parts[3]
    kind_map = {"pulls": "pull", "issues": "issues", "commits": "commit",
                "releases": "releases/tag", "discussions": "discussions"}
    if kind not in kind_map or not number.isdigit() and kind != "releases":
        return repo_url
    return f"https://github.com/{owner}/{name}/{kind_map[kind]}/{number}"


@source("github")
async def fetch_github(config: Mapping[str, Any]) -> list[dict] | None:
    """Unread GitHub notifications, via the already-authenticated `gh` CLI.

    Shelling out to `gh` rather than calling the REST API directly is the same
    choice every handler here makes: the token is already stored, already
    scoped (`repo`, `read:org`), and already refreshed by something that is not
    this daemon. Reimplementing that would mean this module holding a GitHub
    credential, which is a thing to own rather than a thing to avoid.

    Read-only: `/notifications` with no mutation. Nothing here marks anything
    as read -- the user doing that in GitHub is precisely the signal that
    clears the row.
    """
    settings = cfg(config)
    if not settings["github_enabled"]:
        return None
    code, out = await _run(["gh", "api", "/notifications", "--paginate"], int(settings["timeout_s"]))
    if code != 0:
        # A failed poll is NOT an empty inbox. Returning None here is what stops
        # a `gh` outage from clearing every open signal.
        log.warning("inbound: gh api /notifications failed (exit %s): %s", code, out.strip()[:200])
        return None
    try:
        payload = json.loads(out or "[]")
    except ValueError as exc:
        log.warning("inbound: gh returned unparseable JSON: %s", exc)
        return None
    if not isinstance(payload, list):
        return None

    reasons = {str(r) for r in settings["github_reasons"]}
    items: list[dict] = []
    for note in payload:
        # Filtered here rather than in the report, so a notification that is
        # merely news never becomes a row. A table that holds everything and a
        # view that hides most of it still has to be pruned, still shows up in
        # counts, and eventually someone reads the raw table.
        if reasons and str(note.get("reason", "")) not in reasons:
            continue
        subject = note.get("subject") or {}
        repo = ((note.get("repository") or {}).get("full_name")) or ""
        ts = note.get("updated_at") or ""
        try:
            external_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            external_ts = None
        items.append({
            "external_id": str(note.get("id")),
            "kind": f"{note.get('reason', '?')}/{subject.get('type', '?')}",
            "title": f"{repo}: {subject.get('title', '(no title)')}",
            "url": _html_url(str(subject.get("url") or ""), repo),
            "actor": repo,
            "external_ts": external_ts,
        })
    return items

# --------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------


def gmail_credentials() -> dict | None:
    """`secrets/gmail.json`: `{"user": "...", "app_password": "..."}`.

    IMAP with an app password rather than OAuth, deliberately. OAuth would mean
    three new third-party packages (`google-auth`, `google-auth-oauthlib`,
    `google-api-python-client`), a Google Cloud project, a consent screen and a
    refresh token to keep alive. This daemon is stdlib-only everywhere else and
    runs on two cores; `imaplib` is in the standard library and the credential
    is one string.

    The trade is real and worth stating: an app password is a full-mailbox
    credential, where an OAuth token could have been scoped to readonly. It is
    mitigated by never issuing a mutating IMAP command, by the file living
    outside git (`.gitignore` names `secrets/` explicitly), and by app passwords
    being individually revocable without touching the account password. If that
    trade stops being acceptable, the fix is OAuth, not a wider IMAP scope.
    """
    path = store.SECRETS / "gmail.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("inbound: %s is unreadable: %s", path.name, exc)
        return None
    if not doc.get("user") or not doc.get("app_password"):
        log.warning("inbound: %s is missing 'user' or 'app_password'", path.name)
        return None
    return doc


def mail_text(msg: Any, limit: int = 4000) -> str:
    """The readable text of a parsed message: text/plain, else text/html with
    the tags stripped. Attachments are skipped."""
    import html

    plain, rich = "", ""
    for part in msg.walk():
        if part.is_multipart() or part.get_filename():
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = (part.get_payload(decode=True) or b"").decode(charset, "replace")
        except LookupError:
            text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if kind == "text/plain" and not plain:
            plain = text
        elif kind == "text/html" and not rich:
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
            rich = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = plain or rich
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()[:limit]


def _gmail_blocking(settings: dict, creds: dict, known: set[str] | None = None) -> list[dict] | None:
    """The IMAP round trip. Runs in a thread -- imaplib is blocking.

    `known` is the Message-IDs already open: their bodies were read when they
    arrived, so only new mail costs a full fetch."""
    import email
    import imaplib
    from email.header import decode_header, make_header

    try:
        conn = imaplib.IMAP4_SSL(str(settings["gmail_host"]), timeout=int(settings["timeout_s"]))
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS all land here
        log.warning("inbound: gmail connect failed: %s", exc)
        return None
    try:
        conn.login(str(creds["user"]), str(creds["app_password"]))
        # readonly=True is not politeness -- it stops SELECT from clearing the
        # \Recent flag, which would make this poller change the mailbox it is
        # only supposed to be reading.
        conn.select(str(settings["gmail_mailbox"]), readonly=True)
        typ, data = conn.search(None, str(settings["gmail_search"]))
        if typ != "OK":
            return None
        all_ids = (data[0] or b"").split()
        raw_ids = all_ids[-int(settings["gmail_max"]):]
        items: list[dict] = []
        for raw_id in raw_ids:
            message_num = raw_id.decode("ascii", errors="replace")
            # BODY.PEEK, never RFC822: a plain fetch sets \Seen, which would
            # mark the user's mail as read just by looking at it -- the poller
            # silently doing the one thing it must never do.
            typ, msg_data = conn.fetch(
                message_num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
            )
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            def header(name: str, _msg=msg) -> str:
                value = _msg.get(name, "")
                try:
                    return str(make_header(decode_header(value)))
                except Exception:  # noqa: BLE001 - a broken header is not a failed poll
                    return value

            # Message-ID rather than the IMAP sequence number: sequence numbers
            # are reassigned as the mailbox changes, so keying on one would make
            # a signal clear and re-open as unrelated mail arrives.
            message_id = header("Message-ID") or f"seq:{message_num}"
            if "@asana.com" in header("From").lower():
                # Only an assignment is a thing waiting on the user. The
                # "You have unread notifications" digest, comments and
                # completions are Asana talking about itself (Anze, 2026-09-29).
                task = parse_asana_assignment(header("Subject"))
                if task is None:
                    continue
                task_url = _asana_task_url(conn, message_num)
                gid = re.search(r"/task/(\d+)", task_url or "")
                items.append({
                    "external_id": message_id,
                    "kind": "asana-task",
                    "title": task["title"],
                    "url": task_url or "",
                    "actor": header("From")[:120],
                    "external_ts": None,
                    "project": task["project"],
                    "task_gid": gid.group(1) if gid else None,
                })
                continue
            item = {
                "external_id": message_id,
                "kind": "mail",
                "title": f"{header('From')[:60]}: {header('Subject')}",
                "url": "",
                "actor": header("From")[:120],
                "external_ts": None,
            }
            if message_id not in (known or set()):
                # BODY.PEEK, as above. The briefing reads the text, and a reply
                # draft needs the headers that thread it.
                typ, full = conn.fetch(message_num, "(BODY.PEEK[])")
                if typ == "OK" and full and isinstance(full[0], tuple):
                    whole = email.message_from_bytes(full[0][1])
                    item["body"] = mail_text(whole)
                    # `header()` reads the four fields fetched above; Reply-To
                    # and References only exist on the full message.
                    item["meta"] = {
                        "message_id": header("Message-ID"),
                        "references": " ".join(str(whole.get("References", "")).split()),
                        "reply_to": header("Reply-To", whole) or header("From"),
                        "subject": header("Subject"),
                        "where": f"mail from {_display_name(header('From'))}",
                    }
            items.append(item)
        # More matches than gmail_max: the older ones are unlisted, not dealt with.
        return store.Partial(items) if len(all_ids) > len(raw_ids) else items
    except Exception as exc:  # noqa: BLE001 - any failure is "cannot tell", not "empty"
        log.warning("inbound: gmail poll failed: %s", exc)
        return None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


_ASANA_ASSIGNED = re.compile(r"assigned you a task:\s*(?P<title>.+?)\s*(?:\[(?P<project>[^\[\]]+)\])?\s*$")


def parse_asana_assignment(subject: str) -> dict | None:
    """`📥 Gregor assigned you a task: <title> [<project> (<owner>)]` -> title
    and project, or None for any other Asana mail. The trailing `(Anže)` is
    Asana's per-user project label, not part of the name."""
    match = _ASANA_ASSIGNED.search(subject)
    if match is None:
        return None
    project = match.group("project")
    if project:
        project = re.sub(r"\s*\([^()]*\)\s*$", "", project).strip() or None
    return {"title": match.group("title"), "project": project}


def _asana_task_url(conn: Any, message_num: str) -> str | None:
    """The task link from the mail body. The subject has no task id, and the id
    is what lets the Asana API return the description. BODY.PEEK, as above."""
    import email

    typ, data = conn.fetch(message_num, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    msg = email.message_from_bytes(data[0][1])
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            found = re.search(r"https://app\.asana\.com/\S*?/task/\d+", text)
            if found:
                return found.group(0)
    return None


@source("gmail")
async def fetch_gmail(config: Mapping[str, Any]) -> list[dict] | None:
    """Unread mail matching a configured IMAP search.

    Off by default and stays off until `secrets/gmail.json` exists -- an enabled
    source with no credential would alert every poll, which is how a channel
    stops being read.
    """
    settings = cfg(config)
    if not settings["gmail_enabled"]:
        return None
    creds = gmail_credentials()
    if creds is None:
        return None
    known = {str(item["external_id"]) for item in waiting("gmail")}
    # imaplib blocks. Same reasoning as running subprocesses in a thread executor
    # rather than asyncio subprocesses: the loops must keep beating.
    return await asyncio.to_thread(_gmail_blocking, settings, creds, known)


def _display_name(sender: str) -> str:
    """`"Gregor Leban" <g@x>` -> `Gregor Leban`; a bare address -> its local part."""
    from email.utils import parseaddr

    name, address = parseaddr(sender)
    return name.strip() or address.split("@")[0] or "someone"

# --------------------------------------------------------------------------
# Mattermost
# --------------------------------------------------------------------------


@source("mattermost")
async def fetch_mattermost(config: Mapping[str, Any]) -> list[dict] | None:
    """Direct messages, group messages and mentions after each channel's
    last-viewed mark. See daemon/mattermost.py."""
    settings = cfg(config)
    if not settings["mattermost_enabled"]:
        return None
    mm = mattermost.client(config)
    if mm is None:
        log.warning("inbound: mattermost enabled but mattermost_url or secrets/mattermost.json is missing")
        return None
    try:
        return await asyncio.to_thread(
            mattermost.unread_items, mm, list(settings["mattermost_exclude"]),
            bool(settings["mattermost_broadcast_mentions"]), int(settings["mattermost_max_per_channel"]),
        )
    except Exception as exc:  # noqa: BLE001 - any failure is "cannot tell", not "empty"
        log.warning("inbound: mattermost poll failed: %s", exc)
        return None

# --------------------------------------------------------------------------
# Asana tasks: is it critical, and the ping that says so
# --------------------------------------------------------------------------


def asana_token() -> str | None:
    """`secrets/asana.json`: `{"token": "<personal access token>"}`.

    Optional. Without it the brain judges from the title alone, which is all
    the assignment mail carries. Only ever used for one GET per new task --
    nothing here writes to Asana.
    """
    path = store.SECRETS / "asana.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("inbound: %s is unreadable: %s", path.name, exc)
        return None
    return str(doc.get("token") or "") or None


def _asana_task_blocking(gid: str, token: str, timeout_s: int) -> dict | None:
    import urllib.error
    import urllib.request

    fields = "name,notes,due_on,due_at,custom_fields.name,custom_fields.display_value,tags.name"
    request = urllib.request.Request(
        f"https://app.asana.com/api/1.0/tasks/{gid}?opt_fields={fields}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8")).get("data")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("inbound: asana task %s unavailable: %s", gid, exc)
        return None


def asana_task(gid: str, config: Mapping[str, Any] | None = None) -> dict | None:
    """One Asana task's details for Jarvis's judgement (title, notes, due, custom fields, tags), or None.
    Blocking: call it from a thread. Without `secrets/asana.json` there is nothing to read with."""
    token = asana_token()
    if not token or not gid:
        return None
    return _asana_task_blocking(gid, token, int(cfg(config)["timeout_s"]))


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


async def poll_one(name: str, config: Mapping[str, Any] | None = None) -> str:
    fetcher = SOURCES.get(name)
    if fetcher is None:
        return f"{name}: no such source"
    config = config if config is not None else store.load_config()
    try:
        items = await fetcher(config)
    except Exception as exc:  # noqa: BLE001 - one broken source must not stop the others
        log.warning("inbound: source %s raised: %s", name, exc)
        return f"{name}: failed ({type(exc).__name__})"
    if items is None:
        # Disabled, unconfigured, or failed. All three mean the same thing to
        # the table: do not touch it. The distinction is in the log.
        return f"{name}: no result (disabled or unavailable)"
    new, cleared = record(name, items)
    line = f"{name}: {len(items)} open, {new} new, {cleared} cleared"
    if isinstance(items, store.Partial):
        # Herald never alerts: Jarvis raises `inbound-truncated` when a result line carries this token.
        log.warning("inbound: %s fetch hit its limit; nothing cleared", name)
        line += " (inbound-truncated: limit hit, nothing cleared)"
    return line


async def poll_all(config: Mapping[str, Any] | None = None) -> list[str]:
    settings = cfg(config)
    if not settings["enabled"]:
        return ["inbound disabled"]
    return [await poll_one(name, config) for name in sorted(SOURCES)]


def report() -> str:
    items = waiting()
    now = time.time()
    lines = [f"inbound signals - {len(items)} waiting", ""]
    if not items:
        lines.append("  nothing waiting")
    by_source: dict[str, int] = {}
    for item in items:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
    for name in sorted(SOURCES):
        state = "on" if cfg().get(f"{name}_enabled") else "off"
        lines.append(f"  {name:10} {state:4} {by_source.get(name, 0)} waiting")
    if items:
        lines += ["", "  WAITING:"]
        for item in items[:20]:
            age = (now - item["first_seen"]) / 3600
            age_s = f"{age:.0f}h" if age < 48 else f"{age / 24:.0f}d"
            title = item["title"]
            if item["kind"] == "asana-task":
                flag = {1: "CRITICAL ", 0: ""}.get(item["critical"], "unjudged ")
                title = f"{flag}[{item['project'] or '-'}] {title}"
            elif item["critical"] == 1:
                title = f"URGENT {title}"
            lines.append(f"    {age_s:>4}  {item['source']:7} {item['kind'] or '?':28} {title[:70]}")
            if item["url"]:
                lines.append(f"          {item['url']}")
    return "\n".join(lines)
