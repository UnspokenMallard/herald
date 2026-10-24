"""Mattermost: what is waiting on the user there, and the one write Hermod makes.

Moved from Jarvis's daemon/mattermost.py on 2026-10-17 (Herald); the text is the original.

Asked for on 2026-10-01: "add this to ntfy service". The signals are direct
messages, group messages and posts that mention the user by name (Anze's
choice of "DMs + mentions" over every unread post), in every channel except
the ones `[inbound] mattermost_exclude` names -- `socials-alerts-critical`
first, which carries ~90 unread bot alerts a day and is read elsewhere.

### Read is the default, and a draft is the only write

Every request is a GET except two POSTs, held by an allowlist: `/users/ids`
(a read the API does over POST) and `/drafts`, which saves a draft for the
user to send or discard. There is deliberately no path to `/posts`, which is
what sends a message -- "Never send any messages on Mattermost or emails on
your own, just prepare drafts" (Anze, 2026-10-01). `selftest.py` asserts that
a POST to `/posts` is refused before it reaches the network.

### "Read" is the server's own mark

A signal is a post created after the channel's `last_viewed_at`. Viewing the
channel in any client moves that mark, the next poll no longer returns the
post, and `inbound.record()` (Herald's) clears it. Measured 2026-10-01: collapsed reply
threads are off for this account (`collapsed_reply_threads = off`), so a
channel's mark covers its threads too -- there is no separate thread mark to
follow. Same three-valued rule as every other source: any failure is `None`,
never `[]`, because an empty list clears every open signal.

The token is the personal access token in `secrets/mattermost.json`, shared
with the Mattermost MCP server (`~/code/personal/mattermost-mcp`).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from . import store
from .store import log

# The only POSTs this client may make. `/posts` is not here, on purpose.
_ALLOWED_POSTS = (re.compile(r"^/users/ids$"), re.compile(r"^/drafts$"))

# (method, url, body or None, headers) -> (status, response bytes). Swapped
# out by selftest.py; the default is urllib.
Transport = Callable[[str, str, "bytes | None", dict], "tuple[int, bytes]"]


class MattermostError(Exception):
    pass


def token() -> str | None:
    """`{"token": "<personal access token>"}`, or the raw token."""
    path = store.SECRETS / "mattermost.json"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("mattermost: %s is unreadable: %s", path.name, exc)
        return None
    if raw.startswith("{"):
        try:
            raw = str(json.loads(raw).get("token") or "")
        except ValueError as exc:
            log.warning("mattermost: %s is not valid JSON: %s", path.name, exc)
            return None
    return raw.strip() or None


def _urllib(method: str, url: str, body: bytes | None, headers: dict, timeout_s: int) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Client:
    def __init__(self, base_url: str, bearer: str, timeout_s: int = 20,
                 transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer
        self.timeout_s = timeout_s
        self.transport = transport

    def request(self, method: str, path: str, body: Any = None) -> Any:
        bare = path.split("?")[0]
        if method == "POST":
            if not any(p.match(bare) for p in _ALLOWED_POSTS):
                raise MattermostError(f"refusing POST {bare}: only reads and drafts are allowed")
        elif method != "GET":
            raise MattermostError(f"refusing {method} {bare}: only reads and drafts are allowed")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.bearer}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/api/v4{path}"
        if self.transport is not None:
            status, raw = self.transport(method, url, data, headers)
        else:
            status, raw = _urllib(method, url, data, headers, self.timeout_s)
        if not 200 <= status < 300:
            raise MattermostError(f"HTTP {status} on {method} {bare}: {raw[:200]!r}")
        return json.loads(raw.decode("utf-8")) if raw.strip() else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)


def client(cfg: Mapping[str, Any]) -> Client | None:
    """A client from `[inbound] mattermost_url` and the token file, or None."""
    section = dict(cfg.get("inbound") or {})
    url = str(section.get("mattermost_url") or "")
    bearer = token()
    if not url or not bearer:
        return None
    return Client(url, bearer, timeout_s=int(section.get("timeout_s", 60)))


def mention_pattern(me: Mapping[str, Any], broadcast: bool = False) -> re.Pattern:
    """What counts as mentioning the user: `@username`, the custom mention keys
    from their notification settings, and the first name if they turned that
    on. `@channel`, `@all` and `@here` only with `broadcast`: an announcement
    to a whole channel is not someone asking for the user."""
    keys = {f"@{me['username']}"}
    notify = me.get("notify_props") or {}
    for key in str(notify.get("mention_keys") or "").split(","):
        if key.strip():
            keys.add(key.strip())
    if notify.get("first_name") == "true" and me.get("first_name"):
        keys.add(str(me["first_name"]))
    if broadcast:
        keys |= {"@channel", "@all", "@here"}
    alternatives = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    return re.compile(rf"(?<![\w@])(?:{alternatives})(?![\w-])", re.IGNORECASE)


def _excluded(channel: Mapping[str, Any], team: str, exclude: set[str]) -> bool:
    names = {str(channel.get("name") or ""), str(channel.get("display_name") or ""),
             f"{team}/{channel.get('name')}"}
    return bool({n.lower() for n in names} & exclude)


def unread_items(mm: Client, exclude: list[str] | None = None, broadcast: bool = False,
                 max_per_channel: int = 30) -> list[dict]:
    """The complete current set of signals (a `store.Partial` when max_per_channel cut some). Raises on any failure -- the caller
    turns that into None."""
    me = mm.get("/users/me")
    teams = mm.get("/users/me/teams") or []
    channels: dict[str, dict] = {}
    members: dict[str, dict] = {}
    # DMs and group messages come back once per team; the dicts dedupe them.
    for team in teams:
        for channel in mm.get(f"/users/me/teams/{team['id']}/channels") or []:
            channels[channel["id"]] = channel
        for member in mm.get(f"/users/me/teams/{team['id']}/channels/members") or []:
            members[member["channel_id"]] = member
    team_name = {t["id"]: t["name"] for t in teams}
    # A permalink needs a team in its path; for a DM any of the user's teams works.
    default_team = teams[0] if teams else {"id": "", "name": ""}
    skip = {e.lower() for e in exclude or []}
    pattern = mention_pattern(me, broadcast)

    found: list[tuple[dict, dict]] = []
    truncated = False
    for channel in channels.values():
        member = members.get(channel["id"])
        if member is None:
            continue
        team = team_name.get(channel.get("team_id") or "", default_team["name"])
        if _excluded(channel, team, skip):
            continue
        direct = channel.get("type") in ("D", "G")
        if direct:
            if int(channel.get("total_msg_count") or 0) <= int(member.get("msg_count") or 0):
                continue
        elif not int(member.get("mention_count") or 0):
            continue
        last_viewed = int(member.get("last_viewed_at") or 0)
        listing = mm.get(f"/channels/{channel['id']}/posts?since={last_viewed}") or {}
        # `since` also returns older posts edited after the mark; create_at is
        # what makes a post unread.
        posts = [p for p in (listing.get("posts") or {}).values()
                 if int(p.get("create_at") or 0) > last_viewed and p.get("user_id") != me["id"]
                 and not str(p.get("type") or "").startswith("system_") and not p.get("delete_at")]
        if not direct:
            posts = [p for p in posts if pattern.search(str(p.get("message") or ""))]
        posts.sort(key=lambda p: int(p["create_at"]))
        truncated = truncated or len(posts) > max_per_channel
        found += [(channel, p) for p in posts[-max_per_channel:]]

    if not found:
        return []
    senders = sorted({p["user_id"] for _, p in found})
    users = {u["id"]: u for u in mm.post("/users/ids", senders) or []}

    items: list[dict] = []
    for channel, post in found:
        sender = users.get(post["user_id"], {}).get("username") or "someone"
        kind = "mattermost-dm" if channel.get("type") in ("D", "G") else "mattermost-mention"
        if channel.get("type") == "D":
            where = f"DM from @{sender}"
        elif channel.get("type") == "G":
            where = f"@{sender} in a group message"
        else:
            where = f"@{sender} in {channel.get('display_name') or channel.get('name')}"
        team = team_name.get(channel.get("team_id") or "") or default_team["name"]
        text = str(post.get("message") or "")
        items.append({
            "external_id": post["id"],
            "kind": kind,
            "title": f"{where}: {' '.join(text.split())[:200]}",
            "url": f"{mm.base_url}/{urllib.parse.quote(team)}/pl/{post['id']}",
            "actor": f"@{sender}",
            "external_ts": int(post["create_at"]) / 1000,
            "body": text[:4000],
            "meta": {
                "where": where,
                "channel_id": channel["id"],
                "channel_type": channel.get("type"),
                "team_id": channel.get("team_id") or default_team["id"],
                "post_id": post["id"],
                "root_id": post.get("root_id") or "",
                "sender": sender,
            },
        })
    # Older unread posts past max_per_channel are unlisted, not dealt with: flag it so nothing clears.
    return store.Partial(items) if truncated else items


def existing_draft(mm: Client, team_id: str, channel_id: str, root_id: str) -> bool:
    """Whether the user already has a draft where Jarvis would put one."""
    for draft in mm.get(f"/users/me/teams/{team_id}/drafts") or []:
        if draft.get("channel_id") == channel_id and (draft.get("root_id") or "") == root_id:
            return True
    return False


def save_draft(mm: Client, team_id: str, channel_id: str, root_id: str, message: str) -> str:
    """Save `message` as the user's draft in that channel or thread. Never
    over one they already have: a draft they were typing is theirs."""
    if existing_draft(mm, team_id, channel_id, root_id):
        return "not saved - you already had a draft there"
    me = mm.get("/users/me")
    mm.post("/drafts", {"user_id": me["id"], "channel_id": channel_id, "root_id": root_id,
                        "message": message})
    return "saved"
