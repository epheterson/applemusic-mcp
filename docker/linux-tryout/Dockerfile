# Linux "try it" box for mcp-applemusic — a real Debian + Google Chrome desktop you
# reach in your browser via noVNC, sign into Apple Music, and exercise the off-mac
# path (data via the API, playback via the Chrome web player).
#
# Intel/amd64 only: Google Chrome's Widevine CDM (required for full-length DRM
# playback) ships amd64 only. On Apple Silicon this image runs under emulation.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Virtual display + VNC/noVNC + a minimal WM + a terminal, and Chrome's runtime libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates procps nano x11-utils \
      xvfb x11vnc fluxbox xterm novnc websockify \
      fonts-liberation libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
      libdrm2 libgbm1 libgtk-3-0 libasound2 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libxkbcommon0 libpango-1.0-0 \
  && rm -rf /var/lib/apt/lists/*

# Google Chrome stable → Widevine → full-length playback (not 90s previews).
RUN wget -q -O /tmp/chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
  && apt-get update \
  && apt-get install -y --no-install-recommends /tmp/chrome.deb \
  && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

# Install the package. The pyproject marker pulls Playwright on Linux; we drive the
# SYSTEM Google Chrome (channel="chrome"), so no `playwright install chromium` needed.
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

# Chrome refuses to run as root; also just safer. Run everything as a normal user.
RUN useradd -m -s /bin/bash tester
USER tester
WORKDIR /home/tester

ENV DISPLAY=:99 \
    APPLEMUSIC_MCP_HOME=/home/tester/.applemusic-mcp \
    APPLEMUSIC_BROWSER_NO_SANDBOX=1

COPY --chown=tester:tester docker/linux-tryout/start.sh /home/tester/start.sh
COPY --chown=tester:tester docker/linux-tryout/welcome.txt /home/tester/welcome.txt
COPY --chown=tester:tester docker/linux-tryout/smoke.py /home/tester/smoke.py
RUN chmod +x /home/tester/start.sh

EXPOSE 6080
CMD ["/home/tester/start.sh"]
