#!/usr/bin/env bash
#
# Pre-release gate. Run this on a machine signed into Apple Music (tokens
# configured) BEFORE bumping the version, because the live API mutation paths
# can ONLY be validated against a real, signed-in account with an ACTIVE
# subscription — GitHub CI (no tokens, no account) cannot. This is the gate
# that catches #28/#37-class regressions and the branch-A "DELETE is broken on
# the public host" finding.
#
# Unlike the old UI gate this is cross-platform: it needs a developer token
# (generated or harvested) + a media-user-token (`applemusic-mcp signin`), not
# Music.app or an unlocked Mac.
#
# Usage:   ./scripts/preflight.sh
# Green => safe to bump version, update CHANGELOG, and open a PR (merge to main
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
echo "[2/3] Live API environment check…"
"$PY" scripts/check_live_env.py

echo
echo "[3/3] Live API integration suite (real account, TEST_API=1)…"
# The hermetic conftest points APPLEMUSIC_MCP_HOME at a temp dir so unit tests never
# touch real creds — but that also hides the real (file-stored) media-user-token from
# the live gate, which would then SKIP. Inject it via APPLEMUSIC_USER_TOKEN (the same
# env var CI/containers use) so the live gate sees the signed-in account.
_LIVE_TOK="$("$PY" -c "from applemusic_mcp.auth import get_user_token; print(get_user_token())" 2>/dev/null || true)"
# Override the default '-m \"not slow and not ui\"' addopts so the ui-marked
# live gate actually runs. Capture output so we can guard against a false-green
# where every live test SKIPPED (e.g. env half-ready) and pytest still exits 0.
set +e
out="$(APPLEMUSIC_USER_TOKEN="$_LIVE_TOK" TEST_API=1 "$PY" -m pytest -o addopts="" -m ui \
        tests/test_live_integration.py -v 2>&1)"
rc=$?
set -e
unset _LIVE_TOK
echo "$out"

if [ "$rc" -ne 0 ]; then
  echo
  echo "❌ Live suite FAILED — do NOT release. See failures above."
  exit 1
fi

# Guard: the gate is only satisfied if the core mutation flows actually PASSED.
# A run where they SKIPPED (no tokens / storefront didn't resolve) but pytest
# still exited 0 must NOT read as green — that's a false-green worse than no gate.
for t in test_full_mutation_lifecycle test_rating_roundtrip; do
  if ! echo "$out" | grep -q "$t PASSED"; then
    echo
    echo "❌ $t did not PASS (skipped or failed) — gate NOT satisfied."
    echo "   Ensure the account is signed in (tokens configured) with an ACTIVE"
    echo "   subscription, then re-run. (See scripts/check_live_env.py output above.)"
    exit 1
  fi
done

echo
echo "✅ Pre-release gate PASSED — safe to bump version + release."
