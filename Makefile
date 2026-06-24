PY ?= python3

.PHONY: help test test-all preflight

help:
	@echo "make test       - fast suite (mocked logic); what GitHub CI runs"
	@echo "make test-all   - fast + live API suite (needs tokens for a signed-in account)"
	@echo "make preflight  - PRE-RELEASE GATE: fast + live env check + live suite"

# Fast, deterministic, no account needed. Mirrors CI (-m 'not slow and not ui').
test:
	$(PY) -m pytest -q

# Everything, including the live API mutation tests. Requires a developer token
# (generate-token or harvested) + a media-user-token (`applemusic-mcp signin`)
# for an account with an active subscription.
test-all:
	TEST_API=1 $(PY) -m pytest -o addopts="" -v

# The gate to run before every release. See RELEASING.md.
preflight:
	./scripts/preflight.sh
