"""Real-browser login for Canal de Isabel II - Oficina Virtual (async port of
auth_refresher/browser_client.py from the Mac prototype).

The login form is protected by Google reCAPTCHA Enterprise (invisible mode),
which a plain HTTP client can't pass. A real, persistent Chrome profile
(channel="chrome", not Playwright's bundled Chromium) passes it cleanly most
of the time. When it doesn't, it escalates to a visible image challenge - this
runs headless (no display in this container), so instead of a human watching
a window, we notify Home Assistant and let the Ingress panel (app/panel.py)
screenshot/click into the same live `page` object until it clears or times out.

Challenges are not always a dead end on their own: reCAPTCHA's risk score is
per-attempt, and in practice a challenge that appears on one cycle often does
not reappear on the very next one, 10 minutes later, with nobody touching
anything (observed directly in this add-on's own logs). So a lone challenge is
handled silently - fail this cycle fast, let the next scheduled cycle retry -
and only escalates to notifying the user once a challenge repeats back-to-back
with no successful login in between. See the silent_recaptcha_retries add-on option below.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .ha_client import HAClient
from .state import SharedState

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://oficinavirtual.canaldeisabelsegunda.es"
LOGIN_TIMEOUT_MS = 30_000
MANUAL_CHALLENGE_TIMEOUT_MS = 10 * 60 * 1000

# How many consecutive challenge failures (no successful login in between) to
# absorb silently before notifying the user and waiting for a manual solve.
# 1 = the first challenge always gets one silent auto-retry on the next
# scheduled cycle; the user is only bothered if it happens twice in a row.
# Configurable via the add-on's silent_recaptcha_retries option (0 restores
# the old behaviour of notifying on every challenge); this is just the
# fallback for direct callers that don't pass one explicitly.
DEFAULT_SILENT_RETRY_LIMIT = 1


_SESSION_CONFIG_JS = """() => {
  const out = {};
  try {
    if (window.Liferay && Liferay.Session) {
      // Liferay publishes its own session length to the browser so it can pop
      // the "your session is about to expire" warning. Both are milliseconds.
      for (const key of ['sessionLength', 'warningLength', 'autoExtend']) {
        try {
          out[key] = Liferay.Session.get ? Liferay.Session.get(key) : Liferay.Session[key];
        } catch (e) { /* key absent on this version */ }
      }
    } else {
      out.liferaySession = 'absent';
    }
  } catch (e) { out.error = String(e); }
  return out;
}"""


async def log_session_config(page: Page) -> None:
    """Log the portal's advertised session timeout, so the right
    scan_interval_minutes can be chosen from evidence instead of bisected over
    hours of failed logins. Best-effort: never let a diagnostic break a cycle."""
    try:
        cfg = await page.evaluate(_SESSION_CONFIG_JS)
    except Exception as err:  # noqa: BLE001
        _LOGGER.info("Could not read the portal's session config: %s", err)
        return

    length_ms = cfg.get("sessionLength")
    if isinstance(length_ms, (int, float)) and length_ms > 0:
        _LOGGER.info(
            "Portal session timeout: %.1f min (warning at %.1f min, autoExtend=%s) - raw=%s",
            length_ms / 60000,
            (cfg.get("warningLength") or 0) / 60000,
            cfg.get("autoExtend"),
            cfg,
        )
    else:
        _LOGGER.info("Portal session config (no usable sessionLength): %s", cfg)


async def is_login_page(page: Page) -> bool:
    return await page.get_by_role("button", name="Entrar").first.count() > 0


async def dismiss_cookie_banner(page: Page) -> None:
    try:
        await page.get_by_role("button", name="Permitir todas las cookies").click(timeout=5000)
    except PlaywrightTimeoutError:
        pass


async def perform_login(
    page: Page,
    creds: dict,
    ha_client: HAClient,
    shared: SharedState,
    silent_retry_limit: int = DEFAULT_SILENT_RETRY_LIMIT,
) -> None:
    _LOGGER.info("Navigating to %s", BASE_URL)
    shared.status_message = "Logging in..."
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)

    await dismiss_cookie_banner(page)

    await page.get_by_role("button", name="Entrar").first.click()
    await page.wait_for_selector(
        "#_com_vass_cyii_ovir_login_module_loginForm", timeout=LOGIN_TIMEOUT_MS
    )

    await page.locator("#radioParticularLoginDesktop").dispatch_event("click")
    await page.evaluate("document.getElementById('tipoUsuario').value = 'PARTICULAR'")

    await page.locator("#tipoDocumento").select_option(creds["document_type"])
    await page.locator("#numeroDocumento").fill(creds["username"])
    await page.locator("#passwordFieldHidden").fill(creds["password"])

    url_before = page.url
    await page.locator("#btLogin").click()

    try:
        await page.wait_for_function(
            "url => window.location.href !== url", arg=url_before, timeout=8_000
        )
        shared.consecutive_challenge_failures = 0
        return
    except PlaywrightTimeoutError:
        pass

    challenge_visible = await page.locator("iframe[src*='recaptcha'][src*='bframe']").count() > 0
    if not challenge_visible:
        raise RuntimeError(
            "Page did not navigate after clicking Entrar, and no reCAPTCHA challenge "
            "appeared - login likely failed outright (check credentials)."
        )

    shared.consecutive_challenge_failures += 1
    attempt = shared.consecutive_challenge_failures

    if attempt <= silent_retry_limit:
        # Don't wake the user for a challenge that may well clear on its own -
        # fail this cycle fast and let the next scheduled cycle try again.
        # main.py's own except-block will push this message to the status
        # sensor (detail=str(err)) - no need to call ha_client here too.
        _LOGGER.warning(
            "Visible reCAPTCHA challenge appeared (silent retry %d/%d) - not "
            "notifying, next scheduled cycle will try again on its own",
            attempt, silent_retry_limit,
        )
        raise RuntimeError(
            f"reCAPTCHA challenge appeared (silent retry {attempt}/{silent_retry_limit}, no notification sent)"
        )

    _LOGGER.warning(
        "Visible reCAPTCHA challenge appeared again (%d in a row) - notifying and waiting for manual solve",
        attempt,
    )
    shared.challenge_active = True
    shared.status_message = "Waiting for reCAPTCHA to be solved manually"
    await ha_client.set_status("waiting_captcha", detail="Visible reCAPTCHA challenge - solve it via the Ingress panel")
    await ha_client.notify(
        "Ha aparecido un reCAPTCHA visual al iniciar sesion. Abre el panel del "
        "add-on 'Canal Isabel II' para resolverlo (tienes 10 minutos)."
    )
    try:
        await page.wait_for_function(
            "url => window.location.href !== url",
            arg=url_before,
            timeout=MANUAL_CHALLENGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"reCAPTCHA challenge was not solved within {MANUAL_CHALLENGE_TIMEOUT_MS // 60_000} minutes."
        )
    finally:
        shared.challenge_active = False
        await ha_client.dismiss_notification()

    shared.consecutive_challenge_failures = 0
    _LOGGER.info("Login OK after manual challenge solve, url=%s", page.url)


async def ensure_logged_in(
    page: Page,
    creds: dict,
    ha_client: HAClient,
    shared: SharedState,
    silent_retry_limit: int = DEFAULT_SILENT_RETRY_LIMIT,
) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    if await is_login_page(page):
        await perform_login(page, creds, ha_client, shared, silent_retry_limit)
    else:
        _LOGGER.info("Already logged in (persistent session still valid)")
        shared.consecutive_challenge_failures = 0
    await log_session_config(page)
