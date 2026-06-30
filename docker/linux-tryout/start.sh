#!/usr/bin/env bash
# Boot a minimal Linux desktop (Xvfb + fluxbox), serve it over noVNC, and open a
# terminal with instructions. Reach it at http://<host>:6080/vnc.html
set -e
export DISPLAY=:99
mkdir -p "$APPLEMUSIC_MCP_HOME"

# Virtual framebuffer.
Xvfb :99 -screen 0 1440x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.3
done

fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1

# VNC server on the virtual display, then noVNC (web UI) bridging to it.
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -bg -o /tmp/x11vnc.log
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

# A terminal with the welcome text, then an interactive shell.
xterm -fa Monospace -fs 11 -geometry 132x40+8+8 \
  -title "Apple Music MCP — Linux try-out" \
  -e bash -c 'cat ~/welcome.txt; exec bash' &

echo "noVNC ready → http://localhost:6080/vnc.html  (no VNC password)"
echo "Container is up. Ctrl-C here to stop it."
tail -f /dev/null
