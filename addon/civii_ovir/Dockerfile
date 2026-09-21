ARG BUILD_FROM=debian:bookworm-slim

# --- Builder: build the venv here so pip, setuptools, wheel and the build
# --- caches never reach the runtime image.
FROM ${BUILD_FROM} AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
# No `playwright install` - we drive real Google Chrome (installed in the
# runtime stage), not Playwright's bundled Chromium, so no browser download is
# needed here.
RUN pip install --no-cache-dir -r /tmp/requirements.txt


# --- Runtime -------------------------------------------------------------
FROM ${BUILD_FROM}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# One apt layer: runtime deps + real Google Chrome. Chrome (channel="chrome",
# not Playwright's Chromium) passes the site's reCAPTCHA far more reliably -
# see the README. curl fetches the .deb directly, so no wget/gnupg keyring
# dance is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl jq python3 \
    && curl -fsSL -o /tmp/google-chrome.deb \
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
    && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
    && rm /tmp/google-chrome.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Chrome's .deb dependencies are enough to *launch* it, but not to render:
# without the GL/EGL stack `playwright install-deps` adds, page.screenshot()
# hangs for 30s and times out, which breaks the whole point of the Ingress
# panel (solving a visible reCAPTCHA by looking at the page). Learned the hard
# way - do not drop this to save image size.
RUN apt-get update \
    && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app /app/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
