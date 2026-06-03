#!/usr/bin/env bash
#
# Pre-release gate. Run this on a Mac signed into Apple Music BEFORE bumping the
# version, because the tokenless UI-automation paths can ONLY be validated on a
# real, signed-in, unlocked Mac — GitHub CI (Ubuntu, no Music.app, no sign-in)
# cannot. This is the gate that catches #28-class regressions (e.g. the one-word
# AppleScript reserved-word bug that silently broke the macOS-15 add path).
#
# Usage:   ./scripts/preflight.sh
# Green => safe to bump version, update CHANGELOG, and merge to main (which
#          auto-tags + publishes to PyPI).
#
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

echo "──────────────────────────────────────────────────────────"
echo " applemusic-mcp · pre-release gate"
echo "──────────────────────────────────────────────────────────"

echo
echo "[1/3] Fast suite (mocked logic — same as CI)…"
"$PY" -m pytest -q

echo
echo "[2/3] Live environment check…"
"$PY" scripts/check_live_env.py

echo
echo "[3/3] Live UI integration suite (real Music.app, TEST_UI=1)…"
# Override the default '-m \"not slow and not ui\"' addopts so the ui-marked
# live gate actually runs. Capture output so we can guard against a false-green
# where every live test SKIPPED (e.g. env half-ready) and pytest still exits 0.
set +e
# Only the OS-aware live suite gates the release: it auto-selects the right add
# surface per macOS version and version-gates the surface-specific tests. (The
# older tests/test_applescript.py::TestUI*Live are pop-over-specific and would
# false-fail on macOS 15 where the pop-over isn't in the AX tree; run those via
# `make test-all` on a macOS 26 box, not as the gate.)
out="$(TEST_UI=1 "$PY" -m pytest -o addopts="" -m ui \
        tests/test_live_integration.py -v 2>&1)"
rc=$?
set -e
echo "$out"

if [ "$rc" -ne 0 ]; then
  echo
  echo "❌ Live suite FAILED — do NOT release. See failures above."
  exit 1
fi

# Guard: the gate is only satisfied if the cross-OS add flows actually PASSED.
# A run where they SKIPPED (no fresh candidate / not signed in) but some other
# test passed must NOT read as green — that's a false-green worse than no gate.
for t in test_library_add_full_flow test_add_to_playlist_full_flow; do
  if ! echo "$out" | grep -q "$t PASSED"; then
    echo
    echo "❌ $t did not PASS (skipped or failed) — gate NOT satisfied."
    echo "   Ensure Music.app is signed in with an active subscription and that"
    echo "   a fresh (not-already-in-library) test track is available, then re-run."
    echo "   (If candidates are exhausted by repeated runs, let iCloud settle so"
    echo "   just-removed tracks stop reading as in-library.)"
    exit 1
  fi
done

echo
echo "✅ Pre-release gate PASSED — safe to bump version + release."
echo "   Ideally run this on BOTH a macOS 15 and a macOS 26 machine: the add"
echo "   surfaces differ (deep-link vs pop-over) and only one runs per OS."
