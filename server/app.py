"""Hermod's HTTP service on 127.0.0.1:3026 (launchd com.herald.server): every channel between Anze and
the machine, for callers that are not Python on this Mac (the HUD, scripts) and for Jarvis, which sends
its pings, reports, briefings, drafts and alert toasts through it so the credentials live in this one
process and the channel rules are enforced in one place. Python readers (Jarvis, Mimir) import the
`hermod` library directly for the signals and the phone-state check (same code, same records in Mimir).

Localhost only: the host check refuses DNS rebinding, and a write carrying an Origin header (any browser
page) is refused, so no web page can make Hermod say or save anything through Anze's browser.
Hermod never alerts, pings on its own or holds work: Jarvis triggers every poll and every push.
It never posts, sends, reacts or DMs as Anze: Gmail and Mattermost get drafts only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermod import briefing, commands, delivery, drafts, inbound, ping, reports, store, tasks, toast  # noqa: E402
from hermod._mimir import records as mr  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

HOST, PORT = "127.0.0.1", 3026


def get(fn):
    async def endpoint(r: Request):
        try:
            return JSONResponse(await run_in_threadpool(fn, r))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except mr.MimirDown as e:
            return JSONResponse({"error": f"Mimir is unreachable: {e}"}, status_code=503)
    return endpoint


def post(fn):
    """`fn(body: dict) -> dict` (sync or async). A browser page may not write."""
    async def endpoint(r: Request):
        if r.headers.get("origin"):
            return JSONResponse({"error": "writes are not accepted from a browser page"}, status_code=403)
        try:
            body = await r.json()
            body = body if isinstance(body, dict) else {}
            out = fn({**body, "_path": dict(r.path_params)})
            if hasattr(out, "__await__"):
                out = await out
            return JSONResponse(out)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        except mr.MimirDown as e:
            return JSONResponse({"error": f"Mimir is unreachable: {e}"}, status_code=503)
    return endpoint


def sync_post(fn):
    async def run(body):
        return await run_in_threadpool(fn, body)
    return post(run)


def do_ping(b: dict) -> dict:
    text = ping.compose_payload(b)
    out = ping.send(text, incident_id=b.get("incident_id"), retry_triage=b.get("retry_triage"), kind=ping.kind_of(b))
    return {**out, "body": text}


def do_ping_hud(b: dict) -> dict:
    text = ping.compose_hud(int(b.get("waiting") or 0), int(b.get("done") or 0), int(b.get("open") or 0),
                            [str(i) for i in b.get("items") or []])
    return {**ping.send_hud(text, str(b.get("click") or ""), kind=ping.kind_of({**b, "kind": "hud"})), "body": text}


def do_ping_persona(b: dict) -> dict:
    text = ping.compose_persona(str(b.get("name") or ""), str(b.get("severity") or "warn"), str(b.get("summary") or ""))
    return {**ping.send_persona(text, str(b.get("click") or "")), "body": text}


def do_report(b: dict) -> dict:
    return reports.push(str(b["title"]), str(b.get("text") or ""), b.get("spoken"))


def do_briefing(b: dict) -> dict:
    return briefing.send(list(b.get("new") or []), list(b.get("reminders") or []))


async def do_draft(b: dict) -> dict:
    return {"result": await drafts.save(str(b["channel"]), dict(b.get("meta") or {}), str(b["text"]))}


def do_toast(b: dict) -> dict:
    return toast.show(str(b.get("title") or "Jarvis"), str(b.get("body") or ""))


async def do_inbound_poll(_b: dict) -> dict:
    return {"results": await inbound.poll_all()}


def do_inbound_update(b: dict) -> dict:
    return {"updated": inbound.update([str(i) for i in b.get("ids") or []], **dict(b.get("fields") or {}))}


def do_task(b: dict) -> dict:
    return tasks.create(str(b["signal_id"]), str(b["title"]), str(b.get("note") or ""))


def do_command_poll(_b: dict) -> dict:
    return commands.receive()


def do_claim(_b: dict) -> dict:
    return {"commands": commands.claim()}


def do_ack(b: dict) -> dict:
    return {"ok": commands.finish(str(b["_path"]["id"]), str(b.get("outcome") or "failed"), str(b.get("detail") or ""))}


def health(_r) -> dict:
    cfg = store.load_config()
    return {"ok": True, "outbox": mr.pending(store.SOURCE),
            "channels": {"ping": ping.cfg(cfg)["enabled"], "reports": reports.cfg(cfg)["enabled"],
                         "briefing": briefing.cfg(cfg)["enabled"], "commands": commands.cfg(cfg)["enabled"],
                         "inbound": inbound.cfg(cfg)["enabled"]},
            "token": bool(ping.token())}


routes = [
    Route("/health", get(health)),
    Route("/delivery", get(lambda r: delivery.phone_state())),
    Route("/inbound", get(lambda r: {"waiting": inbound.waiting(r.query_params.get("source"))})),
    Route("/commands", get(lambda r: {"commands": commands.recent(int(r.query_params.get("limit", 20)))})),
    Route("/ping", sync_post(do_ping), methods=["POST"]),
    Route("/ping/hud", sync_post(do_ping_hud), methods=["POST"]),
    Route("/ping/persona", sync_post(do_ping_persona), methods=["POST"]),
    Route("/report", sync_post(do_report), methods=["POST"]),
    Route("/briefing", sync_post(do_briefing), methods=["POST"]),
    Route("/draft", post(do_draft), methods=["POST"]),
    Route("/toast", sync_post(do_toast), methods=["POST"]),
    Route("/run/inbound-poll", post(do_inbound_poll), methods=["POST"]),
    Route("/inbound/update", sync_post(do_inbound_update), methods=["POST"]),
    Route("/task", sync_post(do_task), methods=["POST"]),
    Route("/run/command-poll", sync_post(do_command_poll), methods=["POST"]),
    Route("/commands/claim", sync_post(do_claim), methods=["POST"]),
    Route("/commands/{id}/ack", sync_post(do_ack), methods=["POST"]),
]

app = Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
