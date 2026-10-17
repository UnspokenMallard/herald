"""Herald against a scratch store: the channel rules (the ping carries counts and nothing else, drafts
never send, a failed poll never empties the inbox, the command verbs grant nothing), the composers, the
transport with a faked network, and the service's write guard.
Run: ~/.herald/.venv/bin/python ~/.herald/tests/test_herald.py"""
import asyncio
import email
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="herald-test-"))
os.environ["HERALD_HOME"] = str(TMP)  # read at import: never the live store, never the live secrets
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

from herald import briefing, commands, delivery, drafts, inbound, mattermost, ping, reports, store  # noqa: E402

ping._pick = lambda options: options[0]  # "Jarvis here": the opener is random in production

CFG = {"ping": {"enabled": True, "url": "http://127.0.0.1:9/jarvis", "buttons_url": "", "timeout_s": 1}}


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_net(sent: list):
    def urlopen(request, timeout=10):
        sent.append((request.full_url, dict(request.header_items()), request.data.decode("utf-8") if request.data else ""))
        return _Resp()
    return urlopen


def test_content_free_ping():
    assert store.DB_PATH == TMP / "state" / "herald.db"
    empty = ping.compose({"review": 0, "inbound": 0, "incidents": 0})
    assert empty.endswith("Nothing is waiting for you.") and ping.is_content_free(empty)
    body = ping.compose({"review": 2, "inbound": 3, "incidents": 1})
    assert ping.is_content_free(body) and "2 review items" in body and "3 inbound signals" in body
    leaks = [r"waiting for you: C:\Users\anzes\Projects\cmdpeek", "waiting for you: 1 review item(s) on jarvis/q-20260903-2210",
             "waiting for you: review requested by someone@example.com", "waiting for you: 1 item(s) in UnspokenMallard/psmux",
             "NEEDS REVIEW: gate failed"]
    assert not any(ping.is_content_free(t) or ping.check(t) for t in leaks)
    assert ping.compose_task("Web Platform", True).endswith("you have a new task in Web Platform, marked as critical.")
    assert ping.compose_task("Unspoken/Mallard", True).endswith("you have a new task, marked as critical.")
    task_leaks = ["You got a new task in Web Platform: error wrongly reported", "You got a new task in someone@example.com"]
    assert not any(ping.is_task_ping(t) or ping.is_content_free(t) for t in task_leaks)
    refused = ping.compose({"review": 0, "inbound": 0, "incidents": 0, "refused": 2})
    assert refused.endswith("You have 2 refused actions waiting for you.") and ping.is_content_free(refused)
    assert all(ping.is_content_free(f"{o}. {ping.waiting({'review': 1, 'inbound': 0, 'incidents': 0})}")
               and ping.is_task_ping(f"{o}, you have a new task.") for o in ping.OPENERS)


