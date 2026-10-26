"""Hermod's CLI: `herald <command>`. Exit codes are what Jarvis's job scripts rely on.

  health                      the service's answer (what is on, whether a token exists)
  delivery [--json]           can the phone receive? exit 0 receiving, 1 not, 2 could not check
  inbound [poll|waiting]      the table; `poll` runs every source once (exit 0); `waiting` exits 1 if any
  commands [recent]           the command channel's last rows
  ping-test [--send]          what a ping would say now, whether it passes the content-free check
  asana-digest | calendar-sync | ticktick-tasks | messages-feed   the feed jobs (Jarvis schedules them through bin/herald-job.sh)
"""
from __future__ import annotations

import asyncio
import json
import runpy
import sys
import time
from pathlib import Path

from . import commands, delivery, inbound, ping, reports, store
from ._mimir import records as mr

JOBS = Path(__file__).resolve().parents[1] / "jobs"


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except mr.MimirDown as exc:
        print(f"Mimir is unreachable, so Hermod cannot read its signals or commands: {exc}", file=sys.stderr)
        return 3


def _main(argv: list[str] | None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("health", [])
    if cmd == "health":
        cfg = store.load_config()
        print(json.dumps({"outbox": mr.pending(store.SOURCE), "ping": ping.cfg(cfg)["enabled"], "reports": reports.cfg(cfg)["enabled"],
                          "commands": commands.cfg(cfg)["enabled"], "inbound": inbound.cfg(cfg)["enabled"],
                          "ntfy_token": bool(ping.token())}, indent=2))
        return 0
    if cmd == "delivery":
        state = delivery.phone_state()
        if "--json" in rest:
            print(json.dumps(state, indent=2))
        else:
            print(f"{delivery.headline(state)}: {state['detail']}")
            if state["remedy"]:
                print(f"  -> {state['remedy']}")
        return 2 if state["state"] == delivery.UNKNOWN else (0 if state["healthy"] else 1)
    if cmd == "inbound":
        if rest[:1] == ["poll"]:
            for line in asyncio.run(inbound.poll_all()):
                print(line)
            return 0
        print(inbound.report())
        return 1 if rest[:1] == ["waiting"] and inbound.waiting() else 0
    if cmd == "commands":
        for row in commands.recent(10):
            when = time.strftime("%m-%d %H:%M", time.localtime(row["received_at"]))
            print(f"  {when}  {str(row['verb'] or '?'):8} {row['outcome']:9} {row['detail'] or ''}")
        return 0
    if cmd == "ping-test":
        text = ping.compose({"review": 0, "inbound": 0, "incidents": 0, "refused": 0})
        print(f"url      : {ping.cfg()['url'] or '(unset)'}\nenabled  : {ping.cfg()['enabled']}\nbody     : {text!r}\n"
              f"safe     : {ping.is_content_free(text)}")
        if "--send" in rest:
            print(f"delivered: {ping.send(text)}")
        return 0
    if cmd in ("asana-digest", "calendar-sync", "ticktick-tasks", "messages-feed"):
        sys.argv = [cmd + ".py", *rest]
        runpy.run_path(str(JOBS / f"{cmd}.py"), run_name="__main__")
        return 0
    print(__doc__)
    return 2
