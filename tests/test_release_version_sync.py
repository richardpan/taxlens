"""Guard against the regression that shipped v0.27.3 through v0.34.0
with installer assets named `TaxLens-0.27.2.*` because desktop/package.json
had a stale hardcoded version that nobody synced to the git tag.

The release workflow now syncs the version at build time, but the
checked-in value should still track pyproject.toml so local dev builds
produce correctly-named artifacts too."""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "version not found in pyproject.toml"
    return m.group(1)


def test_desktop_package_version_matches_pyproject():
    py_version = _read_pyproject_version()
    pkg = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == py_version, (
        f"desktop/package.json version ({pkg['version']}) is out of sync with "
        f"pyproject.toml ({py_version}). electron-builder names installer "
        f"artifacts from this field — if you don't bump it, GitHub Release "
        f"assets will be misnamed (e.g. TaxLens-0.27.2.exe for a v0.34.0 tag)."
    )


def test_release_workflow_syncs_version_from_tag():
    """Belt-and-suspenders: even if someone forgets to bump
    desktop/package.json, the release workflow should rewrite it from
    the git tag before electron-builder runs."""
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "Sync desktop/package.json version from tag" in wf, (
        "release.yml is missing the step that syncs the desktop version "
        "from the git tag. Without it, stale checked-in versions silently "
        "produce misnamed installer artifacts."
    )
    # And the step must run before the electron-builder invocation.
    sync_idx = wf.index("Sync desktop/package.json version from tag")
    builder_idx = wf.index("electron-builder")
    assert sync_idx < builder_idx, (
        "Version-sync step must run before electron-builder, otherwise "
        "the artifacts are built with the stale version."
    )
