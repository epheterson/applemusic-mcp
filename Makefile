PY ?= python3

.PHONY: help test test-all preflight

help:
	@echo "make test       - fast suite (mocked logic); what GitHub CI runs"
	@echo "make test-all   - fast + live UI suite (needs signed-in Music.app)"
	@echo "make preflight  - PRE-RELEASE GATE: fast + live env check + live suite"

# Fast, deterministic, no Music.app needed. Mirrors CI (-m 'not slow and not ui').
test:
	$(PY) -m pytest -q

# Everything, including the live UI automation tests. Requires a Mac signed into
# Apple Music, unlocked, with Accessibility granted to the test runner.
test-all:
	TEST_UI=1 $(PY) -m pytest -o addopts="" -v

# The gate to run before every release. See RELEASING.md.
preflight:
	./scripts/preflight.sh
