"""The desktop channel: a macOS notification through osascript (Notification Center).

Moved from Jarvis's daemon/core.py `_toast` on 2026-10-17 (Herald). This is the alert sink -- Jarvis
decides which alerts reach it (severity, cooldown, backoff, postponing) and Hermod only shows them.
Local to this Mac on purpose: alert text carries file paths, job names and error strings, and none of
that goes to a third-party notification service. It is NOT the phone ping and never reaches ntfy.
"""
from __future__ import annotations

import json
import subprocess


def show(title: str, body: str) -> dict:
    """Raise one notification. Shelling out rather than adding a package: no dependency. Fire and
    forget with a short timeout, because an alert that blocks is worse than a missed one.

    Do not swallow the result: a notifier that silently fails is worse than no notifier -- it produces
    the belief that alerts are being delivered. Returns `{"shown": bool, "reason": str}`.
    """
    # JSON string literals are valid AppleScript string literals for this text.
    script = f"display notification {json.dumps(body[:250])} with title {json.dumps(title[:80])}"
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"shown": False, "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"shown": False, "reason": f"rc={proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}"}
    return {"shown": True, "reason": ""}
