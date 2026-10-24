"""Hermod's store: secrets and config.toml. The signals and commands live in Mimir (hermod/_mimir.py).

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.CONFIG`, `store.SECRETS`) the way Jarvis's
selftest does with `core.ROOT`. Stdlib only: imported by Jarvis's venv, Mimir's venv and Hermod's own.

Secrets live in `~/.herald/secrets/` (0600, never git: `.gitignore` names the directory). There is
deliberately no config key for any credential: config.toml is committed.
"""
from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("HERMOD_HOME", Path.home() / ".herald"))
STATE = ROOT / "state"
SOURCE = "hermod"  # the prefix of every record kind Hermod writes in Mimir
CONFIG = ROOT / "config.toml"
SECRETS = ROOT / "secrets"
log = logging.getLogger("hermod")

class Fetched(list):
    """A fetch result that can say why a signal left: `answered` holds the external ids Anze replied to, so
    `inbound.record` clears them as "answered" rather than "gone" (audit A23). A plain list works too."""
    answered: frozenset = frozenset()


class Partial(Fetched):
    """A fetch result known to be incomplete (a page or per-channel limit was hit). It is recorded but
    must never clear the signals it does not name: `inbound.record` skips the `gone` sweep for it."""


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
        log.warning("hermod: %s is unreadable: %s", path.name, exc)
        return None
    return doc if isinstance(doc, dict) else None
