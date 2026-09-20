"""Ingress web panel: a live screenshot of the current Playwright page plus a
click-forwarding endpoint, so a visible reCAPTCHA challenge can be solved from
a phone/laptop through Home Assistant's UI instead of a physical screen.

Served by aiohttp on CIVII_INGRESS_PORT, matching config.yaml's ingress_port.
"""

from __future__ import annotations

from aiohttp import web

from .state import SharedState

_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canal Isabel II</title>
<style>
  body { margin: 0; background: #111; color: #eee; font-family: sans-serif; }
  #status { padding: 8px 12px; font-size: 14px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  #retry { background: #2a6; color: #fff; border: none; border-radius: 4px; padding: 6px 12px; font-size: 13px; }
  #retry:disabled { background: #555; }
  img { display: block; width: 100%; cursor: crosshair; }
  #idleNotice { padding: 40px 16px; text-align: center; color: #999; font-size: 14px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<div id="status">
  <span id="statusText">Cargando...</span>
  <button id="retry">Reintentar ahora</button>
</div>
<div id="idleNotice">El navegador solo se abre durante cada ciclo (para ahorrar memoria en reposo). Nada que mostrar ahora mismo.</div>
<img id="shot" src="screenshot.jpg" hidden>
<script>
const img = document.getElementById('shot');
const idleNotice = document.getElementById('idleNotice');
const statusText = document.getElementById('statusText');
const retryBtn = document.getElementById('retry');

function refreshShot() {
  img.src = 'screenshot.jpg?t=' + Date.now();
}

let shotTimer = null;
function setBrowserActive(active) {
  img.hidden = !active;
  idleNotice.hidden = active;
  if (active && !shotTimer) {
    refreshShot();
    shotTimer = setInterval(refreshShot, 800);
  } else if (!active && shotTimer) {
    clearInterval(shotTimer);
    shotTimer = null;
  }
}

async function refreshStatus() {
  try {
    const r = await fetch('status');
    const d = await r.json();
    setBrowserActive(d.browser_active);
    statusText.textContent = d.challenge_active
      ? 'reCAPTCHA activo - toca la imagen para resolverlo'
      : (d.status_message || '');
  } catch (e) {}
}
setInterval(refreshStatus, 2000);
refreshStatus();

retryBtn.addEventListener('click', async () => {
  retryBtn.disabled = true;
  retryBtn.textContent = 'Reintentando...';
  await fetch('retry', {method: 'POST'});
  setTimeout(() => { retryBtn.disabled = false; retryBtn.textContent = 'Reintentar ahora'; }, 5000);
});

img.addEventListener('click', async (ev) => {
  const rect = img.getBoundingClientRect();
  const xRatio = (ev.clientX - rect.left) / rect.width;
  const yRatio = (ev.clientY - rect.top) / rect.height;
  await fetch('click', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({xRatio, yRatio}),
  });
  setTimeout(refreshShot, 300);
});
</script>
</body>
</html>
"""

_PLACEHOLDER_JPEG_TEXT = b""  # aiohttp will just send an empty body if no page yet


def build_app(shared: SharedState) -> web.Application:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_PAGE_HTML, content_type="text/html")

    async def screenshot(_request: web.Request) -> web.Response:
        if shared.page is None:
            return web.Response(status=503, text="Browser not ready yet")
        try:
            data = await shared.page.screenshot(type="jpeg", quality=60)
        except Exception as err:  # noqa: BLE001
            return web.Response(status=503, text=f"Screenshot failed: {err}")
        return web.Response(body=data, content_type="image/jpeg")

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "browser_active": shared.page is not None,
                "challenge_active": shared.challenge_active,
                "status_message": shared.status_message,
                "last_fetch_at": shared.last_fetch_at,
                "last_error": shared.last_error,
                "last_readings_count": shared.last_readings_count,
            }
        )

    async def click(request: web.Request) -> web.Response:
        if shared.page is None:
            return web.Response(status=503, text="Browser not ready yet")
        body = await request.json()
        viewport = shared.page.viewport_size or {"width": 1400, "height": 1000}
        x = float(body["xRatio"]) * viewport["width"]
        y = float(body["yRatio"]) * viewport["height"]
        await shared.page.mouse.click(x, y)
        return web.json_response({"ok": True})

    async def retry(_request: web.Request) -> web.Response:
        shared.retry_requested.set()
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/screenshot.jpg", screenshot)
    app.router.add_get("/status", status)
    app.router.add_post("/click", click)
    app.router.add_post("/retry", retry)
    return app
