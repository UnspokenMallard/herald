# Next prompt: Herald

DONE 2026-10-17: Herald is built and live (`~/.herald/DESIGN.md`, "As built"; Jarvis commit "Move the channels, the
signal table and the phone commands into Herald"). What is left is a check of the first nights and cleanup that waits on
Anze. Forge is next (`~/.forge/NEXT-PROMPT.md`).

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing.

| When | Step | Waits on |
|---|---|---|
| 2026-10-18 08:00 | first-night check: `herald delivery` says receiving; `herald inbound` and `herald commands` show recent rows; the 07:25 `report-push-morning` result in `jarvis.db` says `pushed ... message(s)`; `~/.herald/state/asana-digest.json` and `calendar.json` fresh (jobs `asana-digest` hourly, `calendar-sync` every 10 min); no open `herald-unreachable` incident (`~/.sentinel/bin/sentinel incidents --open`) | the first night |
| after that check, Anze | drop `signals` and `commands` from `~/.jarvis/jarvis.db`, delete `jarvis.db.bak-herald`, and delete the old copies `~/.jarvis/secrets/{ntfy,gmail,mattermost,asana}.json` (Herald has its own, 0600) | Anze's confirmation |
| Anze's call, later | calendar secret addresses into `~/.herald/secrets/calendar.json` (still waiting from before the build), then `[ping] meeting_guard = true` in Jarvis's config | Anze; the work account may have it disabled |

## Audit 2026-10-21: your items

From the Jarvis audit (HUD task audit-jarvis-and-all-its-com-e5a9). The evidence, effort, risk and
done-criteria of each item are in `~/.jarvis/AUDIT-2026-10-21.md`; the phases and Anze's decisions
are in `~/.jarvis/NEXT-PROMPT.md`. Build an item only after Anze approves its phase. Prompt A below
covers these rows too. The rule stays: this component measures or knows; Jarvis alerts and acts.

- **A08 (Phase 0, Jarvis side)** Ping repeats an unchanged state (135 pings in 14 days, 20 on 10-20).
  The fix is in Jarvis's ping; if it needs a "last delivered state" from here, add it read-only.
- **A21 (Phase 2)** The calendar is empty: `calendar.json` has 0 events and 0 feeds, no secret.
  Anze picks the iCal secret address or the Google Calendar API read-only (D8).
- **A22 (Phase 2)** Pre-meeting brief, after A21 and a clean week of events.
- **A23 (Phase 2)** Inbound: Jarvis has no consumer for `inbound-truncated`; every signal clears as
  "gone" and there is no "answered" clear; 16 of 36 Gmail signals were never judged; no deadline
  watcher for Asana (96 open tasks) or TickTick (16 items).
- **A28 (Phase 2, last)** `deliver()` app inbox (proactive design step 10), after A08 and A26.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| now, Anze | D8: calendar source | Anze |
| with Phase 2 | A23, then A21/A22 once the calendar has events, then A28 | Phase 2 approval |

## A: advance the work

> Read `~/.herald/DESIGN.md` ("As built") and this file. Do the first row whose date has passed and report.
> Rules: Herald never alerts, pings on its own or holds work; Jarvis triggers every poll and push and decides whether and
> when. The ping stays counts-only: do not widen `herald/ping.py`'s vocabulary or named-line shapes, and never route report or
> briefing text through it. Nothing ever posts, sends, reacts or DMs as Anze: Gmail and Mattermost get drafts only (no SMTP,
> no `/posts`). There is no fallback past Herald: nothing in the daemon talks to ntfy or reads a channel credential. A failed
> poll returns None, never an empty list. Never add a granting verb to `herald.commands.VERBS`. Never put a credential in
> config.toml or memory. Never drop a table or delete a backup or a secret copy without Anze's confirmation. Restart the
> daemon only with `~/.jarvis/restart.sh`; a config edit hot-reloads into the running daemon, so edit config and restart in one
> step. Stop and tell me if anything differs from what this file expects.

## B: status only

> Run `~/.herald/bin/herald health`, `~/.herald/bin/herald delivery`, `~/.herald/bin/herald inbound`,
> `~/.herald/bin/herald commands`, `~/.herald/bin/install-launchd status` and
> `~/.herald/.venv/bin/python ~/.herald/tests/test_herald.py`. Change nothing.

## C: something is broken

> A channel to the phone, the signal table or the Herald service broke. First, do not delete `state/herald.db`, a backup
> or a secret: they are the evidence. Copy the db with `sqlite3 .backup`, read `~/.herald/state/logs/server.log` and
> `~/.jarvis/logs/jarvis.log`, run `curl -s http://127.0.0.1:3026/health` and `~/.jarvis/.venv/bin/python
> ~/.jarvis/status.py`, and report before fixing. Do not restart the service before reading its log. `git -C ~/.jarvis log`
> shows the cutover commit; `jarvis.db.bak-herald` and the untouched `signals` / `commands` tables hold the pre-cutover rows.
