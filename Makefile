PY ?= python3

.PHONY: help test test-all preflight preflight-ui

help:
	@echo "make test         - fast suite (mocked logic); what GitHub CI runs"
	@echo "make coverage     - fast suite + 100% coverage gate (no real account access)"
	@echo "make test-all     - fast + live API suite (needs tokens for a signed-in account)"
	@echo "make preflight    - PRE-RELEASE GATE: fast + live env check + live API suite"
	@echo "make preflight-ui - NATIVE UI GATE: live Music.app playback paths (run on iMac AND mini,"
	@echo "                    unlocked console session) before any UI-touching release"

# Fast, deterministic, no account needed. Mirrors CI (-m 'not slow and not ui').
test:
	$(PY) -m pytest -q

# Fast suite with coverage. Enforces 100% (fail_under in pyproject); external
# network is blocked in non-live tests, so this never touches a real account.
coverage:
	$(PY) -m pytest -q --cov=applemusic_mcp --cov-report=term-missing

# Everything, including the live API mutation tests. Requires a developer token
# (generate-token or harvested) + a media-user-token (`applemusic-mcp signin`)
# for an account with an active subscription.
test-all:
	TEST_API=1 $(PY) -m pytest -o addopts="" -v

# The gate to run before every release. See RELEASING.md.
preflight:
	./scripts/preflight.sh

# Native Music.app UI-automation gate. Run on the iMac AND the mini (unlocked
# console session, signed in) before any release that touches UI logic. Plays
# muted. See RELEASING.md.
preflight-ui:
	./scripts/preflight-ui.sh
