FROM anasty17/mltb:latest

WORKDIR /app
RUN chmod 777 /app

# --- headful browser stack for the shortlink resolver -------------------
# The Turnstile widget on the shortlink family does not mount under headless
# Chromium: no widget means no token, and every resolve would fail. A real
# X server is therefore a hard dependency, not a nicety. xvfb-run gives the
# bot a virtual display at startup; chromium-fixes pulls the shared libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        x11-utils \
        fonts-liberation \
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libatspi2.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv mltbenv

COPY requirements.txt .
RUN mltbenv/bin/pip install --no-cache-dir -r requirements.txt

# Chromium goes in after requirements so a resolver-only change does not
# invalidate the ~150 MB browser download.
RUN mltbenv/bin/python -m patchright install --with-deps chromium \
    && chmod -R 777 /root/.cache/ms-playwright 2>/dev/null || true

COPY . .

RUN sed -i 's/\r$//' *.sh

# Expose the virtual display to every process the bot spawns, including the
# ones aria2 and JDownloader shell out to.
ENV DISPLAY=:99
ENV SHORTLINK_BROWSER=1
# See docker-compose.yml: TZ=UTC makes Cloudflare withhold the Turnstile
# challenge entirely, so the resolver stalls on the front door.
ENV TZ=Europe/London

CMD ["bash", "start.sh"]
