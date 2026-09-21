# Canal de Isabel II - Home Assistant Add-on

Home Assistant Add-on that brings **hourly water consumption** from Canal de Isabel II
(Oficina Virtual, Madrid) directly into Home Assistant. It authenticates with your NIF/NIE and
password by reproducing, inside a real Chrome browser, the calls the portal's own website makes
to download hourly telelectura data — no manual cookie extraction, no external machine needed.

## Motivation

This started from investigating a recurring pattern of nighttime consumption spikes (1-2 AM)
that were only visible in the portal's hourly detail view. Instead of manually downloading CSVs
every time, this add-on brings the data straight into Home Assistant, running inside HA itself
(Home Assistant OS / Supervisor) — no Mac, PC, or external server required.

## Why an Add-on and not a regular integration

The portal's login is protected by Google reCAPTCHA Enterprise (invisible mode), which a
regular HTTP client can't get past. On top of that, a real browser's session cookies **can't be
reused by a separate HTTP client** — the site's anti-bot layer (F5) binds them to the network
fingerprint of the browser that created them. So the whole flow (login, filtering, CSV export)
runs inside a real Chrome session (via Playwright), which needs its own Docker container rather
than living inside the Home Assistant Core process.

## Requirements

- Home Assistant **OS** or **Supervised** (you need Supervisor to install Add-ons; this won't
  work on a bare Core/Container install).
