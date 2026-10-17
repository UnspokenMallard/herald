# Next prompt: Herald

Herald is every channel between Anze and the machine, to be split out of Jarvis so Jarvis is an orchestrator. Designed on
2026-10-17 (`~/.herald/DESIGN.md`, decisions at its top); not built yet. Components are built one
per session, in the order Budget, Sentinel, Herald, Forge: each edits `~/.jarvis/config.toml`
and the daemon, so two at once would conflict.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing (Jarvis keeps running as is).

| When | Step | Waits on |
|---|---|---|
| when Sentinel is DONE (its NEXT-PROMPT says so) | build Herald from its DESIGN.md | the previous component's cutover |

## A: advance the work

> Build Herald. Read `~/.herald/DESIGN.md`, `~/.jarvis/NEXT-PROMPT.md` (its load-bearing rules),
> and how Budget was split (`~/.budget/DESIGN.md` "As built", Jarvis commit "Move the spend
> ledger, ceilings, limits and brake decision into Budget") and Mind (`~/.mind/DESIGN.md`): same
> layout, same cutover discipline. Work in the main checkouts, not a worktree: the cutover
> restarts the live daemon (`~/.jarvis/restart.sh` only; the HUD with
> `launchctl kickstart -k gui/$(id -u)/com.jarvis.hud`).
> Rules: Herald never alerts, pings or holds work; Jarvis triggers it and acts. Back up
> `jarvis.db` with `sqlite3 .backup` before moving any table; copy rows, never drop a table or
> delete a backup without Anze's confirmation. Redirect the component's data in `selftest.py`
> before anything imports it (as `BUDGET_HOME` is). Selftest, HUD tests, Mind and Budget tests
> must pass before and after. Ask Anze the design's open questions first (AskUserQuestion, one
> batch). When done, mark this file DONE, point Forge (`~/.forge`) at its build, add a dated
> section to `~/.jarvis/JOURNAL.md`, and stop and tell me if anything differs from the design.

## B: status only

> Read `~/.herald/DESIGN.md` and this file, and report whether Herald is built. Change nothing.

## C: something is broken

> Herald or the Jarvis daemon broke during the cutover. First, do not delete `jarvis.db`, a
> backup or a state file. Run `~/.jarvis/.venv/bin/python ~/.jarvis/status.py` and the selftest,
> read the daemon log, and report before fixing. `git -C ~/.jarvis log` shows the cutover commit
> to compare against.
