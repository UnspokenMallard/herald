# Herald

Every channel between Anze and the machine, in both directions: the phone ping, reports,
briefings, reply drafts, the phone command channel, and the inbound signals (mail, Mattermost,
GitHub, Asana, calendar). Split out of Jarvis on 2026-10-17 so Jarvis is an orchestrator.

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.herald`, own git repo, code and `state/` together, own venv |
| Schedule | Jarvis triggers polls and pushes; Herald has no timers |
| Process | HTTP service on `127.0.0.1:3026` (launchd `com.herald.server`): many callers send through it |
| Sessions | Built in its own session after Sentinel, from this design |

## Boundary

Herald **carries and enforces the channel rules**. Jarvis decides *whether* and *when* to tell
Anze something and what a phone command does; Herald decides *how* it is said and refuses
anything a channel must not carry. Rules that move here and must be enforced in code, not
remembered:
- The ping carries counts and nothing else (`ping.is_content_free`).
- Reports go to their own topic and may carry content.
- Never post, send, react or DM as Anze: Gmail and Mattermost get drafts only (`drafts.py`).
- A phone command grants almost nothing: Herald receives and validates it; Jarvis executes the
  verb under its own ladder (`command.py` splits: transport here, verbs in Jarvis).

## What moves here

- Outbound: `daemon/ping.py`, `reports.py`, `delivery.py` (can the phone receive), `briefing.py`
  (Jarvis's voice), `drafts.py`, `webview.py`, handlers `report_push`, `delivery_check`,
  `scripts/hud-ping.py`.
- Inbound: `daemon/inbound.py`, `mattermost.py`, handlers `inbound_poll`, `command_poll`,
  `scripts/asana-digest.py`, `scripts/calendar-sync.py` (their state files move; Mind's
  attention feed and people sync read them from here after the move).
- Secrets for these channels (ntfy credentials, Gmail and Mattermost tokens, iCal addresses)
  move to `~/.herald/secrets/` (0600), never into git or memory.

Stays in Jarvis: what to say and when (the alert-to-ping decision, `reminders.py`,
`usertasks.py`, the briefing schedule), the verbs behind phone commands, the action ladder.

## Interface

- `POST /ping` `{counts}` (refused unless content-free), `POST /report` `{topic, title, body}`,
  `POST /briefing` `{signals}`, `POST /draft` `{channel, to, subject, body}`
- `GET /inbound` (waiting signals), `POST /run/inbound-poll`, `GET /delivery`
- `GET /commands` (pending phone commands), `POST /commands/{id}/ack`
- `GET /health`

## First steps for the build session

1. Read the docstrings of every module listed: most carry an incident that shaped them.
2. Map every caller of `ping.send`, `reports.*`, `drafts.*`, `inbound.*`, `briefing.*` and the
   ntfy topics (Jarvis, HUD `notify.js`, scripts, Mind).
3. Move secrets with their permissions, then outbound, then inbound, then commands.
4. Keep the delivery check passing throughout: a channel that silently stops is the failure
   this code was written against (twice in September).

## As built (2026-10-17)

Built and cut over in one session (Anze chose "everything in one session"; two answers shaped it: the ping split and the
Herald-down behaviour, below). Layout as Budget's: `herald/` library, `server/app.py` (launchd `com.herald.server`,
`bin/install-launchd [status]`, log `state/logs/server.log`), `bin/herald` (CLI) and `bin/herald-job.sh` (Jarvis's job entry
point), `jobs/` (the two feed scripts), `config.toml`, `tests/test_herald.py` (18 tests, scratch `HERALD_HOME`),
`tools/migrate-from-jarvis.py` (one-off). Jarvis's side is `daemon/herald_client.py`.

- **Library vs service.** Every send goes through the service on `127.0.0.1:3026`, so the credentials live in this one process
  and the rules are enforced in one place: `/ping`, `/ping/hud`, `/report`, `/briefing`, `/draft`, `/toast`, `/run/inbound-poll`,
  `/run/command-poll`, `/commands/claim`, `/commands/{id}/ack`, `/inbound/update`; reads `/health`, `/delivery`, `/inbound`,
  `/commands`. Python readers (Jarvis, Mind) import the library for the signal table and the phone-state check, as for Budget
  and Sentinel. Writes refuse an `Origin` header and any host but loopback.
- **Moved with their rows** (copied; the old tables stay in `jarvis.db` until Anze confirms dropping them; backup
  `~/.jarvis/jarvis.db.bak-herald`, deleted 2026-10-18): `signals` (40, ids kept) and `commands` (6) into `state/herald.db`. Secrets copied (0600) from
  `~/.jarvis/secrets/` to `~/.herald/secrets/`: ntfy, gmail, mattermost, asana (the Jarvis copies stay until Anze confirms).
  State files `asana-digest.json`, `asana-first-critical.json`, `asana-tasks.json`, `calendar.json` now live in `~/.herald/state/`;
  the HUD (`HERALD` in `hud/lib/util.js`), Mind's attention feed and people sync, `weekly-changes.py` and the ping's meeting
  guard read them from there. The HUD reads the signal table from `herald.db`.
- **The ping split (Anze's call).** Herald = the vocabulary, the one-line shapes, the sentence frames in Jarvis's voice, the
  buttons, the content-free check and the POST (`herald/ping.py`). Jarvis = whether and when: the change gate and interval
  floor, quiet hours, the weekend rule, the snooze, the meeting guard, what is counted (`daemon/ping.py`). Jarvis sends integers
  and names (`kind` = `counts`, `task`, `health`, `hud`); Herald composes, re-checks the finished body and refuses what is not
  content-free; Jarvis raises `ping-not-content-free` from the refusal, because Herald never alerts. The vocabulary was not
  widened: the named lines are the ones Anze approved on 2026-09-30, 10-05 and 10-14.
- **Herald down (Anze's call): fail loud, no fallback.** `herald_client.HeraldDown`; `herald-unreachable` alert (6 h cooldown,
  resolver reads `/health`, playbook rule). The ping and briefing save no state until delivery, so they retry on the next pass;
  a report push raises into its job's retries; a draft says "not saved" in the briefing; `inbound-poll` says nothing was polled
  and nothing cleared. Selftest asserts nothing in the daemon talks to ntfy, reads a channel credential or runs osascript.
- **Reports, briefing, drafts.** Jarvis keeps freshness, quiet hours, narration, the triage and drafting (brain calls), `due()`,
  the gates and the `publish-report` / `publish-briefing` / `save-draft` ladder checks. Herald splits, composes, posts and
  appends drafts. The Mattermost client still refuses `/posts` before the network; there is still no SMTP.
- **Phone commands.** Herald polls the topic with the command token, keeps the closed verb set (`herald.commands.VERBS`),
  records each message once by id, drops the unknown and the stale, and hands each valid command over once (`claim`: pending ->
  running). Jarvis runs the verb under its own rules and reports the outcome (`ack`); a crash between leaves a `running` row, never a
  second run. Selftest still asserts the verb table against the granting words.
- **Inbound.** Herald polls GitHub, Gmail and Mattermost (`gh` via a PATH set in the plist) and holds the Asana token; it clears a
  row only on a successful poll. Jarvis judges (Asana criticality, mail and chat triage) and writes verdicts back with
  `herald.inbound.update`, which accepts judgement columns only.
- **`core._push` is the desktop toast, not the ping.** The earlier note that its delivery half "becomes `POST /ping`" was wrong:
  `_push` raises a macOS notification with alert text. It now goes to Herald's `/toast` (`herald/toast.py` owns the osascript
  call). Jarvis keeps the severity gate, cooldown, backoff, postponing and `alerts.log`.
- Differences from the design above:
  - `POST /ping` takes the structured kinds above, not only `{counts}`; the finished body is what is checked.
  - `GET /commands` lists the recent rows; the hand-over is `POST /commands/claim` (it changes state).
  - **`webview.py` stays in Jarvis.** It renders Jarvis's own state (jobs, incidents, review, spend) read-only on `127.0.0.1:2587`;
    moving it would make Herald import Jarvis. It reads the signal table and the phone state from Herald's library.
  - `scripts/hud-ping.py` stays in Jarvis as the gate (snooze, quiet hours) in front of `/ping/hud`; the HUD still calls it.
  - Not moved: `reminders.py`, `usertasks.py` (they decide what to say), the briefing schedule, the ladder.

## TickTick tasks from actionable messages (2026-10-23)

`herald/tasks.py`, `POST /task {signal_id, title, note}`, `[tasks]` in config.toml. Jarvis's triage judges a Mattermost DM/mention
or work mail `actionable` (one more field in the same Haiku verdict) and asks for the task; Herald creates it in the Work list
and the next briefing line says "I added a task: ...". Create only. Dedup: `signals.task_id` is claimed before the call and looked
up across rows with the same (source, external_id); a failed create releases the claim, a crash between leaves a lost task, never
a duplicate. Needs the TickTick MCP token (`~/.config/ticktick-mcp/credentials.json`; a 401 means re-authenticate). A message
Anze reads in Mattermost before the poll never becomes a signal (`mattermost.unread_items` only lists unread), so it gets no task.
After merging: restart `com.herald.server` (`bin/install-launchd`) so `/task` exists.
