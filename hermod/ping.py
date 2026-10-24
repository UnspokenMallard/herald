"""A push that carries a count and nothing else.

Moved from Jarvis's daemon/ping.py on 2026-10-17 (Herald): the vocabulary, the shapes, the
composers and the transport are here, enforced in code. *Whether and when* to ping (the change gate,
`min_interval_s`, quiet hours, the weekend rule, the snooze, the meeting guard) stays in Jarvis,
which sends integers and names to `POST /ping`; Hermod builds the sentence, re-checks the finished
body and refuses what is not content-free. The module text below is the original, incidents and all.

The existing push sink is a Windows toast, chosen local-only on purpose: alert
text carries absolute paths, job names, repository names and branch names, and
none of that should reach a third-party server. That decision stands. It also
means the sink does not work -- all three Jarvis tasks run S4U, so `_toast`
fails with "The notification platform is unavailable" and every push-worthy
alert since 2026-08-31 has been marked `_(toast undelivered - Session 0)_`.

The practical effect is that approval latency is tied to when the user next
opens a file. Work that finishes at 09:00 waits until the next morning digest.

This module is the way out that does not reopen the privacy decision: **the
payload is counts.** Not a subject, not a path, not a repository, not a job
name -- integers and fixed words. Whoever can read it learns that some number
changed, which is exactly enough to make someone open the real report, and
nothing that would matter if the transport were hostile.

That constraint is enforced, not remembered. `compose()` builds its text from
`int`s and literals only, and `is_content_free()` re-checks the finished string
against a strict pattern; `send()` refuses anything that fails. A future edit
that interpolates a title into the body fails the check rather than shipping.

### Exception: inbound signals are named (Anze, 2026-09-30)

"When you have inbound signals notifications, change it so that it actually says
what the signals are." A count of mails made him open the digest to learn it was
a newsletter. The ping now lists up to three waiting signals under its counts
line -- the sender's display name and the subject, with every address stripped
(`inbound_lines()`), one line each. The counts line is still held to
`is_content_free()`; the named lines are held to a shape (`is_inbound_ping()`):
no newline, no control character, no `@` or `<>`, 90 characters at most. That is
free text someone else wrote reaching the phone, knowingly: the ntfy server is
this Mac's own (no upstream, no Firebase), and the morning digest already sends
the same titles on `jarvis-reports`. `[ping] inbound_named = false` turns it off.

### Exception: review items, failed checks and the command reply are named too (Anze, 2026-10-05)

Same rule as the inbound lines: the head sentence is held to `is_content_free()`,
the named lines to the shape in `is_named_ping()`. The ping speaks as Jarvis in
fixed sentence frames, and under the counts says what the review items and the
inbound signals are (`compose_named()`); `compose_health()` lists which checks
failed and why; the command reply and `status` use `compose_current()`. The
`[ping] inbound_named` switch turns all naming off.

### It is a state-change ping, not an alert relay

One message per alert would leak timing and volume, and would be noise. This
sends when the *count of things waiting on the user* changes, and at most once
per `min_interval_s` otherwise. A ping that says the same thing twice is a ping
that gets muted. Nothing waiting sends nothing (2026-10-01); "nothing waiting"
reaches the phone only as the answer to a `status` command.
"""

from __future__ import annotations


import json
import random
import re
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import store
from .store import log


