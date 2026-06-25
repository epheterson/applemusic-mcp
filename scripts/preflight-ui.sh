#!/usr/bin/env bash
#
# Pre-release gate for the native Music.app UI-automation paths (catalog
# deep-link playback via CoreGraphics, UI catalog search, transport controls).
#
# These paths are version-fragile and CANNOT be validated by GitHub CI (no
# Music.app, no display, no Accessibility). Run this on EVERY Mac/Music version
# we support BEFORE any release that touches UI-automation logic:
#   * the iMac  (macOS 15 / Music 1.5)
#   * the mini  (macOS 26 / Music 26)
#
# Requirements on the machine you run it on:
#   * signed into Apple Music with an active subscription
#   * an UNLOCKED, active console session (use Screen Sharing's GUI login, not
#     SSH — synthetic mouse clicks need a real WindowServer session)
#   * Accessibility granted to the terminal / host running this
#
# Usage:   ./scripts/preflight-ui.sh
# Green => the native UI playback paths actually work on THIS Mac/Music version.
#
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

echo "──────────────────────────────────────────────────────────"
echo " applemusic-mcp · native UI-automation gate"
echo "──────────────────────────────────────────────────────────"
echo " Music plays MUTED (volume forced to 0) and is paused after each test."
echo

set +e
out="$(TEST_UI=1 "$PY" -m pytest -o addopts="" -m ui_live tests/test_live_ui.py -v -rs 2>&1)"
rc=$?
set -e
echo "$out"

if [ "$rc" -ne 0 ]; then
  echo
  echo "❌ UI gate FAILED — do NOT release a UI-touching change. See failures above."
  exit 1
fi

# False-green guard: a run where every test SKIPPED (locked screen, no console
# session, not signed in) must NOT read as validated — that's the trap this gate
# exists to avoid. Require that the core playback paths actually PASSED.
for t in test_native_play_album_clicks_play_button \
         test_native_play_specific_track_double_clicks_row \
         test_native_play_library_track; do
  if ! echo "$out" | grep -q "$t PASSED"; then
    echo
    echo "❌ $t did not PASS (skipped or failed) — UI NOT validated on this machine."
    echo "   Run from an UNLOCKED console session (Screen Sharing GUI login, not SSH),"
    echo "   signed into Apple Music, with Accessibility granted."
    exit 1
  fi
done

echo
echo "✅ Native UI-automation paths validated on this Mac/Music version."
echo "   Remember to run this on BOTH the iMac and the mini before release."
