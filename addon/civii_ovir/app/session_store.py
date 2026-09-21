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
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

COOKIE_FILE = Path("/data/session_cookies.json")


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
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save the cookie jar (next cycle will log in again): %s", err)


async def restore(context) -> int:
    """Load the previous cycle's cookie jar into this context. Returns the
    number of cookies restored (0 if there was nothing usable). Never raises -
    an unreadable or stale jar just degrades to a normal login."""
    if not COOKIE_FILE.exists():
        return 0

    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        await context.add_cookies(cookies)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not restore the cookie jar, logging in from scratch: %s", err)
        return 0

    _LOGGER.info("Restored %d cookies from the previous cycle (domains: %s)", len(cookies), _domains(cookies))
    return len(cookies)