# The whole permitted vocabulary of a ping body. Digits, the words below, and
# separators. Anything else -- a path separator, a repo name, an @ -- fails.
#
# The hyphen was added with the command channel's acknowledgement (2026-09-06).
# It widens the character class enough for `loop-crash` to survive this regex --
# and not enough to matter, because the word set below is the real boundary and
# neither `loop` nor `crash` is in it. Uppercase, `\`, `/` and `@` are still out,
# which is what a path, a branch or an address needs to get through.
_ALLOWED = re.compile(r"^[0-9 ,.:()\-a-z]*$")
_WORDS = {
    "nothing", "waiting", "review", "inbound", "incident", "incidents",
    "item", "items", "signal", "signals", "urgent", "and", "open", "for", "you",
    # `item(s)` tokenises to `item` + `s`. Listed rather than special-cased in
    # the tokeniser: the vocabulary is the security boundary, so it should be
    # readable in one place as the literal set of words that may ship.
    "s",
    # Added for command.py's acknowledgement. Fixed literals only: `ack()`
    # composes from these plus integers and never from a command's detail text,
    # which carries incident kinds and event ids.
    "ok", "command", "refused",
    # `refused action(s)`: the autonomy ladder's refusals, waiting on a
    # declaration only the human can write (2026-09-13).
    "action", "actions",
    # `health check: N failed`: health-check.py, the watcher outside the daemon,
    # which until 2026-09-30 claimed a phone ping in its docstring but only ever
    # raised a Notification Center alert. Count only, never a check name.
    "health", "check", "failed",
    # Jarvis's voice (2026-10-05, "sound more human like ... a personal
    # assistant"): fixed sentence frames, still integers and literals only.
    "jarvis", "here", "have", "is", "done", "command", "commands", "checks",
    "my", "found", "a", "new", "task", "marked", "as", "critical",
    # The openers below (2026-10-05, "not always 'Jarvis here'").
    "quick", "update", "from", "heads", "up", "checking", "in", "hello", "it", "hi",
    # Hermod, Hermod's display name (2026-10-23): a fixed literal, like "jarvis".
    "hermod",
}

# How Jarvis opens a message (Anze, 2026-10-05: "add some minor randomness").
# Every word is in `_WORDS`, and each reads naturally before a comma or a full
# stop. `_pick` is a seam so selftest can pin the choice.
OPENERS = (
    "Jarvis here",
    "Jarvis checking in",
    "Hello, it is Jarvis",
    "Hi, Jarvis here",
    "Quick update from Jarvis",
    "Heads up from Jarvis",
    "Hermod here for Jarvis",
    "Quick update from Hermod",
    "Heads up from Hermod",
)
_pick = random.choice


def opener() -> str:
    return _pick(OPENERS)


def cfg(config: Mapping[str, Any] | None = None) -> dict:
    """The transport settings of `[ping]` in Hermod's config.toml. Whether and when to ping (the change
    gate, interval, quiet hours, weekends, snooze, meeting guard) is Jarvis's: it is not here."""
    config = config if config is not None else store.load_config()
    section = dict(config.get("ping") or {})
    # Off until a URL is configured AND this is set. Two switches because the
    # transport landing before the decision to use it is the normal order here,
    # and a half-configured push that starts firing is worse than none.
    section.setdefault("enabled", False)
    section.setdefault("url", "")
    section.setdefault("title", "Jarvis")
    section.setdefault("timeout_s", 10)
    # Anze, 2026-10-09 (9b): one-tap command buttons on each ping. Off without a URL.
    section.setdefault("buttons_url", "")
    # 2026-10-14: the Diagnose button opens the HUD here, on the incident. "" = no button.
    section.setdefault("hud_url", "https://jarvis.unspokenmallard.xyz")
    return section


