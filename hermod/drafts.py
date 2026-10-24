"""Reply drafts: the only thing Hermod writes to Gmail or Mattermost.

Moved from Jarvis's daemon/drafts.py on 2026-10-17 (Herald). The text is the original; what changed
is who decides: Jarvis checks the `save-draft` action on the autonomy ladder, picks the text (the
brain writes it) and calls `POST /draft`; Hermod holds the credentials and the rule.

Asked for on 2026-10-01: "If there is a need for me to respond, prepare a
message or an email and keep the language ... Never send any messages on
Mattermost or emails on your own, just prepare drafts." Anze chose real draft
folders over drafts carried only in the notification, knowing it makes Jarvis
write to both services.

What holds "never send", in code rather than in a promise:

  * **Gmail.** There is no SMTP anywhere in Hermod. The one IMAP write is
    APPEND, to the mailbox the server itself flags `\\Drafts` (RFC 6154) --
    found by attribute, never by name, because Gmail localises folder names.
    A message in Drafts goes nowhere until a person presses Send.
  * **Mattermost.** `mattermost.Client` allows POST to `/drafts` and
    `/users/ids` only; `/posts`, which is what sends, is refused before the
    network. A draft the user already has in that channel or thread is never
    overwritten -- one they were typing is theirs.
  * **Recipients are never the model's.** The reply goes to the original's
    Reply-To (else From), or into the channel and thread the message came from.
    The brain writes the text and nothing else.
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping

from . import inbound, mattermost, store
from .store import log

_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')

def drafts_mailbox(list_lines: list[bytes]) -> str | None:
    """The mailbox flagged `\\Drafts` in a LIST response, quoted for IMAP."""
    for raw in list_lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        match = _LIST_LINE.match(line)
        if match and "\\drafts" in match.group("flags").lower():
            name = match.group("name").strip()
            return name if name.startswith('"') else f'"{name}"'
    return None


def gmail_message(meta: Mapping[str, Any], text: str, sender: str) -> bytes:
    """The draft as RFC 5322 bytes, threaded under the original."""
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    subject = str(meta.get("subject") or "")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = str(meta["reply_to"])
    msg["Subject"] = subject if re.match(r"(?i)^re:", subject) else f"Re: {subject}".strip()
    original = str(meta.get("message_id") or "")
    if original:
        msg["In-Reply-To"] = original
        msg["References"] = f"{meta.get('references') or ''} {original}".strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    msg.set_content(text)
    return msg.as_bytes()


def _save_gmail_blocking(meta: Mapping[str, Any], text: str, creds: Mapping[str, Any],
                         host: str, timeout_s: int) -> str:
    import imaplib

    conn = imaplib.IMAP4_SSL(host, timeout=timeout_s)
    try:
        conn.login(str(creds["user"]), str(creds["app_password"]))
        typ, lines = conn.list('""', "*")
        mailbox = drafts_mailbox(lines or []) if typ == "OK" else None
        if mailbox is None:
            return "not saved - the server flags no Drafts mailbox"
        typ, _ = conn.append(mailbox, r"(\Draft \Seen)", imaplib.Time2Internaldate(time.time()),
                             gmail_message(meta, text, str(creds["user"])))
        return "saved in Gmail Drafts" if typ == "OK" else f"not saved - APPEND answered {typ}"
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass

async def save(source: str, meta: Mapping[str, Any], text: str,
               config: Mapping[str, Any] | None = None) -> str:
    """Save a reply draft for one signal. Returns what happened, for the
    briefing to say; never raises."""
    import asyncio

    config = config if config is not None else store.load_config()
    if source not in ("gmail", "mattermost"):
        return f"not saved - no drafts for {source}"
    try:
        if source == "gmail":
            creds = inbound.gmail_credentials()
            if creds is None or not meta.get("reply_to"):
                return "not saved - no Gmail credential or reply address"
            settings = inbound.cfg(config)
            return await asyncio.to_thread(_save_gmail_blocking, meta, text, creds,
                                           str(settings["gmail_host"]), int(settings["timeout_s"]))
        mm = mattermost.client(config)
        if mm is None:
            return "not saved - Mattermost is not configured"
        # A mention is answered in its thread; a direct message where it was
        # written -- in its thread if it was a reply, else in the conversation.
        root = str(meta.get("root_id") or "")
        if meta.get("channel_type") not in ("D", "G") and not root:
            root = str(meta.get("post_id") or "")
        result = await asyncio.to_thread(mattermost.save_draft, mm, str(meta.get("team_id") or ""),
                                         str(meta["channel_id"]), root, text)
        return "saved as a Mattermost draft" if result == "saved" else result
    except Exception as exc:  # noqa: BLE001 - a failed save is reported, not raised
        log.warning("drafts: %s draft not saved: %s", source, exc)
        return f"not saved - {type(exc).__name__}: {str(exc)[:80]}"
