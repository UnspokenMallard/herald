"""Herald's store: state/herald.db (WAL), secrets, config.toml.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.DB_PATH`, `store.CONFIG`, `store.SECRETS`) the way Jarvis's
selftest does with `core.ROOT`. Stdlib only: imported by Jarvis's venv, Mind's venv and Herald's own.

Secrets live in `~/.herald/secrets/` (0600, never git: `.gitignore` names the directory). There is
deliberately no config key for any credential: config.toml is committed.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("HERALD_HOME", Path.home() / ".herald"))
STATE = ROOT / "state"
DB_PATH = STATE / "herald.db"
CONFIG = ROOT / "config.toml"
SECRETS = ROOT / "secrets"
log = logging.getLogger("herald")

class Partial(list):
    """A fetch result known to be incomplete (a page or per-channel limit was hit). It is recorded but
    must never clear the signals it does not name: `inbound.record` skips the `gone` sweep for it."""


_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


@contextlib.contextmanager
def connect():
    """`with connect() as conn:` commits or rolls back, and always closes (one handle per call)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def load_config() -> dict:
    """~/.herald/config.toml; missing means every module's own defaults (which are all "off")."""
    try:
        with open(CONFIG, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}


def secret(name: str) -> dict | None:
    """`secrets/<name>.json` as a dict, or None when absent or unreadable (logged, never raised)."""
    path = SECRETS / f"{name}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("herald: %s is unreadable: %s", path.name, exc)
        return None
    return doc if isinstance(doc, dict) else None


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(_META)
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


def get_meta(key: str) -> str | None:
    with connect() as conn:
        conn.execute(_META)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