def token() -> str:
    """The bearer token, from `secrets/ntfy.json` and never from config.

    `config.toml` is committed to git and swept nightly by
    `config-autocommit.ps1`. A token field there would be in history within a
    day, and history is the one place a rotated credential stays. So there is
    deliberately no config key for it: the only path is the gitignored secrets
    file, which means a token cannot end up in the repo by someone doing the
    obvious thing.

    Absent is not an error. An ntfy server with `auth-default-access: deny-all`
    will simply refuse the publish, which surfaces as an undelivered ping rather
    than as an exception on the sentinel.
    """
    try:
        doc = json.loads((store.SECRETS / "ntfy.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        log.warning("ping: secrets/ntfy.json is unreadable: %s", exc)
        return ""
    return str(doc.get("token") or "")

def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def waiting(current: dict[str, int]) -> str:
    """The counts as one sentence, with no interpolation of anything that is not an int."""
    parts: list[str] = []
    if current["review"]:
        parts.append(_n(current["review"], "review item", "review items"))
    if current["inbound"]:
        parts.append(_n(current["inbound"], "inbound signal", "inbound signals"))
    if current["incidents"]:
        parts.append(_n(current["incidents"], "open incident", "open incidents"))
    if current.get("refused"):
        parts.append(_n(current["refused"], "refused action", "refused actions"))
    if not parts:
        return "Nothing is waiting for you."
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"You have {joined} waiting for you."


def compose(current: dict[str, int]) -> str:
    """Counts to text, in Jarvis's voice."""
    return f"{opener()}. " + waiting(current)

# Named lines (Anze, 2026-09-30 for inbound signals; 2026-10-05 for review items,
# failed health checks and the command reply): the head sentence stays held to
# `is_content_free()`, and what is named under it is held to a shape -- one
# line each, no control character, no `@`, `<>` or backslash, bounded length.
# `is_named_ping()` re-checks the finished body, and `send()` refuses the rest.
_NAMED_MAX = 3
_NAMED_CHARS = 90
_NAMED_LINES_MAX = 16
_ADDRESS = re.compile(r"<[^<>]*>|[^\s<>]+@[^\s<>]+")
_SECTION = re.compile(r"^(For review|In your inbox|Failed checks|Open incidents):$")
_NAMED_LINE = re.compile(rf"^(- [^\x00-\x1f\x7f@<>\\]{{1,{_NAMED_CHARS + 20}}}|and [0-9]+ more)$")


def _one_line(text: Any, limit: int = _NAMED_CHARS) -> str:
    text = _ADDRESS.sub("", str(text or ""))
    text = " ".join(re.sub(r"[\x00-\x1f\x7f\"\\<>@]", " ", text).split()).replace(" :", ":")
    return text[:limit - 1] + "…" if len(text) > limit else text


def _bullets(texts: list[str], limit: int = _NAMED_CHARS, cap: int = _NAMED_MAX) -> list[str]:
    lines = [f"- {_one_line(text, limit) or 'untitled'}" for text in texts[:cap]]
    if len(texts) > cap:
        lines.append(f"and {len(texts) - cap} more")
    return lines


def inbound_lines(items: list[Mapping[str, Any]]) -> list[str]:
    """Waiting signals to one line each: display name and subject, no address."""
    return _bullets([str(item.get("title") or "") for item in items])


def review_lines(items: list[Mapping[str, Any]]) -> list[str]:
    """Waiting review items: repository, branch and the first line of the summary."""
    texts = []
    for item in items:
        repo = str(item.get("repo") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        summary = next((ln.strip() for ln in str(item.get("summary") or "").splitlines() if ln.strip()), "")
        head = ", ".join(part for part in (repo, str(item.get("branch") or "")) if part)
        texts.append(f"{head}: {summary}" if summary else head)
    return _bullets(texts)


# Open incidents are named too (Anze, 2026-10-14: "make a notification and bug me ...
# offer suggestions how to fix the issues"). Each error incident is two lines: what it is
# (kind and member keys, never the alert text, which carries paths and output tails),
# then the first step of its playbook (Jarvis's daemon/playbook.py), fixed text written there.


def incident_lines(items: list[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items[:_NAMED_MAX]:
        # `@` would read as an address to `_one_line` and take the whole name with it.
        names = [str(m).split(":", 1)[1].replace("@", " on ") for m in item.get("members") or [] if ":" in str(m)]
        label = f"{item.get('kind')} {', '.join(names)}".strip() if names else str(item.get("kind") or "")
        lines += _bullets([label], limit=_NAMED_CHARS + 20)
        lines += _bullets(["fix: " + str(item.get("hint") or "").replace("`", "")], limit=_NAMED_CHARS + 20)
    if len(items) > _NAMED_MAX:
        lines.append(f"and {len(items) - _NAMED_MAX} more")
    return lines

def is_named_ping(text: str) -> bool:
    """A content-free head line, then labelled sections of named lines only."""
    head, *rest = text.split("\n")
    return (bool(rest) and len(rest) <= _NAMED_LINES_MAX and is_content_free(head)
            and bool(_SECTION.match(rest[0]))
            and all(_SECTION.match(line) or _NAMED_LINE.match(line) for line in rest))


def compose_named(current: dict[str, int], reviews: list[Mapping[str, Any]],
                  inbound_items: list[Mapping[str, Any]], lead: str | None = None,
                  incident_items: list[Mapping[str, Any]] | None = None) -> str:
    """The counts sentence, then what the incidents, review items and inbound signals are."""
    head = f"{opener()}, {lead}. " if lead else f"{opener()}. "
    lines = [head + waiting(current)]
    if incident_items:
        lines += ["Open incidents:"] + incident_lines(incident_items)
    if reviews:
        lines += ["For review:"] + review_lines(reviews)
    if inbound_items:
        lines += ["In your inbox:"] + inbound_lines(inbound_items)
    return "\n".join(lines)

def compose_health(failed: int | list[tuple[str, str]]) -> str:
    """health-check.py's failures to text: how many, then which and why."""
    items = list(failed) if not isinstance(failed, int) else []
    count = len(items) if items else int(failed) if isinstance(failed, int) else 0
    head = f"{opener()}, my health check found {_n(count, 'failed check', 'failed checks')}."
    if not items:
        return head
    return "\n".join([head, "Failed checks:"] + _bullets([f"{name}: {detail}" for name, detail in items],
                                                          limit=_NAMED_CHARS + 20, cap=5))

def is_content_free(text: str) -> bool:
    """Refuse anything outside the counts vocabulary.

    Deliberately a whitelist over the finished string rather than a blacklist of
    bad characters. A blacklist has to anticipate what a leak looks like; this
    has to anticipate what a ping looks like, which is a much smaller thing and
    one this module fully controls.
    """
    # Lowercased first: the sentence frames are capitalised, and the word set,
    # not the case, is the boundary.
    text = text.lower()
    if not _ALLOWED.match(text):
        return False
    words = {w for w in re.findall(r"[a-z]+", text)}
    return words <= _WORDS

# The one ping that names something (Anze, 2026-09-29): "You got a new task in
# Web Platform it was marked as a critical". It carries the Asana PROJECT name
# and nothing else -- never the task title or description, which are free text
# someone else wrote. Held the same way as the counts: `compose_task()` builds
# it, this pattern re-checks the finished string, and `send()` refuses a body
# that matches neither. A project name outside the pattern (a slash, an @, an
# accent, over 40 characters) is dropped from the ping, not escaped into it.
_PROJECT = r"[A-Za-z0-9][A-Za-z0-9 &.\-]{0,39}"
_TASK_PING = re.compile(rf"^(?:{'|'.join(re.escape(o) for o in OPENERS)}), you have a new task(?: in {_PROJECT})?(?:, marked as critical)?\.$")


def compose_task(project: str | None, critical: bool | None) -> str:
    """One newly assigned task to text. `critical=None` (not judged) reads as
    not critical: the ping only claims what the brain actually said."""
    text = f"{opener()}, you have a new task"
    if project and re.fullmatch(_PROJECT, project):
        text += f" in {project}"
    if critical:
        text += ", marked as critical"
    return text + "."

def is_task_ping(text: str) -> bool:
    return bool(_TASK_PING.match(text))

def button_token() -> str:
    """Token of the ntfy user `jarvisbtn`, which may only WRITE `jarvis-cmd` (Anze 2026-10-09, option 9b).

    It travels inside every ping (the phone's buttons publish with it), so it is a
    separate user from both the ping token and the command token: anyone who can read
    the ping topic can publish one of command.VERBS, nothing else, and revoking it
    (`docker exec ntfy ntfy token remove jarvisbtn <token>`) touches neither channel.
    """
    try:
        doc = json.loads((store.SECRETS / "ntfy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(doc.get("button_token") or "")

def buttons(settings: Mapping[str, Any], incident_id: int | None = None,
            retry_triage: int | None = None) -> str:
    """The ntfy `Actions` header: up to three buttons, each publishing one fixed command.

    Status and Snooze 4h always; the third is Retry #N while an issue-triage item waits
    for Anze (`retry_triage`, the issue number), else Dep rehearsal. Every body is a literal from this
    function, parsed by the command channel like a typed message. Empty when buttons are off or no
    token exists.

    While an error incident is open (2026-10-14, `incident_id`) the third is Diagnose instead: a `view`
    button, which opens the HUD on that incident (its log, the fix steps, and a button
    that starts a diagnose session). It publishes nothing and carries no token; the HUD
    login still guards the page.

    Jarvis knows which incident or review item is open; Hermod only draws the buttons, so both
    arrive as plain integers.
    """
    url, tok = str(settings.get("buttons_url") or ""), button_token()
    if not url or not tok:
        return ""
    hud = str(settings.get("hud_url") or "").rstrip("/")
    if hud and incident_id is not None:
        acts = [("Status", "status"), ("Snooze 4h", "snooze 4")]
        return "; ".join([*(f"http, {label}, {url}, method=POST, headers.Authorization=Bearer {tok}, "
                            f"body={body}, clear=true" for label, body in acts),
                          f"view, Diagnose, {hud}/?incident={int(incident_id)}, clear=true"])
    third = (f"Retry #{int(retry_triage)}", f"retry-triage {int(retry_triage)}") if retry_triage is not None \
        else ("Dep rehearsal", "run-deprehearsal")
    acts = [("Status", "status"), ("Snooze 4h", "snooze 4"), third]
    return "; ".join(f"http, {label}, {url}, method=POST, headers.Authorization=Bearer {tok}, "
                     f"body={body}, clear=true" for label, body in acts)


def check(text: str) -> bool:
    """Whether a finished body is one of the three shapes a ping may have."""
    return is_content_free(text) or is_task_ping(text) or is_named_ping(text)


# ntfy headers per ping kind (2026-10-22). Priority 2 for routine counts, 4 for incidents and health,
# 3 otherwise. X-Sequence-ID only where a new ping replaces the old one in place: counts, never incidents.
_PRIORITY = {"counts": 2, "hud-done": 2, "incident": 4, "health": 4}
_SEQUENCE = {"counts": "herald-counts"}


def kind_of(body: Mapping[str, Any]) -> str:
    """The header kind of a `compose_payload` request: a counts ping with incidents is an incident ping."""
    kind = str(body.get("kind") or "")
    if kind == "counts" and int((body.get("counts") or {}).get("incidents") or 0):
        return "incident"
    if kind == "hud" and not int(body.get("waiting") or 0) and not int(body.get("open") or 0):
        return "hud-done"
    return kind


def send(text: str, config: Mapping[str, Any] | None = None, incident_id: int | None = None,
         retry_triage: int | None = None, kind: str = "") -> dict:
    """POST the body. Returns `{"delivered": bool, "reason": str}`; never raises.

    A body that fails `check` is refused, not sanitised: a ping that quietly strips the leak is a ping
    that hides the bug that produced it. Hermod never alerts, so the refusal is the `reason`
    ("not content-free") and Jarvis raises `ping-not-content-free` from it.
    """
    settings = cfg(config)
    if not settings["enabled"] or not settings["url"]:
        return {"delivered": False, "reason": "ping disabled"}
    if not check(text):
        log.warning("ping: refusing to send a body that is not content-free: %r", text[:80])
        return {"delivered": False, "reason": "not content-free"}

    return _publish(settings, text, buttons(settings, incident_id, retry_triage), kind=kind)


def _publish(settings: Mapping[str, Any], text: str, actions: str = "",
             extra: Mapping[str, str] | None = None, kind: str = "") -> dict:
    request = urllib.request.Request(
        str(settings["url"]),
        data=text.encode("utf-8"),
        method="POST",
        headers={"Title": str(settings["title"]), "Content-Type": "text/plain",
                 "Priority": str(_PRIORITY.get(kind, 3)),
                 **({"X-Sequence-ID": _SEQUENCE[kind]} if kind in _SEQUENCE else {}), **(extra or {})},
    )
    bearer = token()
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    if actions:
        request.add_header("Actions", actions)
    try:
        with urllib.request.urlopen(request, timeout=int(settings["timeout_s"])) as resp:
            ok = 200 <= resp.status < 300
            return {"delivered": ok, "reason": "" if ok else f"ntfy answered HTTP {resp.status}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("ping: delivery failed: %s", exc)
        return {"delivered": False, "reason": f"delivery failed: {type(exc).__name__}"}


# The HUD's ping (2026-10-11, scripts/hud-ping.py in Jarvis): a session waits on Anze, or finished.
# Counts in a fixed frame, then named lines "project: title - what" through the same one-line
# sanitiser and shape as the others. No buttons; tapping it opens the HUD on the session (`click`).
_WHAT = {"waiting": "waiting for you", "done": "done and idle", "open": "idle, waiting for you"}


def compose_hud(waiting: int, done: int, still_open: int = 0, items: list[str] | None = None) -> str:
    """`items` are `KIND|PROJECT|TITLE[|QUESTION]` strings: kind is waiting, done or open."""
    if still_open:
        head = f"{opener()}, {'a task is' if still_open == 1 else f'{still_open} task(s)'} open and waiting for you."
    else:
        parts = []
        if waiting:
            parts.append("a task is waiting for you" if waiting == 1 else f"{waiting} task(s) waiting for you")
        if done:
            parts.append("a task is done" if done == 1 else f"{done} task(s) done")
        head = f"{opener()}, {' and '.join(parts)}."
    texts = []
    for raw in items or []:
        kind, project, title, question = (raw.split("|", 3) + ["", "", "", ""])[:4]
        if kind not in _WHAT:
            continue
        name = _one_line(project, 30)
        label = _one_line(title, 40)
        who = f"{name}: {label}" if name and label and label != name else name or label or "a task"
        asks = _one_line(question, 70) if kind == "waiting" else ""
        texts.append(f"{who} - {_WHAT[kind]}" + (f", asks: {asks}" if asks else ""))
    return "\n".join([head] + _bullets(texts, limit=_NAMED_CHARS + 20))


def is_named_body(text: str) -> bool:
    """A content-free head line, then named lines only (the HUD ping has no section headers)."""
    head, *rest = text.split("\n")
    return (len(rest) <= _NAMED_LINES_MAX and is_content_free(head)
            and all(_NAMED_LINE.match(line) for line in rest))


def send_hud(text: str, click: str = "", config: Mapping[str, Any] | None = None, kind: str = "hud") -> dict:
    settings = cfg(config)
    if not settings["enabled"] or not settings["url"]:
        return {"delivered": False, "reason": "ping disabled"}
    if not is_named_body(text):
        log.warning("ping: refusing a HUD body that is not content-free: %r", text[:80])
        return {"delivered": False, "reason": "not content-free"}
    return _publish(settings, text, extra={"Tags": "computer", **({"Click": click} if click.startswith("https://") else {})}, kind=kind)


def compose_payload(body: Mapping[str, Any]) -> str:
    """A structured ping request to its text. Jarvis supplies integers and the names of things; the
    sentence frames, the opener and every shape rule are here.

    kinds: `counts` (the state-change ping and the command reply: `counts`, optional `reviews`,
    `inbound`, `incidents` lists and a `lead`), `task` (`project`, `critical`), `health` (`failed`:
    a count or `[[name, detail], ...]`), `hud` (`waiting`, `done`, `open`, `items`).
    """
    kind = str(body.get("kind") or "")
    if kind == "counts":
        counts = {key: int(body.get("counts", {}).get(key) or 0)
                  for key in ("review", "inbound", "incidents", "refused")}
        return compose_named(counts, list(body.get("reviews") or []), list(body.get("inbound") or []),
                             str(body["lead"]) if body.get("lead") else None,
                             list(body.get("incidents") or []) or None)
    if kind == "task":
        return compose_task(body.get("project"), body.get("critical"))
    if kind == "health":
        failed = body.get("failed") or 0
        return compose_health(failed if isinstance(failed, int) else [(str(n), str(d)) for n, d in failed])
    if kind == "hud":
        return compose_hud(int(body.get("waiting") or 0), int(body.get("done") or 0), int(body.get("open") or 0),
                           [str(i) for i in body.get("items") or []])
    raise ValueError(f"unknown ping kind {kind!r}")