- `amd64` architecture (x86_64 host). If your HA runs on `aarch64` (e.g. Raspberry Pi), you'd
  need to add that architecture to `config.yaml` and verify Google Chrome has a build for it
  (not guaranteed - Chrome doesn't publish official ARM Linux packages).
- Filesystem access to the host (SSH, Samba, or the "File editor" add-on) to copy the add-on's
  files into `/addons/local/`.

## Installation

### Option A: as an add-on repository (recommended if the repo is reachable from HA)

1. **Settings → Add-ons → Add-on Store** → (⋮) menu → **Repositories**.
2. Paste this repository's URL (`https://github.com/lrodriguezesc/canal-isabel-ii-ha`) and add
   it.
3. **"Canal de Isabel II (consumo de agua)"** should show up in the store. Install it from there
   and skip to step 4 in the next section.

### Option B: local add-on (manual file copy)

1. Copy the contents of `addon/civii_ovir/` (from this repo) to `/addons/local/civii_ovir/` on
   your Home Assistant OS host. Over SSH (with the "Terminal & SSH" add-on installed, which
   typically mounts that directory as `/addons/local`):
   ```bash
   scp -r addon/civii_ovir/ root@<your-ha>:/addons/local/civii_ovir
   ```
   or use the "Samba" / "File editor" add-on if you'd rather drag the files over.
2. Go to **Settings → Add-ons → Add-on Store**, (⋮) menu → **"Check for updates"**. Supervisor
   scans `/addons/local/` and **"Canal de Isabel II (consumo de agua)"** should appear under a
   "Local add-ons" section.
3. Open it and click **Install** (the first time builds the Docker image, which takes a few
   minutes: it installs real Google Chrome + Playwright).
4. Go to the add-on's **Configuration** tab and fill in:
   - `username`: your NIF or NIE (the same one you use to log into the Oficina Virtual).
   - `password`: your Oficina Virtual password.
   - `document_type`: `NIF` or `NIE`.
   - `scan_interval_minutes` (optional, default 30 - see "Choosing a refresh interval").
   - `history_window_days` (optional, default 3).
   - `anomaly_threshold_liters` (optional, default 500).
5. Save and **start** the add-on (Info tab → Start). Also enable "Start on boot" and "Watchdog"
   if you want it to recover on its own after an HA restart or a crash.
6. The first run does a one-time ~365-day history backfill (takes a few seconds to minutes),
   then settles into its normal cycle.

### If a visible reCAPTCHA shows up

Login usually goes through cleanly on its own (it uses a persistent Chrome profile that builds
trust over time), but every so often Google may ask for a visible challenge ("select all
squares with motorcycles", etc). When that happens:

1. You get a **persistent Home Assistant notification** about it.
2. Open the add-on's panel: its sidebar icon (if pinned), or **Settings → Add-ons → Canal de
   Isabel II (consumo de agua) → open web UI** (the icon on the "Info" tab, top right).
3. You'll see a live screenshot of the browser. Click the tiles just like you would on the site
   itself, then "Verify"/"Skip" as needed.
4. You have 10 minutes from the notification. If you miss the window, that cycle fails and
   retries on the next scheduled cycle — or hit the panel's **"Retry now"** button to force it
   immediately instead of waiting.

## Adding the sensor to the Energy dashboard

⚠️ **Important**: use the **external statistic**, not the live sensor, or the Energy dashboard
will show a massive one-day spike (see "Troubleshooting" below).

1. **Settings → Energy dashboard** → "Water" section → **Add water source**.
2. In the statistic picker, search for and choose **`civii_ovir:water_consumption`** (not
   `sensor.civii_ovir_cumulative_consumption` — both may show up in the list; you want the one
   that does *not* look like an entity_id).
3. Give it a name, e.g. "Canal de Isabel II", and save.

The up-to-365-day history already imported will show up retroactively in the Energy dashboard's
charts as soon as you add it.

## Entities it creates

| Entity | Class | Unit | Use |
|---|---|---|---|
| `sensor.civii_ovir_cumulative_consumption` | `water` (`total_increasing`) | m³ | Quick at-a-glance running total. **Don't use it as the Energy dashboard source** (see above). |
| `sensor.civii_ovir_last_hour_consumption` | — (`measurement`) | L | Spike-detection automations (e.g. alert if > 1000 L overnight). |
| `sensor.civii_ovir_last_reading_time` | `timestamp` | — | When the latest hourly meter reading actually happened (not when the fetch cycle ran). |
| `sensor.civii_ovir_status` | `enum` | — | Process state: `idle`, `backfilling`, `fetching`, `waiting_captcha`, `error`. Human-readable detail (next check time, error message, etc.) lives in the `detail` attribute. Meant to be used directly in automations. |
| `sensor.civii_ovir_last_anomaly` | `timestamp` | — | When the last anomalous consumption hour occurred (see below). Attributes: `liters`, `threshold_liters`, `likely_telemetry_artifact`. |
| `civii_ovir:water_consumption` | external statistic | L | **This is the one the Energy dashboard uses**, along with long-history charts. |

### Anomaly detection

Every time a batch of hourly readings is processed (a normal cycle, gap catch-up, or the initial
backfill), the most recent hour exceeding `anomaly_threshold_liters` (500 L by default - the
real spikes investigated at the start of this project were 1000-1700 L/h, well above the
household's normal hourly consumption) is flagged. If found, its two neighbouring hours are
checked: if both are "quiet" (< 5 L), it's tagged `likely_telemetry_artifact: true` - the meter
catching up after a coverage gap rather than genuine sustained flow, mirroring the exact pattern
identified by hand at the start of this project. The sensor only advances when the anomaly is
more recent than the one already stored (`last_anomaly_hour` in `app_state.json`), so it doesn't
flicker every time a cycle re-fetches the same hours.

### Example automation

A single automation using trigger IDs and `choose` covers all three cases - swap
`notify.mobile_app_your_phone` for your own mobile app's notify service
(Settings → People → your user → find it under its device's notify entity):

```yaml
automation:
  - alias: "Canal de Isabel II - alerts"
    description: "Notifies on reCAPTCHA needing solving, a persistent error, and a genuine consumption anomaly"
    triggers:
      - trigger: state
        entity_id: sensor.civii_ovir_status
        to: "waiting_captcha"
        id: captcha
      - trigger: state
        entity_id: sensor.civii_ovir_status
        to: "error"
        for:
          hours: 2
        id: error
      - trigger: state
        entity_id: sensor.civii_ovir_last_anomaly
        id: anomaly
    conditions: []
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: captcha
            sequence:
              - action: notify.mobile_app_your_phone
                data:
                  title: "Canal de Isabel II"
                  message: "A reCAPTCHA needs solving - open the add-on panel."
          - conditions:
              - condition: trigger
                id: error
            sequence:
              - action: notify.mobile_app_your_phone
                data:
                  title: "Canal de Isabel II"
                  message: "The water add-on has been failing for 2+ hours: {{ state_attr('sensor.civii_ovir_status', 'detail') }}"
          - conditions:
              - condition: trigger
                id: anomaly
              # Native attribute check instead of a template condition
              - condition: state
                entity_id: sensor.civii_ovir_last_anomaly
                attribute: likely_telemetry_artifact
                state: false
            sequence:
              - action: notify.mobile_app_your_phone
                data:
                  title: "Water consumption anomaly"
                  message: >-
                    {{ state_attr('sensor.civii_ovir_last_anomaly', 'liters') }} L at
                    {{ as_timestamp(states('sensor.civii_ovir_last_anomaly')) | timestamp_custom('%H:%M') }}
    mode: queued
```

## Add-on options

| Option | Range | Default | Description |
|---|---|---|---|
| `username` | — | — | Oficina Virtual NIF/NIE. |
| `password` | — | — | Oficina Virtual password. |
| `document_type` | `NIF`\|`NIE` | `NIF` | Document type. |
| `scan_interval_minutes` | 10-1440 | 30 | How often it refreshes, in minutes (10 minutes to 24 hours). See the note below. |
| `history_window_days` | 1-30 | 3 | How many days back each normal cycle re-checks, to catch late corrections from the meter itself. |
| `anomaly_threshold_liters` | 50-5000 | 500 | Liters/hour above which an hour is considered anomalous (see "Anomaly detection"). |

### Choosing a refresh interval

The meter itself only publishes hourly data, so refreshing more often than once an hour buys you
no extra resolution - but the interval does decide how often the add-on has to log in, and every
login is a chance for a reCAPTCHA to appear. The portal's session lasts somewhere between half an
hour and an hour, and the add-on reuses it between cycles (see "Session reuse" below), which
makes the trade-off non-obvious:

- **30 minutes or less** - each cycle refreshes the session before it expires, so after the
  first login the add-on essentially stops logging in. Fewest reCAPTCHAs, at the cost of a Chrome
  launch every half hour.
- **Several hours, or once a day** - the session is always dead by the next cycle, so every cycle
  logs in; but there are only a handful of cycles a day, so few logins in absolute terms. Fine if
  you just want a daily update.
- **45-90 minutes is the worst of both** - frequent cycles *and* an expired session each time,
  meaning a login almost every cycle.

Data is never lost by choosing a long interval: each cycle re-fetches `history_window_days` of
history, and anything older is picked up by gap recovery below.

### Automatic gap recovery

If a cycle fails (e.g. an unsolved reCAPTCHA) or the add-on was down for several days, the next
successful cycle is **not limited** to `history_window_days`: it compares the last processed
hour's date against today and, if the gap is bigger than the normal window, automatically
extends the requested range (with a couple of days of overlap) to cover everything missing,
split into 30-day chunks if needed (the portal's own limit). No manual intervention needed
beyond solving a reCAPTCHA if it's still blocked.

## Verification / Troubleshooting

- **View logs**: Settings → Add-ons → Canal de Isabel II (consumo de agua) → "Log" tab.
- **Live status**: the add-on's panel (Ingress) shows the last result, whether a reCAPTCHA is
  active, and the manual retry button.
- **Check imported history**: Developer Tools → Statistics → search for
  `civii_ovir:water_consumption`.
- **Sensors don't show up**: check the logs; most likely a login failure (wrong username/
  password in Configuration) or a previous cycle still waiting on a reCAPTCHA.
- **"Today's" consumption in the Energy dashboard looks absurd** (e.g. hundreds of m³): the
  water source is pointing at the live sensor instead of the external statistic - fix it by
  following "Adding the sensor to the Energy dashboard" above.

## Updating

**If you installed it as a repository (Option A):** nothing to download by hand. Supervisor
pulls the repository automatically (`git fetch` + `reset --hard`) every 3 hours, or immediately
when you hit Settings → Add-ons → Add-on Store → (⋮) → **"Check for updates"**. Whenever this
repo's `version:` is bumped, the add-on page shows an **Update** button - one click and you're
on the new version.

**If you installed it as a local add-on (Option B):** re-copy `addon/civii_ovir/` to
`/addons/local/civii_ovir/`, then "Check for updates" → **Update**.

> **Note for developers:** `version:` in `config.yaml` is the *only* update trigger. If you
> change code without bumping it, Supervisor sees nothing new - use the **Rebuild** button on
> the add-on's Info tab instead. One exception: the AppArmor profile is only (re)installed on
> the install/update path, never on a rebuild, so changes to `apparmor.txt` do require a version
> bump.

The persistent Chrome profile and internal state (`app_state.json`, with the running total and
the last-processed-hour watermark) live in the add-on's own `/data` volume, so they survive
restarts, rebuilds and updates - no need to redo the backfill.

## Security

The add-on reports a Supervisor security rating of **8/8**. It requests no privileged
capabilities, no host network or PID namespace, no Docker API access, and keeps the default
Supervisor role; the web UI is served through Ingress rather than an exposed port; and it ships
its own AppArmor profile (`apparmor.txt`), which denies mounting, kernel/security filesystem
access and other host-level operations it never performs.

Your credentials are stored by Supervisor as add-on options (never in this repo or the Docker
image), are read at runtime from `/data/options.json`, and are never written to the logs.

`/data` also holds the Chrome profile (`browser_profile/`) and the saved portal session
(`session_cookies.json`, mode `600`). Both contain live authentication material, so treat an
add-on backup the same way you would treat the password itself. Cookie values are never logged -
only how many were saved or restored, and for which domains.

## Technical notes

Canal de Isabel II's portal is a Liferay Portal, not a public API. The scraping reproduces the
real flow: form login with a per-page-load CSRF token (`p_auth`), the date filter as a portlet
action, and the hourly telelectura CSV download as a resource of that same portlet - all of it
through Playwright's `context.request` (the browser's own session), never a separate `requests`
client. The session expires after roughly half an hour to an hour of inactivity; it
re-authenticates on its own when it detects that.

**Memory usage**: Chrome is only launched during the active cycle (login + download, normally a
few seconds) and closed immediately after - it isn't kept running in the background between
cycles. The persistent profile (`/data/browser_profile`, managed by Supervisor) carries Google's
long-lived reCAPTCHA trust cookies across those restarts. The Ingress panel reflects the same
lifecycle: outside an active cycle or a pending reCAPTCHA, there's nothing to capture and it
says so instead of showing a broken image.

**Session reuse**: the portal login itself does *not* survive in that profile. `JSESSIONID` and
the portal's F5 anti-bot cookies are session cookies (no expiry), which Chrome keeps in memory
only and discards when it closes - so on its own, closing the browser each cycle would mean
logging in from scratch every single cycle, and rolling the reCAPTCHA dice every single time.
The add-on therefore saves the cookie jar to `/data/session_cookies.json` before closing Chrome
and restores it on the next launch. When the portal session is still alive the whole login step
is skipped (`Already logged in (persistent session still valid)` in the logs); when it has
expired, the normal login runs as before.

## Usage notice

This add-on automates logging into your *own* Oficina Virtual account, with your own
credentials, to bring your own consumption data into your own Home Assistant instance. It is
not a general-purpose tool for bypassing reCAPTCHA or any other third-party anti-bot mechanism,
and it isn't meant to be used against accounts or services that aren't yours. There is no
documented public API for Canal de Isabel II; this project is for personal use, provided as-is
(see `LICENSE`), and may stop working if the portal changes its interface.
