#!/Users/anzes/.herald/.venv/bin/python
"""One-off: copy Hermod's data out of Jarvis (2026-10-17). Copies, never moves: Jarvis's tables and
files stay where they are until Anze confirms they can go.

  * `signals` and `commands` rows from ~/.jarvis/jarvis.db into state/herald.db, ids kept (the
    briefing's `thread` keys and a command's id are row ids).
  * the channel secrets from ~/.jarvis/secrets/ into ~/.herald/secrets/ (0600): ntfy, gmail,
    mattermost, asana and, when it exists, calendar.
  * the feed jobs' state files from ~/.jarvis/state/: asana-digest, asana-first-critical, asana-tasks,
    calendar.

Run it with `--dry-run` first. It refuses to overwrite a table that already has rows.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermod import commands, inbound, store  # noqa: E402

J = Path.home() / ".jarvis"
SECRETS = ("ntfy", "gmail", "mattermost", "asana", "calendar")
STATE = ("asana-digest.json", "asana-first-critical.json", "asana-tasks.json", "calendar.json")


def copy_table(src: sqlite3.Connection, table: str, dry: bool) -> int:
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id").fetchall()
    with store.connect() as dst:
        have = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
        keep = [c for c in cols if c in have]
        if dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
            raise SystemExit(f"{table} in {store.DB_PATH} already has rows: refusing to copy over them")
        if not dry:
            idx = [cols.index(c) for c in keep]
            dst.executemany(f"INSERT INTO {table}({', '.join(keep)}) VALUES({', '.join('?' for _ in keep)})",
                            [tuple(r[i] for i in idx) for r in rows])
    return len(rows)


def main() -> int:
    dry = "--dry-run" in sys.argv
    inbound.ensure_schema()
    commands.ensure_schema()
    src = sqlite3.connect(f"file:{J / 'jarvis.db'}?mode=ro", uri=True)
    for table in ("signals", "commands"):
        print(f"{table}: {copy_table(src, table, dry)} rows{' (dry run)' if dry else ''}")
    store.SECRETS.mkdir(parents=True, exist_ok=True)
    os.chmod(store.SECRETS, 0o700)
    for name in SECRETS:
        path = J / "secrets" / f"{name}.json"
        if not path.exists():
            print(f"secret {name}: not in Jarvis, skipped")
            continue
        if not dry:
            target = store.SECRETS / f"{name}.json"
            shutil.copyfile(path, target)
            os.chmod(target, 0o600)
        print(f"secret {name}: copied (0600)")
    store.STATE.mkdir(parents=True, exist_ok=True)
    for name in STATE:
        path = J / "state" / name
        if path.exists() and not dry:
            shutil.copy2(path, store.STATE / name)
        print(f"state {name}: {'copied' if path.exists() else 'absent'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