def test_named_lines():
    named = ping.inbound_lines([{"title": "Event Registry <info@eventregistry.org>: How to monitor news"},
                                {"title": "\"'Blog'\" <someone@example.com>: signup from other@example.com"},
                                {"title": "Asana <no-reply@asana.com>: " + "z" * 200}, {"title": "fourth"}, {"title": "fifth"}])
    assert named[0] == "- Event Registry: How to monitor news" and "@" not in "".join(named) and "<" not in "".join(named)
    assert len(named[2]) == 2 + 90 and named[3:] == ["and 2 more"]
    head = "Jarvis here. You have 5 inbound signals waiting for you.\nIn your inbox:\n" + "\n".join(named)
    assert ping.is_named_ping(head)
    assert not ping.is_named_ping(head + "\n" * 14 + "- one line too many")
    assert not ping.is_named_ping("Jarvis here. You have 1 inbound signal waiting for you.\nIn your inbox:\n- mail to a@b.c")
    assert not ping.is_named_ping("Jarvis here. You have 1 inbound signal waiting for you.\n- x")
    assert not ping.is_named_ping(r"Jarvis here. C:\Users\x 1 inbound signal waiting for you." + "\nIn your inbox:\n- x")
    assert not ping.is_named_ping("Jarvis here. You have 1 review item waiting for you.\nIn your inbox:\nsmuggled line")
    assert ping.compose_health(3) == "Jarvis here, my health check found 3 failed checks."
    named_health = ping.compose_health([("disk free", "4 GB free"), ("hooks", "bash-guard missing a@b.c")])
    assert named_health.split("\n")[:3] == ["Jarvis here, my health check found 2 failed checks.", "Failed checks:",
                                            "- disk free: 4 GB free"]
    assert "@" not in named_health and ping.is_named_ping(named_health)
    review = ping.compose_named({"review": 1, "inbound": 0, "incidents": 0},
                                [{"repo": "/Users/x/code/cmdpeek", "branch": "jarvis/q-1", "summary": "Fixes the parser\nmore"}], [])
    assert review.split("\n")[1:] == ["For review:", "- cmdpeek, jarvis/q-1: Fixes the parser"] and ping.is_named_ping(review)
    inc = ping.compose_named({"review": 0, "inbound": 0, "incidents": 1}, [], [],
                             incident_items=[{"kind": "loop-crash", "members": ["loop-crash:a@b"], "hint": "restart `x`"}])
    assert ping.is_named_ping(inc) and "@" not in inc and "`" not in inc


def test_compose_payload_and_send():
    body = ping.compose_payload({"kind": "counts", "counts": {"review": 0, "inbound": 1, "incidents": 0, "refused": 0},
                                 "inbound": [{"title": "x"}], "lead": "2 commands done"})
    assert body.split("\n")[1:] == ["In your inbox:", "- x"] and "2 commands done" in body and ping.check(body)
    assert ping.compose_payload({"kind": "task", "project": "Web Platform", "critical": None}).endswith("new task in Web Platform.")
    try:
        ping.compose_payload({"kind": "free-text", "text": "C:\\secret"})
        assert False, "an unknown kind composed"
    except ValueError:
        pass
    sent: list = []
    real = ping.urllib.request.urlopen
    ping.urllib.request.urlopen = fake_net(sent)
    try:
        cfg = dict(CFG)
        assert ping.send("waiting for you: 1 review item(s)", cfg)["delivered"] and len(sent) == 1
        out = ping.send(r"waiting for you: C:\Users\anzes\secret", cfg)
        assert out == {"delivered": False, "reason": "not content-free"} and len(sent) == 1  # refused, not sanitised
        assert ping.send("Jarvis here. Nothing is waiting for you.", {"ping": {"enabled": False}})["reason"] == "ping disabled"
        # a leaking body smuggled through a hand-made payload kind fails the finished-string check
        lead_leak = ping.compose_payload({"kind": "counts", "counts": {"review": 1}, "lead": "see C:\\Users\\x"})
        assert not ping.check(lead_leak) and ping.send(lead_leak, cfg)["reason"] == "not content-free" and len(sent) == 1
    finally:
        ping.urllib.request.urlopen = real


def test_buttons():
    (TMP / "secrets").mkdir(exist_ok=True)
    (TMP / "secrets" / "ntfy.json").write_text(json.dumps({"token": "t0", "button_token": "b0", "command_token": "c0"}))
    settings = {"buttons_url": "https://n.example/cmd", "hud_url": "https://hud.example"}
    assert ping.token() == "t0" and commands.token() == "c0"
    third = ping.buttons(settings).split("; ")[2]
    assert third.startswith("http, Dep rehearsal") and "body=run-deprehearsal" in third
    assert "Retry #12" in ping.buttons(settings, retry_triage=12) and "body=retry-triage 12" in ping.buttons(settings, retry_triage=12)
    diag = ping.buttons(settings, incident_id=7, retry_triage=12).split("; ")[2]
    assert diag == "view, Diagnose, https://hud.example/?incident=7, clear=true" and "b0" not in diag
    assert ping.buttons({"buttons_url": ""}) == ""


