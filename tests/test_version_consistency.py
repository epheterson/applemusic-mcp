"""The version-surface check, run as a test.

`scripts/check_versions.py` guards the pre-release gate and `release.yml`, but both
of those fire late — at release time, when a mismatch is already committed. Running
it here means CI catches drift on every PR instead, which is where it actually slips
in: 0.18.0 shipped with `uv.lock` still at 0.17.0, and `server.json` sat at `1.0.0`
through several releases before anyone looked.

The second half of the file checks the checker. A guard that cannot fail is worse
than no guard, because it reads like coverage.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_versions.py"


def _load():
    """Load the script as a module (it lives in scripts/, not the package)."""
    spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repo_version_surfaces_agree():
    """The real guard: every version surface matches pyproject.toml, right now."""
    assert _load().main() == 0


@pytest.mark.parametrize(
    "path_fragment,label",
    [
        ("uv.lock", "uv.lock"),
        ("server.json", "server.json"),
        ("SKILL.md", "SKILL.md"),
        ("__init__.py", "__init__.py"),
    ],
)
def test_detects_drift_in_each_surface(path_fragment, label, capsys):
    """Each surface is actually inspected — not just present in the list."""
    mod = _load()
    real_read = mod._read

    def stale(rel: str) -> str:
        text = real_read(rel)
        if path_fragment in rel:
            # Rewrite just this surface to a version that cannot match.
            return text.replace(mod._pyproject_version(), "0.0.1-stale")
        return text

    mod._read = stale
    assert mod.main() == 1, f"{label} drift went undetected"
    assert "0.0.1-stale" in capsys.readouterr().err


def test_detects_a_missing_changelog_section(capsys):
    """release.yml silently falls back to auto-generated notes without one."""
    mod = _load()
    real_read = mod._read
    mod._read = lambda rel: (
        real_read(rel).replace("## [", "## x[") if rel.endswith("CHANGELOG.md") else real_read(rel)
    )
    assert mod.main() == 1
    assert "CHANGELOG" in capsys.readouterr().err


def test_reports_every_mismatch_not_just_the_first(capsys):
    """Fixing them one release at a time is how drift persists."""
    mod = _load()
    real_read = mod._read
    ver = mod._pyproject_version()
    mod._read = lambda rel: (
        real_read(rel).replace(ver, "0.0.1-stale")
        if ("uv.lock" in rel or "server.json" in rel)
        else real_read(rel)
    )
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "uv.lock" in err and "server.json" in err
