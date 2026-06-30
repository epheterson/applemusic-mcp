#!/usr/bin/env bash
# Build and launch the Linux try-out container, then open noVNC in your browser.
#   ./docker/linux-tryout/run.sh
set -euo pipefail

cd "$(dirname "$0")/../.."   # repo root = docker build context
IMG=applemusic-mcp-linux-tryout

echo "Building $IMG (first build downloads Google Chrome; takes a few minutes)…"
docker build -f docker/linux-tryout/Dockerfile -t "$IMG" .

echo
echo "Starting. When it says 'noVNC ready', open:"
echo "    http://localhost:6080/vnc.html      (click Connect; no password)"
echo "Then follow the on-screen terminal: \`applemusic-mcp login\`."
echo "Sign-in persists in the docker volume 'applemusic-linux-data'."
echo
exec docker run --rm -it \
  -p 6080:6080 \
  --shm-size=1g \
  -v applemusic-linux-data:/home/tester/.applemusic-mcp \
  "$IMG"
