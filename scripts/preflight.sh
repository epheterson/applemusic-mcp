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
out="$(TEST_UI=1 "$PY" -m pytest -o addopts="" -m ui \
        tests/test_live_integration.py tests/test_applescript.py -v 2>&1)"
rc=$?
set -e
echo "$out"

if [ "$rc" -ne 0 ]; then
  echo
  echo "❌ Live suite FAILED — do NOT release. See failures above."
  exit 1
fi

# Guard: a release gate that silently ran zero tests is worse than none.
if ! echo "$out" | grep -qE "[1-9][0-9]* passed"; then
  echo
  echo "❌ Live suite ran 0 PASSING tests (everything skipped?)."
  echo "   The gate is NOT satisfied — verify Music.app is signed in with an"
  echo "   active subscription and a fresh test track is available, then re-run."
  exit 1
fi

echo
echo "✅ Pre-release gate PASSED — safe to bump version + release."
echo "   Ideally run this on BOTH a macOS 15 and a macOS 26 machine: the add"
echo "   surfaces differ (deep-link vs pop-over) and only one runs per OS."
