"""Reports to the phone: the digests, in full, on their own ntfy topic.

Asked for on 2026-09-13 ("send reports there too"), the day Jarvis development was closed. This is
deliberately NOT ping.py. The ping carries counts and is enforced to carry nothing else; that
guarantee is untouched. A report carries what the report carries -- repository names, paths, mail
subjects, security findings -- so it goes to a different topic, `jarvis-reports`, which only the
phone's ntfy user can read and only the daemon's can write, on the self-hosted ntfy server on this
machine. ping.py's privacy decision is about third-party servers; nothing here reaches one. The
server has no upstream relay configured, and the phone connects to it directly through Caddy.

Moved from Jarvis's daemon/reports.py on 2026-10-17 (Herald). What stays in Jarvis, because it
decides *whether and when*: that a report is **fresh** (written within the job's `max_age_s`: a job
that failed and left last week's file behind must not be delivered as this morning's report), the
quiet hours, the `publish-report` action on the autonomy ladder (a refusal is reported, never worked
around) and the narration of the text as prose (a brain call). What is here is how it is said:

  * **Split, never truncated.** ntfy's docs call bodies over 4K untested, so a long report goes as
    numbered parts cut at line boundaries.
  * **A failed POST raises**, so the job's retries and its alert cover a broken transport instead of
    a report quietly going nowhere.
"""

from __future__ import annotations

import urllib.request
from typing import Any, Mapping

from . import ping, store

# A part smaller than this could not hold a line of the reports sent here; it
# also keeps a mistyped max_bytes from looping on a one-character cut.
MIN_PART_BYTES = 256


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    config = config if config is not None else store.load_config()
    section = dict(config.get("reports") or {})
    # Off unless config says otherwise: content leaving the machine is opt-in.
    section.setdefault("enabled", False)
    section.setdefault("base_url", "http://127.0.0.1:2586")
    section.setdefault("topic", "jarvis-reports")
    section.setdefault("max_bytes", 3900)
    section.setdefault("timeout_s", 15)
    # "low": on the phone, without sound or vibration. PLAN.md's Phase 5 rule
    # was "no interrupting notifications"; a report is there to be read, and
    # the counts ping is still the thing that interrupts.
    section.setdefault("priority", "low")
    return section


def split(text: str, max_bytes: int) -> list[str]:
    """Cut `text` into parts of at most `max_bytes` UTF-8 bytes, at line breaks.

    A single line longer than a part is cut at a character boundary. Joined back
    together, the parts are the report; the only thing ever dropped is a part
    that would be whitespace alone -- a message of nothing.
    """
    limit = max(int(max_bytes), MIN_PART_BYTES)
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:
            if current:
                parts.append(current)
                current = ""
            head = line.encode("utf-8")[:limit].decode("utf-8", "ignore")
            parts.append(head)
            line = line[len(head):]
        if current and len((current + line).encode("utf-8")) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def _post(settings: Mapping[str, Any], body: str, title: str) -> None:
    """One message. Raises on any failure."""
    url = f"{str(settings['base_url']).rstrip('/')}/{settings['topic']}"
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            # HTTP headers are Latin-1 on the wire; titles are kept ASCII.
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Markdown": "yes",
            "Tags": "memo",
            "Priority": str(settings["priority"]),
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    bearer = ping.token()
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(request, timeout=int(settings["timeout_s"])) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"ntfy answered HTTP {resp.status}")


def push(title: str, text: str, spoken: str | None = None, config: Mapping[str, Any] | None = None) -> dict:
    """Publish one report. `spoken` is the report rewritten as prose (Jarvis narrates it); without it
    the file's `text` goes as written, under a one-line intro from Jarvis's voice.

    Returns `{"pushed": bool, "parts": int, "reason": str}`. Disabled is a returned reason, not an
    exception: it is not the transport failing. A failed POST raises.
    """
    settings = cfg(config)
    if not settings["enabled"]:
        return {"pushed": False, "parts": 0, "reason": "[reports] disabled in Hermod's config"}
    if not text.strip() and not (spoken or "").strip():
        return {"pushed": False, "parts": 0, "reason": "empty"}
    body = spoken or f"{ping.opener()}, your {title} is ready.\n\n{text}"
    parts = split(body, int(settings["max_bytes"]))
    for index, part in enumerate(parts, 1):
        label = f"Jarvis: {title}" + (f" ({index}/{len(parts)})" if len(parts) > 1 else "")
        _post(settings, part, label)
    return {"pushed": True, "parts": len(parts), "reason": ""}