def test_hud_ping():
    text = ping.compose_hud(1, 0, 0, ["waiting|er-web|Fix login|How to route those?", "done|er-api|Tests|"])
    assert text.split("\n")[0].endswith("a task is waiting for you.")
    assert "- er-web: Fix login - waiting for you, asks: How to route those?" in text and ping.is_named_body(text)
    sent: list = []
    real = ping.urllib.request.urlopen
    ping.urllib.request.urlopen = fake_net(sent)
    try:
        assert ping.send_hud(text, "https://hud.example/?s=1", CFG)["delivered"]
        _url, headers, _body = sent[0]
        assert headers.get("Click") == "https://hud.example/?s=1" and headers.get("Tags") == "computer"
        assert ping.send_hud("Jarvis here, C:\\x leaks", "", CFG)["reason"] == "not content-free"
    finally:
        ping.urllib.request.urlopen = real


def test_reports():
    text = "".join(f"- line {i:03d} " + "x" * 60 + "\n" for i in range(120))
    parts = reports.split(text, 1000)
    assert len(parts) > 1 and all(len(p.encode()) <= 1000 for p in parts) and "".join(parts) == text
    cut = reports.split("y" * 3000 + "\n", 1000)
    assert "".join(cut).rstrip() == "y" * 3000 and all(len(p.encode()) <= 1000 and p.strip() for p in cut)
    sent: list = []
    real = reports.urllib.request.urlopen
    reports.urllib.request.urlopen = fake_net(sent)
    try:
        cfg = {"reports": {"enabled": True, "base_url": "http://127.0.0.1:9", "topic": "t-reports", "max_bytes": 1000}}
        out = reports.push("selftest digest", text, config=cfg)
        assert out["pushed"] and out["parts"] == len(parts) == len(sent)
        assert all(url.endswith("/t-reports") for url, _h, _b in sent)
        assert all(h.get("Priority") == "low" and h.get("Markdown") == "yes" for _u, h, _b in sent)
        assert sent[0][1]["Title"].endswith(f"(1/{len(parts)})")
        assert "".join(b for _u, _h, b in sent) == "Jarvis here, your selftest digest is ready.\n\n" + text
        sent.clear()
        out = reports.push("narrated", text, spoken="Jarvis here, all quiet.", config=cfg)
        assert out["pushed"] and sent[0][2] == "Jarvis here, all quiet."
        sent.clear()
        assert reports.push("off", text, config={"reports": {"enabled": False}})["pushed"] is False and not sent
        assert reports.push("empty", " ", config=cfg)["reason"] == "empty"
    finally:
        reports.urllib.request.urlopen = real


def test_briefing_compose():
    long_fyi = [{"id": i, "source": "github", "kind": "review_requested", "title": f"o/r: change {i} " + "x" * 80,
                 "url": "", "meta": {}, "thread": None, "critical": None, "headline": None, "draft": None} for i in range(80)]
    urgent = [{"id": 999, "source": "gmail", "kind": "mail", "title": "t", "url": "", "meta": {}, "thread": None,
               "critical": 1, "headline": "the API is down", "draft": None}]
    body, is_urgent, _ = briefing.compose(urgent + long_fyi, [], max_bytes=1500)
    assert len(body.encode()) <= 1500 and "The API is down" in body and "more in the morning digest" in body and is_urgent
    assert body.startswith("Jarvis here") and "a new issue has come to our attention" in body
    sent: list = []
    real = briefing.urllib.request.urlopen
    briefing.urllib.request.urlopen = fake_net(sent)
    try:
        cfg = {"briefing": {"enabled": True, "base_url": "http://127.0.0.1:9", "topic": "jarvis"}}
        out = briefing.send(urgent, [], cfg)
        assert out["delivered"] and out["urgent"] and sent[0][1]["Priority"] == "4" and sent[0][0].endswith("/jarvis")
        assert briefing.send(urgent, [], {"briefing": {"enabled": False}})["delivered"] is False
    finally:
        briefing.urllib.request.urlopen = real


