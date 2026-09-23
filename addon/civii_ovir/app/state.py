"""Small pieces of state shared between the fetch loop and the Ingress panel.

Both run as asyncio tasks in the same event loop (single-threaded, no locks
needed) inside app/main.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SharedState:
    page: Optional[object] = None  # playwright.async_api.Page, once a browser is open
    challenge_active: bool = False
    last_fetch_at: Optional[str] = None
    last_error: Optional[str] = None
    last_readings_count: int = 0
    status_message: str = "Starting..."
    retry_requested: asyncio.Event = field(default_factory=asyncio.Event)
    # Consecutive reCAPTCHA challenges with no successful login in between.
    # Reset to 0 on any successful login (automatic or manual). See
    # browser_login.py's SILENT_RETRY_LIMIT - the backoff before we bother the
    # user is keyed off this counter.
    consecutive_challenge_failures: int = 0
