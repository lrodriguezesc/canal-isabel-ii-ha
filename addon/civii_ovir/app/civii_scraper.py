"""Consumption filter + CSV export (async port of auth_refresher/fetch_consumption.py).

Reproduces the exact portlet calls validated by hand against the real portal:
scrape a fresh p_auth -> POST the filter form -> GET the CSV export resource.
Runs through the browser's own request context (`context.request`), not
`requests` - see browser_login.py's docstring for why a plain HTTP client
gets rejected even with valid cookies.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup
from playwright.async_api import APIRequestContext, Page

from .browser_login import BASE_URL, ensure_logged_in
from .ha_client import HAClient
from .state import SharedState

_LOGGER = logging.getLogger(__name__)

CONSUMO_PAGE_PATH = "/group/ovir/consumo"
CONSUMO_PORTLET_ID = "ovirtelelecturamodule_INSTANCE_mljLXkIZgbLP"
CONSUMO_EXPORT_RESOURCE_ID = "/telelecturas/export-csv"

MAX_FETCH_RETRIES = 3
_P_AUTH_RE = re.compile(r"p_auth=([A-Za-z0-9_-]+)")


class SessionExpiredError(Exception):
    pass


class DataValidationError(Exception):
    pass


def _is_login_page(html: str) -> bool:
    return "numeroDocumento" in html and "loginForm" in html


def _extract_p_auth(html: str, near: str) -> str:
    idx = html.find(near)
    search_space = html[idx : idx + 20000] if idx != -1 else html
    match = _P_AUTH_RE.search(search_space)
    if not match:
        raise RuntimeError(f"Could not find p_auth token (near={near!r})")
    return match.group(1)


def _serialize_form(form, overrides: dict[str, str]) -> dict[str, str]:
    data: dict[str, str] = {}

    for tag in form.find_all("input"):
        name = tag.get("name")
        if not name:
            continue
        input_type = (tag.get("type") or "text").lower()
        if input_type in ("radio", "checkbox"):
            if tag.has_attr("checked"):
                data[name] = tag.get("value", "on")
            continue
        if input_type == "submit":
            continue
        data[name] = tag.get("value", "")

    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        options = select.find_all("option")
        chosen = next((o for o in options if o.has_attr("selected")), None)
        if chosen is None and options:
            chosen = options[0]
        data[name] = chosen.get("value", "") if chosen is not None else ""

    data.update(overrides)
    return data


async def _fetch_filter_form(request: APIRequestContext):
    resp = await request.get(BASE_URL + CONSUMO_PAGE_PATH)
    html = await resp.text()
    if _is_login_page(html):
        raise SessionExpiredError("Session expired while loading consumo page")

    soup = BeautifulSoup(html, "html.parser")
    date_input = soup.find(id="fechaDesde1")
    if date_input is None:
        raise RuntimeError("Could not find 'fechaDesde1' field on consumo page")
    form = date_input.find_parent("form")
    if form is None:
        raise RuntimeError("Could not find enclosing <form> for the consumption filter")
    return form, html


async def _submit_filter(request: APIRequestContext, date_from: date, date_to: date) -> None:
    form, page_html = await _fetch_filter_form(request)

    date_from_name = form.find(id="fechaDesde1").get("name")
    date_to_name = form.find(id="fechaHasta1").get("name")
    periodicidad_name = form.find(id="selectPeriodicidad").get("name")

    overrides = {
        date_from_name: date_from.isoformat(),
        date_to_name: date_to.isoformat(),
        periodicidad_name: "Horaria",
    }
    form_data = _serialize_form(form, overrides)

    raw_action = form.get("action", "")
    action_url = raw_action if raw_action.startswith("http") else BASE_URL + raw_action
    if "p_auth=" not in action_url:
        p_auth = _extract_p_auth(page_html, near=CONSUMO_PORTLET_ID)
        sep = "&" if "?" in action_url else "?"
        action_url = f"{action_url}{sep}p_auth={p_auth}"

    resp = await request.post(action_url, form=form_data)
    if _is_login_page(await resp.text()):
        raise SessionExpiredError("Session expired while submitting consumption filter")


async def _export_csv(request: APIRequestContext) -> str:
    export_url = (
        f"{BASE_URL}{CONSUMO_PAGE_PATH}"
        f"?p_p_id={CONSUMO_PORTLET_ID}"
        f"&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
        f"&p_p_resource_id={CONSUMO_EXPORT_RESOURCE_ID.replace('/', '%2F')}"
        f"&p_p_cacheability=cacheLevelPage"
        f"&_{CONSUMO_PORTLET_ID}_fileFormat=CSV"
    )
    resp = await request.get(export_url)
    text = await resp.text()
    if _is_login_page(text):
        raise SessionExpiredError("Session expired while exporting CSV")
    return text


def _parse_csv(csv_text: str) -> tuple[list[dict], set[str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    readings: list[dict] = []
    periods: set[str] = set()

    for row in reader:
        periodo = (row.get("Periodo") or "").strip()
        if periodo:
            periods.add(periodo)

        fecha_hora = (row.get("Fecha/Hora") or "").strip()
        consumo_raw = (row.get("Consumo (litros)") or "").strip()
        if not fecha_hora or not consumo_raw:
            continue
        try:
            timestamp = datetime.strptime(fecha_hora, "%d/%m/%Y %H")
            liters = float(consumo_raw)
        except ValueError:
            # Deliberately logs only the two fields that failed to parse, never
            # the whole row - the CSV also carries the contract number, the
            # meter serial and the supply address, and add-on logs regularly
            # get pasted into bug reports.
            _LOGGER.warning(
                "Skipping unparseable CSV row (fecha=%r, consumo=%r)", fecha_hora, consumo_raw
            )
            continue

        readings.append({"timestamp": timestamp.isoformat(), "liters": liters})

    readings.sort(key=lambda r: r["timestamp"])
    return readings, periods


def _validate_period(periods: set[str], date_from: date, date_to: date) -> bool:
    if not periods:
        return True
    expected = f"{date_from.isoformat()}/{date_to.isoformat()}"
    return periods == {expected}


async def fetch_range(
    page: Page,
    request: APIRequestContext,
    creds: dict,
    ha_client: HAClient,
    shared: SharedState,
    date_from: date,
    date_to: date,
) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            await _submit_filter(request, date_from, date_to)
            csv_text = await _export_csv(request)
        except SessionExpiredError:
            _LOGGER.info("Session expired mid-fetch, re-authenticating (attempt %d)", attempt)
            await ensure_logged_in(page, creds, ha_client, shared)
            continue

        readings, periods = _parse_csv(csv_text)
        if _validate_period(periods, date_from, date_to):
            return readings

        _LOGGER.warning(
            "CSV export returned unexpected period(s) %s for requested %s-%s, retrying (%d/%d)",
            periods,
            date_from,
            date_to,
            attempt,
            MAX_FETCH_RETRIES,
        )
        last_error = DataValidationError(
            f"CSV period {periods} did not match requested {date_from}-{date_to}"
        )

    raise last_error or DataValidationError("Failed to fetch a valid CSV export")
