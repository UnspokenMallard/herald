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
