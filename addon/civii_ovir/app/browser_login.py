"""Real-browser login for Canal de Isabel II - Oficina Virtual (async port of
auth_refresher/browser_client.py from the Mac prototype).

The login form is protected by Google reCAPTCHA Enterprise (invisible mode),
which a plain HTTP client can't pass. A real, persistent Chrome profile
(channel="chrome", not Playwright's bundled Chromium) passes it cleanly most
of the time. When it doesn't, it escalates to a visible image challenge - this
runs headless (no display in this container), so instead of a human watching
a window, we notify Home Assistant and let the Ingress panel (app/panel.py)
screenshot/click into the same live `page` object until it clears or times out.
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


async def is_login_page(page: Page) -> bool:
    return await page.get_by_role("button", name="Entrar").first.count() > 0


async def dismiss_cookie_banner(page: Page) -> None:
    try:
        await page.get_by_role("button", name="Permitir todas las cookies").click(timeout=5000)
    except PlaywrightTimeoutError:
        pass


async def perform_login(
    page: Page, creds: dict, ha_client: HAClient, shared: SharedState
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
        return
    except PlaywrightTimeoutError:
        pass

    challenge_visible = await page.locator("iframe[src*='recaptcha'][src*='bframe']").count() > 0
    if not challenge_visible:
        raise RuntimeError(
            "Page did not navigate after clicking Entrar, and no reCAPTCHA challenge "
            "appeared - login likely failed outright (check credentials)."
        )

    _LOGGER.warning("Visible reCAPTCHA challenge appeared - notifying and waiting for manual solve")
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

    _LOGGER.info("Login OK after manual challenge solve, url=%s", page.url)


async def ensure_logged_in(
    page: Page, creds: dict, ha_client: HAClient, shared: SharedState
) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    if await is_login_page(page):
        await perform_login(page, creds, ha_client, shared)
    else:
        _LOGGER.info("Already logged in (persistent session still valid)")