def test_mattermost_cannot_send():
    calls = []
    mm = mattermost.Client("https://mm.example", "t", transport=lambda m, u, b, h: calls.append((m, u)) or (200, b"{}"))
    refused = []
    for method, path in (("POST", "/posts"), ("PUT", "/posts/x/patch"), ("DELETE", "/posts/x"), ("POST", "/posts?set_online=true")):
        try:
            mm.request(method, path, {"message": "hi"})
        except mattermost.MattermostError:
            refused.append(path)
    assert len(refused) == 4 and not calls  # refused before the network
    mm.post("/drafts", {"message": "x"})
    assert calls == [("POST", "https://mm.example/api/v4/drafts")]


def _post(pid, user, at, message, kind="", root=""):
    return {"id": pid, "user_id": user, "create_at": at, "message": message, "type": kind, "root_id": root, "delete_at": 0}


def test_mattermost_signals_and_drafts():
    me = {"id": "me", "username": "anzes", "notify_props": {"mention_keys": "anzes,AnzeS"}}
    pattern = mattermost.mention_pattern(me)
    assert pattern.search("hey @anzes, look") and pattern.search("anzes: ok?")
    assert not pattern.search("@anzesx hi") and not pattern.search("@channel deploy")
    assert mattermost.mention_pattern(me, broadcast=True).search("@channel deploy")
    api = {
        "/users/me": me, "/users/me/teams": [{"id": "t1", "name": "eventregistry"}],
        "/users/me/teams/t1/channels": [
            {"id": "dm", "team_id": "", "type": "D", "name": "me__iaro", "display_name": "", "total_msg_count": 5},
            {"id": "gen", "team_id": "t1", "type": "O", "name": "general", "display_name": "General", "total_msg_count": 9},
            {"id": "alerts", "team_id": "t1", "type": "O", "name": "socials-alerts-critical",
             "display_name": "socials-alerts-critical", "total_msg_count": 99},
            {"id": "quiet", "team_id": "t1", "type": "O", "name": "random", "display_name": "Random", "total_msg_count": 4}],
        "/users/me/teams/t1/channels/members": [
            {"channel_id": "dm", "msg_count": 3, "mention_count": 2, "last_viewed_at": 1000},
            {"channel_id": "gen", "msg_count": 7, "mention_count": 1, "last_viewed_at": 1000},
            {"channel_id": "alerts", "msg_count": 0, "mention_count": 5, "last_viewed_at": 0},
            {"channel_id": "quiet", "msg_count": 1, "mention_count": 0, "last_viewed_at": 1000}],
        "/channels/dm/posts": {"posts": {
            "p0": _post("p0", "iaro", 900, "older, already read"), "p1": _post("p1", "iaro", 1100, "OAuth login is broken on er-api"),
            "p2": _post("p2", "iaro", 1200, "can you look?"), "p3": _post("p3", "me", 1300, "my own reply"),
            "p4": _post("p4", "iaro", 1400, "", kind="system_add_to_channel")}},
        "/channels/gen/posts": {"posts": {"g1": _post("g1", "luis", 1100, "lunch anyone?"),
                                          "g2": _post("g2", "luis", 1200, "@anzes the logo files are up")}},
        "/channels/alerts/posts": {"posts": {"a1": _post("a1", "bot", 10, "@anzes ALERT")}},
    }
    fetched: list = []

    def fake_api(method, url, body, headers):
        path = url.split("/api/v4")[1].split("?")[0]
        fetched.append(path)
        if path == "/users/ids":
            return 200, json.dumps([{"id": "iaro", "username": "iaroslav"}, {"id": "luis", "username": "luis"}]).encode()
        return 200, json.dumps(api[path]).encode()

    items = mattermost.unread_items(mattermost.Client("https://mm.example", "t", transport=fake_api), exclude=["socials-alerts-critical"])
    assert [i["external_id"] for i in items] == ["p1", "p2", "g2"]
    assert "/channels/alerts/posts" not in fetched and "/channels/quiet/posts" not in fetched
    assert items[2]["meta"]["channel_type"] == "O" and items[2]["meta"]["where"] == "@luis in General"
    assert items[0]["meta"]["where"] == "DM from @iaroslav" and items[2]["url"] == "https://mm.example/eventregistry/pl/g2"

    posted: list = []

    def drafts_api(method, url, body, headers):
        path = url.split("/api/v4")[1]
        if method == "POST":
            posted.append(json.loads(body))
            return 201, body
        if path == "/users/me":
            return 200, b'{"id": "me"}'
        return 200, json.dumps([{"channel_id": "dm", "root_id": ""}]).encode()

    dmm = mattermost.Client("https://mm.example", "t", transport=drafts_api)
    kept = mattermost.save_draft(dmm, "t1", "dm", "", "hello")
    saved = mattermost.save_draft(dmm, "t1", "gen", "g2", "on it")
    assert "already had a draft" in kept and saved == "saved"
    assert posted == [{"user_id": "me", "channel_id": "gen", "root_id": "g2", "message": "on it"}]


