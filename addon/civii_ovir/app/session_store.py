"""Persist the browser's cookie jar across browser restarts.

The add-on launches a fresh Chrome per fetch cycle and closes it afterwards so
idle RAM drops back to baseline (see _launch_browser in main.py). The on-disk
Chrome profile survives that, but the portal login does not: JSESSIONID and the
F5 anti-bot TS* cookies are *session* cookies (no expiry), which Chrome keeps in
memory only and drops on close. Without this module every cycle therefore lands
on the login form again and rolls the reCAPTCHA dice.

So the jar is saved and restored explicitly rather than relying on Chrome's
session-restore behaviour, which is undocumented and a Chrome update could take
away. Playwright round-trips session cookies losslessly: context.cookies()
returns them with expires == -1 and add_cookies() accepts them back in that form.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

COOKIE_FILE = Path("/data/session_cookies.json")


def describe_expiries(cookies: list[dict]) -> str:
    """One-line summary of how long each cookie claims to live, for diagnosing
    how long a reused session actually stays valid.

    Names and lifetimes only - never values. Note that the portal's own
    JSESSIONID is a *session* cookie (expires == -1): it carries no lifetime
    at all, because the timeout that matters is enforced server-side. That is
    exactly why it does not survive a browser restart on its own.
    """
    now = time.time()
    parts = []
    for c in sorted(cookies, key=lambda x: x.get("name", "")):
        expires = c.get("expires", -1)
        if expires is None or expires < 0:
            parts.append(f"{c.get('name')}=session")
        else:
            parts.append(f"{c.get('name')}=+{int((expires - now) / 60)}min")
    return ", ".join(parts)


def _domains(cookies: list[dict]) -> list[str]:
    """Distinct cookie domains, for logging.

    Cookie *values* are never logged - JSESSIONID is a live authentication
    token and add-on logs regularly get pasted into bug reports.
    """
    return sorted({c.get("domain", "?") for c in cookies})


async def save(context) -> None:
    """Write the context's whole cookie jar to /data. Never raises - a failure
    here only costs us a login next cycle."""
    try:
        cookies = await context.cookies()
        COOKIE_FILE.write_text(json.dumps(cookies), encoding="utf-8")
        COOKIE_FILE.chmod(0o600)
        _LOGGER.info("Saved %d cookies for the next cycle (domains: %s)", len(cookies), _domains(cookies))
        _LOGGER.info("Cookie lifetimes: %s", describe_expiries(cookies))
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save the cookie jar (next cycle will log in again): %s", err)


async def restore(context) -> int:
    """Load the previous cycle's cookie jar into this context. Returns the
    number of cookies restored (0 if there was nothing usable). Never raises -
    an unreadable or stale jar just degrades to a normal login."""
    if not COOKIE_FILE.exists():
        return 0

    try:
        # What the on-disk Chrome profile carried over on its own, *before* we
        # add anything. If the portal's session cookie shows up here too, this
        # module is redundant; if it only appears after the restore below, the
        # restore is what keeps the session alive.
        from_profile = await context.cookies()
        _LOGGER.info(
            "Chrome profile alone provided %d cookies: %s",
            len(from_profile),
            sorted(c.get("name", "?") for c in from_profile) or "(none)",
        )

        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        await context.add_cookies(cookies)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not restore the cookie jar, logging in from scratch: %s", err)
        return 0

    _LOGGER.info("Restored %d cookies from the previous cycle (domains: %s)", len(cookies), _domains(cookies))
    return len(cookies)
