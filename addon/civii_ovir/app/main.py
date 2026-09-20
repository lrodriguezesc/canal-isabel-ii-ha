"""civii_ovir add-on entrypoint.

Two concurrent asyncio tasks sharing one Playwright browser context:
  - fetch_loop: logs in, fetches hourly consumption on a schedule, pushes
    sensor states + external long-term statistics into Home Assistant.
  - the Ingress panel (app/panel.py): lets a human solve a visible reCAPTCHA
    challenge remotely when browser_login.py's automated login can't.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from .browser_login import ensure_logged_in
from .civii_scraper import fetch_range
from .ha_client import HAClient
from .panel import build_app
from .state import SharedState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("civii_ovir")

DATA_DIR = Path("/data")
PROFILE_DIR = DATA_DIR / "browser_profile"
STATE_FILE = DATA_DIR / "app_state.json"

MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
STAT_ID = "civii_ovir:water_consumption"
MAX_EXPORT_RANGE_DAYS = 30
INITIAL_BACKFILL_DAYS = 365
# When resuming after a gap (missed cycles, e.g. an unsolved reCAPTCHA), fetch
# back a bit further than the last known-good hour rather than starting right
# at it, to be safe against boundary/off-by-one effects.
GAP_OVERLAP_DAYS = 2

SENSOR_CUMULATIVE = "sensor.civii_ovir_cumulative_consumption"
SENSOR_LAST_HOUR = "sensor.civii_ovir_last_hour_consumption"
SENSOR_LAST_READING = "sensor.civii_ovir_last_reading_time"
SENSOR_LAST_ANOMALY = "sensor.civii_ovir_last_anomaly"

# An hour with consumption below this is considered "quiet" for the
# telemetry-artifact heuristic below (real spikes investigated at the start
# of this project were 1000-1700 L/h with near-zero neighbours either side,
# which is the meter catching up after a coverage gap rather than a genuine
# sustained flow).
QUIET_HOUR_LITERS = 5.0


def _load_credentials() -> dict:
    creds = {
        "username": os.environ["CIVII_USERNAME"],
        "password": os.environ["CIVII_PASSWORD"],
        "document_type": os.environ.get("CIVII_DOCUMENT_TYPE", "NIF"),
    }
    if not creds["username"] or not creds["password"]:
        raise SystemExit("username/password not configured - fill in the add-on's Configuration tab")
    return creds


def _load_app_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("last_anomaly_hour", None)
        return state
    return {
        "cumulative_liters": 0.0,
        "last_processed_hour": None,
        "stats_backfilled": False,
        "last_anomaly_hour": None,
    }


def _save_app_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _to_madrid(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts).replace(tzinfo=MADRID_TZ)


def _detect_latest_anomaly(readings: list[dict], threshold_liters: float) -> dict | None:
    """Find the most recent hour in this (sorted, ascending) batch exceeding
    threshold_liters. Flags it as a likely telemetry artifact - the meter
    catching up after a coverage gap rather than a genuine sustained flow -
    when both neighbouring hours are near-zero, mirroring the pattern found
    by hand at the start of this project (1000-1700 L/h spikes bracketed by
    ~0 L hours)."""
    candidate = None
    for i, r in enumerate(readings):
        if r["liters"] < threshold_liters:
            continue
        prev_liters = readings[i - 1]["liters"] if i > 0 else None
        next_liters = readings[i + 1]["liters"] if i < len(readings) - 1 else None
        likely_artifact = (
            prev_liters is not None
            and next_liters is not None
            and prev_liters < QUIET_HOUR_LITERS
            and next_liters < QUIET_HOUR_LITERS
        )
        candidate = {
            "timestamp": r["timestamp"],
            "liters": r["liters"],
            "likely_telemetry_artifact": likely_artifact,
        }
    return candidate


async def _push_anomaly(ha_client: HAClient, app_state: dict, anomaly: dict, threshold_liters: float) -> None:
    last_anomaly = app_state.get("last_anomaly_hour")
    if last_anomaly and anomaly["timestamp"] <= last_anomaly:
        return  # already reported (or an older one resurfacing from an overlapping window)

    await ha_client.set_state(
        SENSOR_LAST_ANOMALY,
        _to_madrid(anomaly["timestamp"]).astimezone(timezone.utc).isoformat(),
        {
            "device_class": "timestamp",
            "friendly_name": "Ultima anomalia (Canal de Isabel II)",
            "liters": anomaly["liters"],
            "threshold_liters": threshold_liters,
            "likely_telemetry_artifact": anomaly["likely_telemetry_artifact"],
        },
    )
    app_state["last_anomaly_hour"] = anomaly["timestamp"]
    _save_app_state(app_state)
    _LOGGER.warning(
        "Anomaly detected: %s L at %s (likely_telemetry_artifact=%s)",
        anomaly["liters"],
        anomaly["timestamp"],
        anomaly["likely_telemetry_artifact"],
    )


async def _push_readings(
    ha_client: HAClient, app_state: dict, readings: list[dict], anomaly_threshold_liters: float
) -> None:
    """Reconstruct a monotonic running-sum series for this batch (readings may
    overlap already-processed hours, since each cycle re-fetches a few days
    back to catch retroactive corrections), import it as external statistics,
    push the live sensor states, and advance the persisted watermark."""
    if not readings:
        return

    readings = sorted(readings, key=lambda r: r["timestamp"])
    last_processed = app_state.get("last_processed_hour")

    anomaly = _detect_latest_anomaly(readings, anomaly_threshold_liters)
    if anomaly:
        await _push_anomaly(ha_client, app_state, anomaly, anomaly_threshold_liters)

    overlap_liters = sum(
        r["liters"] for r in readings if last_processed and r["timestamp"] <= last_processed
    )
    running = app_state["cumulative_liters"] - overlap_liters

    stats = []
    for r in readings:
        running += r["liters"]
        stats.append(
            {
                "start": _to_madrid(r["timestamp"]).isoformat(),
                "sum": running,
                "state": running,
            }
        )

    await ha_client.import_statistics(
        metadata={
            "statistic_id": STAT_ID,
            "source": "civii_ovir",
            "name": "Canal de Isabel II - Consumo de agua",
            "unit_of_measurement": "L",
            "has_sum": True,
            "has_mean": False,
        },
        stats=stats,
    )

    newest = readings[-1]
    if not last_processed or newest["timestamp"] > last_processed:
        app_state["cumulative_liters"] = running
        app_state["last_processed_hour"] = newest["timestamp"]
        _save_app_state(app_state)

    await ha_client.set_state(
        SENSOR_LAST_HOUR,
        newest["liters"],
        {
            "unit_of_measurement": "L",
            "state_class": "measurement",
            "friendly_name": "Consumo ultima hora (Canal de Isabel II)",
        },
    )
    await ha_client.set_state(
        SENSOR_CUMULATIVE,
        round(app_state["cumulative_liters"] / 1000, 3),
        {
            "unit_of_measurement": "m³",
            "device_class": "water",
            "state_class": "total_increasing",
            "friendly_name": "Consumo acumulado (Canal de Isabel II)",
        },
    )
    await ha_client.set_state(
        SENSOR_LAST_READING,
        _to_madrid(newest["timestamp"]).astimezone(timezone.utc).isoformat(),
        {"device_class": "timestamp", "friendly_name": "Ultima lectura (Canal de Isabel II)"},
    )


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into <=max_days chunks (the portal's export limit),
    oldest first - so a monotonic running sum can be built up chunk by chunk."""
    chunks = []
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(chunk_end - timedelta(days=max_days - 1), start)
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - timedelta(days=1)
    return list(reversed(chunks))