def test_gmail_drafts():
    raw = drafts.gmail_message({"subject": "Napaka pri prijavi", "reply_to": "Gregor <g@er.org>", "message_id": "<m1@er.org>",
                                "references": "<m0@er.org>"}, "Zdravo Gregor,\n\npogledam.\n\nAnže", "me@gmail.com")
    d = email.message_from_bytes(raw)
    assert d["To"] == "Gregor <g@er.org>" and d["In-Reply-To"] == "<m1@er.org>"
    assert d["References"] == "<m0@er.org> <m1@er.org>" and d["Subject"] == "Re: Napaka pri prijavi"
    assert drafts.drafts_mailbox([b'(\\HasNoChildren) "/" "INBOX"', b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Osnutki"']) == '"[Gmail]/Osnutki"'
    assert drafts.drafts_mailbox([b'(\\HasNoChildren) "/" "INBOX"']) is None
    # no SMTP anywhere: the module does not even import it
    src = (ROOT / "herald" / "drafts.py").read_text()
    assert "smtplib" not in src and "SMTP" not in src.replace("There is no SMTP", "")
    assert asyncio.run(drafts.save("slack", {}, "x", {})).startswith("not saved - no drafts for")
    assert "no Gmail credential" in asyncio.run(drafts.save("gmail", {"reply_to": "a@b.c"}, "x", {}))  # no secrets/gmail.json here


def test_inbound_clears_only_on_a_list():
    def note(reason, note_id, repo="o/r"):
        return {"id": note_id, "reason": reason, "updated_at": "2026-09-05T10:00:00Z", "repository": {"full_name": repo},
                "subject": {"title": f"{reason} thing", "type": "PullRequest", "url": f"https://api.github.com/repos/{repo}/pulls/7"}}

    fixture = [note("subscribed", "1"), note("subscribed", "2"), note("author", "3"), note("review_requested", "4"),
               note("mention", "5"), note("assign", "6")]
    real_run = inbound._run

    async def fake_gh(argv, timeout_s):
        return 0, json.dumps(fixture)

    async def failing_gh(argv, timeout_s):
        return 1, "gh: could not connect"

    async def scenario():
        inbound._run = fake_gh
        got = await inbound.fetch_github({})
        assert sorted((g["kind"] or "").split("/")[0] for g in got) == ["assign", "mention", "review_requested"]
        assert got[0]["url"] == "https://github.com/o/r/pull/7"
        assert inbound.record("github", got) == (3, 0) and inbound.record("github", got) == (0, 0)
        remaining = [g for g in got if "review_requested" not in (g["kind"] or "")]
        assert inbound.record("github", remaining) == (0, 1) and len(inbound.waiting("github")) == 2
        inbound._run = failing_gh
        assert await inbound.fetch_github({}) is None
        await inbound.poll_one("github", {})
        assert len(inbound.waiting("github")) == 2  # an outage must not erase the inbox
        with store.connect() as conn:
            conn.execute("UPDATE signals SET first_seen = ?, last_seen = ? WHERE cleared_at IS NULL",
                         (time.time() - 400 * 86400,) * 2)
        inbound.prune(retain_days=30)
        assert len(inbound.waiting("github")) == 2  # an open signal survives at any age

    try:
        asyncio.run(scenario())
    finally:
        inbound._run = real_run
    assert asyncio.run(inbound.fetch_gmail({"inbound": {"gmail_enabled": False}})) is None
    assert inbound.gmail_credentials() is None
    (TMP / "secrets" / "gmail.json").write_text('{"user": "a@b.c"}')
    assert inbound.gmail_credentials() is None  # missing app_password
    with store.connect() as conn:
        conn.execute("DELETE FROM signals")


def test_inbound_judgement_columns():
    inbound.record("gmail", [{"external_id": "<m1>", "kind": "mail", "title": "a: b", "url": "", "actor": "a", "body": "text",
                              "meta": {"where": "mail from a"}},
                             {"external_id": "<t1>", "kind": "asana-task", "title": "task", "url": "", "project": "Web", "task_gid": "1"}])
    rows = inbound.open_rows()
    assert [r["external_id"] if "external_id" in r else r["kind"] for r in rows] == ["mail"]  # tasks have their own ping
    assert rows[0]["meta"] == {"where": "mail from a"} and rows[0]["body"] == "text"
    assert [t["title"] for t in inbound.pending_tasks()] == ["task"]
    assert inbound.update([rows[0]["id"]], critical=1, headline="h", draft="saved in Gmail Drafts") == 1
    assert inbound.open_rows()[0]["critical"] == 1
    for bad in ({"title": "x"}, {"cleared_at": 1.0}, {"body": "x"}, {"id": 3}):
        try:
            inbound.update([rows[0]["id"]], **bad)
            assert False, f"{bad} was writable"
        except ValueError:
            pass
    assert inbound.adopt_announced(time.time() + 1) == 1 and inbound.open_rows()[0]["notified_at"] is not None
    inbound.record("gmail", [])
    with store.connect() as conn:
        kept_text = conn.execute("SELECT COUNT(*) FROM signals WHERE body IS NOT NULL OR meta IS NOT NULL").fetchone()[0]
        conn.execute("DELETE FROM signals")
    assert kept_text == 0  # a cleared signal keeps no message text


def test_asana_assignment():
    task = inbound.parse_asana_assignment("\U0001f4e5 Gregor assigned you a task: error wrongly reported [Web Platform (An\u017ee)]")
    assert task == {"title": "error wrongly reported", "project": "Web Platform"}
    assert inbound.parse_asana_assignment("\U0001f449 You have unread notifications") is None


def test_commands_grant_nothing():
    assert not [v for v in commands.VERBS if any(bad in v for bad in commands.FORBIDDEN)]
    assert commands.parse("restart the daemon") is None and commands.parse("resolv loop-crash x") is None
    assert commands.parse("resolve loop-crash it stopped") == ("resolve", ["loop-crash", "it", "stopped"])
    now = time.time()
    a = commands.receive_message({"id": "m1", "message": "snooze 3", "time": now})
    assert a["outcome"] == "pending" and a["verb"] == "snooze"
    assert commands.receive_message({"id": "m1", "message": "snooze 3", "time": now})["outcome"] == "duplicate"
    assert commands.receive_message({"id": "m2", "message": "approve everything", "time": now})["outcome"] == "unknown"
    assert commands.receive_message({"id": "m3", "message": "snooze 3", "time": now - 999999})["outcome"] == "stale"
    assert commands.receive_message({"message": "status"})["outcome"] == "refused"
    rows = {r["message_id"]: r["outcome"] for r in commands.recent(50)}
    assert rows == {"m1": "pending", "m2": "unknown", "m3": "stale"}  # refusals are on file
    claimed = commands.claim()
    assert [(c["verb"], c["args"]) for c in claimed] == [("snooze", ["3"])] and commands.claim() == []  # handed over once
    assert commands.finish(claimed[0]["id"], "done", "pings muted for 3h") is True
    assert commands.finish(claimed[0]["id"], "done", "again") is False  # only a running row can be finished
    assert {r["message_id"]: r["outcome"] for r in commands.recent(50)}["m1"] == "done"


def test_command_poll_is_three_valued():
    real = commands.urllib.request.urlopen
    on = {"command": {"enabled": True}}

    def boom(request, timeout=10):
        raise OSError("ntfy is down")

    commands.urllib.request.urlopen = boom
    try:
        assert commands.fetch(on) is None
        assert commands.receive(on) == {"state": "failed", "results": []}  # could not ask is not "nothing was said"
    finally:
        commands.urllib.request.urlopen = real
    assert commands.receive({"command": {"enabled": False}})["state"] == "disabled"


def _ntfy_stats(ts, subscribers):
    stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(ts))
    return (f"{stamp} INFO Server stats (emails_received=0, messages_cached=26, messages_published=169, "
            f"subscribers={subscribers}, tag=manager, topics_active=3, users=3, visitors=7)\n")


def _caddy_entry(ts, uri, *, status=101, host="ntfy.unspokenmallard.xyz", agent="ntfy/1.25.2 (play; Android 16; SDK 36)",
                 ip="46.122.17.209", duration=3000.0):
    entry = {"level": "info", "ts": ts, "logger": "http.log.access", "msg": "handled request",
             "request": {"remote_ip": ip, "client_ip": ip, "proto": "HTTP/1.1", "method": "GET", "host": host, "uri": uri,
                         "headers": {"Authorization": ["REDACTED"], "User-Agent": [agent]}},
             "duration": duration, "status": status}
    return json.dumps(entry, separators=(",", ":")) + "\n"


def test_delivery_is_three_valued():
    now = time.time()
    ntfy, caddy = TMP / "ntfy.log", TMP / "caddy.log"
    cfg = {"ping": {"enabled": True, "url": "http://127.0.0.1:2586/jarvis"}, "reports": {"enabled": True, "topic": "jarvis-reports"},
           "delivery": {"ntfy_log": str(ntfy), "caddy_log": str(caddy), "app_host": "ntfy.unspokenmallard.xyz",
                        "alert_after_s": 7200, "stats_max_age_s": 300}}
    every, two = "/jarvis,jarvis-cmd,jarvis-reports/ws", "/jarvis-cmd,jarvis-reports/ws"

    def span(oldest, newest, subscribers):
        return "".join(_ntfy_stats(now - s, subscribers) for s in range(oldest, newest - 1, -60))

    def state(stats, requests=""):
        ntfy.write_text(stats, encoding="utf-8")
        caddy.write_text(requests, encoding="utf-8")
        return delivery.phone_state(cfg, now)

    def keys(st):
        return [k for k, _m in st["findings"]]

    st = state(span(3600, 30, 3), _caddy_entry(now - 900, every))
    assert st["state"] == delivery.CONNECTED and st["healthy"] is True and not st["findings"]
    st = state(span(6 * 3600, 5 * 3600, 3) + span(5 * 3600 - 60, 30, 0), _caddy_entry(now - 5 * 3600, every))
    assert keys(st) == ["delivery:disconnected"] and st["healthy"] is False and abs((st["contact_at"] or 0) - (now - 5 * 3600)) <= 61
    st = state(span(3600, 1800, 3) + span(1740, 30, 0), _caddy_entry(now - 1800, every))
    assert st["state"] == delivery.DISCONNECTED and not st["findings"] and st["healthy"] is True
    st = state(span(3600, 30, 2), _caddy_entry(now - 600, two))
    assert keys(st) == ["delivery:topics"] and st["missing"] == ["jarvis"] and st["healthy"] is False  # a count is not delivery
    st = state(span(3600, 30, 3), _caddy_entry(now - 600, two))
    assert not st["findings"] and st["healthy"] is None
    st = state(span(3600, 30, 2), _caddy_entry(now - 900, two) + _caddy_entry(now - 300, every, status=403)
               + _caddy_entry(now - 200, every, agent="Mozilla/5.0 (Linux; Android 10; K)"))
    assert st["missing"] == ["jarvis"]
    st = state(span(3600, 30, 0), _caddy_entry(now - 1500, "/jarvis/json?poll=1&since=abc", status=200, duration=0.3)
               + _caddy_entry(now - 1400, "/jarvis-reports/json?poll=1&since=abc", status=200, duration=0.3))
    assert st["state"] == delivery.DISCONNECTED and not st["findings"]
    ntfy.unlink(missing_ok=True)
    st = delivery.phone_state(cfg, now)
    assert st["state"] == delivery.UNKNOWN and st["healthy"] is None and keys(st) == ["delivery:unknown"]
    st = state(span(3600, 1200, 3), _caddy_entry(now - 1300, every))
    assert st["state"] == delivery.UNKNOWN and "not running" in st["detail"]
    assert list(delivery.required_topics(cfg)) == ["jarvis", "jarvis-reports"]
    assert list(delivery.required_topics({**cfg, "reports": {"enabled": False, "topic": "jarvis-reports"}})) == ["jarvis"]
    ntfy.write_text(_ntfy_stats(now - 30, 0), encoding="utf-8")
    assert "ntfy had 0 subscribers" in delivery.push_note({"delivery": {"ntfy_log": str(ntfy)}})


def test_service():
    from starlette.testclient import TestClient

    import app as server

    with TestClient(server.app, base_url="http://127.0.0.1:3026") as c:
        assert c.get("/health").json()["ok"] is True
        assert c.post("/ping", json={"kind": "counts", "counts": {}}, headers={"origin": "http://evil.example"}).status_code == 403
        assert c.post("/ping", json={"kind": "free-text"}).status_code == 400
        out = c.post("/ping", json={"kind": "counts", "counts": {"inbound": 1}}).json()  # herald config in scratch is empty: ping is off
        assert out["delivered"] is False and out["reason"] == "ping disabled" and ping.check(out["body"])
        assert c.post("/toast", json={"title": "t"}, headers={"origin": "http://evil.example"}).status_code == 403
        assert c.get("/inbound").json() == {"waiting": []}
        assert c.post("/inbound/update", json={"ids": [1], "fields": {"title": "x"}}).status_code == 400  # not a judgement column
        assert c.post("/run/command-poll", json={}).json()["state"] == "disabled"
        assert c.post("/commands/claim", json={}).json() == {"commands": []}
        assert c.get("/delivery").json()["state"] in (delivery.UNKNOWN, delivery.CONNECTED, delivery.DISCONNECTED)
        assert c.get("/commands").status_code == 200
        assert c.get("/health", headers={"host": "evil.example"}).status_code == 400  # DNS rebinding


def test_cli_exit_codes():
    import subprocess
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); from herald.cli import main; sys.exit(main(['ping-test']))"],
                         capture_output=True, text=True, env={**os.environ, "HERALD_HOME": str(TMP)})
    assert out.returncode == 0 and "safe     : True" in out.stdout, out.stderr


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
    print(f"{len(tests) - failed} passed" + (f", {failed} FAILED" if failed else ""))
    sys.exit(1 if failed else 0)