async def _fetch_and_push_range(
    page, request, creds, ha_client, shared, app_state, date_from: date, date_to: date,
    anomaly_threshold_liters: float,
) -> int:
    """Fetch+push [date_from, date_to], chunked into <=30-day pieces if needed
    (the portal's per-request export limit). Returns total readings pushed."""
    total = 0
    for chunk_start, chunk_end in _date_chunks(date_from, date_to, MAX_EXPORT_RANGE_DAYS):
        try:
            readings = await fetch_range(
                page, request, creds, ha_client, shared, chunk_start, chunk_end
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Fetch chunk %s-%s failed: %s", chunk_start, chunk_end, err)
            continue
        await _push_readings(ha_client, app_state, readings, anomaly_threshold_liters)
        total += len(readings)
    return total


async def _backfill_history(page, request, creds, ha_client, shared, app_state, anomaly_threshold_liters) -> None:
    if app_state.get("stats_backfilled"):
        return

    _LOGGER.info("Starting one-time %d-day history backfill", INITIAL_BACKFILL_DAYS)
    shared.status_message = "Backfilling history..."

    today = date.today()
    oldest = today - timedelta(days=INITIAL_BACKFILL_DAYS)
    await _fetch_and_push_range(
        page, request, creds, ha_client, shared, app_state, oldest, today, anomaly_threshold_liters
    )

    app_state["stats_backfilled"] = True
    _save_app_state(app_state)
    _LOGGER.info("Backfill complete")


def _compute_fetch_start(app_state: dict, history_days: int, date_to: date) -> date:
    """Normally just the last history_days window. But if the persisted
    watermark is older than that (missed cycles - an unsolved reCAPTCHA, the
    add-on being down, ...), extend the start back to the watermark (plus a
    bit of overlap) so the gap gets backfilled too instead of leaving a hole,
    capped at INITIAL_BACKFILL_DAYS so a corrupted/ancient watermark can't
    trigger an unbounded fetch."""
    normal_from = date_to - timedelta(days=history_days - 1)

    last_processed = app_state.get("last_processed_hour")
    if not last_processed:
        return normal_from

    last_date = datetime.fromisoformat(last_processed).date()
    gap_from = last_date - timedelta(days=GAP_OVERLAP_DAYS)
    floor = date_to - timedelta(days=INITIAL_BACKFILL_DAYS)

    return max(min(normal_from, gap_from), floor)


async def _launch_browser(playwright):
    """Launch a fresh Chrome against the persistent on-disk profile. Called
    once per fetch cycle (not kept open between cycles) so the add-on's RAM
    drops back to baseline the rest of the time - the profile on disk (not
    the running process) is what carries reCAPTCHA trust across launches."""
    return await playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel="chrome",
        headless=True,
        locale="es-ES",
        viewport={"width": 1400, "height": 1000},
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )


async def fetch_loop(
    playwright, ha_client: HAClient, shared: SharedState, creds: dict, scan_interval_min: int,
    history_days: int, anomaly_threshold_liters: float,
) -> None:
    app_state = _load_app_state()

    while True:
        context = None
        try:
            context = await _launch_browser(playwright)
            page = context.pages[0] if context.pages else await context.new_page()
            shared.page = page

            if not app_state.get("stats_backfilled"):
                await ha_client.set_status("backfilling", detail=f"Importing up to {INITIAL_BACKFILL_DAYS} days of history")
                await _backfill_history(
                    page, context.request, creds, ha_client, shared, app_state, anomaly_threshold_liters
                )

            shared.status_message = "Fetching consumption..."
            await ha_client.set_status("fetching", detail="Logging in")
            await ensure_logged_in(page, creds, ha_client, shared)

            date_to = date.today()
            date_from = _compute_fetch_start(app_state, history_days, date_to)
            normal_from = date_to - timedelta(days=history_days - 1)
            if date_from < normal_from:
                _LOGGER.warning(
                    "Gap detected (last processed hour %s) - catching up from %s instead of the usual %d-day window",
                    app_state.get("last_processed_hour"),
                    date_from,
                    history_days,
                )
                shared.status_message = f"Catching up missed history since {date_from}..."
                await ha_client.set_status("fetching", detail=f"Catching up missed history since {date_from}")
            else:
                await ha_client.set_status("fetching", detail=f"Fetching {date_from} to {date_to}")

            count = await _fetch_and_push_range(
                page, context.request, creds, ha_client, shared, app_state, date_from, date_to,
                anomaly_threshold_liters,
            )

            shared.last_fetch_at = datetime.now(timezone.utc).isoformat()
            shared.last_readings_count = count
            shared.last_error = None
            _LOGGER.info("Fetch cycle OK, %d readings", count)
        except Exception as err:  # noqa: BLE001
            shared.last_error = str(err)
            _LOGGER.exception("Fetch cycle failed")
            await ha_client.set_status("error", detail=str(err))
        finally:
            shared.page = None
            if context is not None:
                await context.close()

        next_at = datetime.now(timezone.utc) + timedelta(minutes=scan_interval_min)
        shared.status_message = (
            f"Idle - next check ~{next_at.astimezone(MADRID_TZ).strftime('%H:%M')}"
            + (f" (last: {shared.last_error})" if shared.last_error else "")
        )
        if not shared.last_error:
            await ha_client.set_status(
                "idle", detail=f"Next check ~{next_at.astimezone(MADRID_TZ).strftime('%H:%M')}"
            )

        shared.retry_requested.clear()
        try:
            await asyncio.wait_for(shared.retry_requested.wait(), timeout=scan_interval_min * 60)
            _LOGGER.info("Retry requested from panel, running now")
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    creds = _load_credentials()
    scan_interval_min = int(os.environ.get("CIVII_SCAN_INTERVAL_MINUTES", "30"))
    history_days = int(os.environ.get("CIVII_HISTORY_WINDOW_DAYS", "3"))
    ingress_port = int(os.environ.get("CIVII_INGRESS_PORT", "8099"))
    anomaly_threshold_liters = float(os.environ.get("CIVII_ANOMALY_THRESHOLD_LITERS", "500"))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    shared = SharedState()

    async with aiohttp.ClientSession() as session:
        ha_client = HAClient(session)

        async with async_playwright() as p:
            web_app = build_app(shared)
            runner = web.AppRunner(web_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", ingress_port)
            await site.start()
            _LOGGER.info("Ingress panel listening on :%d", ingress_port)

            try:
                await fetch_loop(
                    p, ha_client, shared, creds, scan_interval_min, history_days, anomaly_threshold_liters
                )
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
