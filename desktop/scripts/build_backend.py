"""Cross-platform PyInstaller build script for the TaxLens backend.

Produces a single-file executable bundling the Python interpreter, all
`tax_rules/`, the static web UI, and demo returns. The Electron shell
auto-discovers and launches the binary, so end-users don't need Python.

Run from repo root:
    python desktop/scripts/build_backend.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "desktop" / "bin"
SEP = ";" if os.name == "nt" else ":"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure PyInstaller is available (assumes the active interpreter is the
    # project venv — CI workflow does this with `pip install pyinstaller`).
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "taxlens-backend",
        "--paths", str(REPO / "src"),
        "--add-data", f"{REPO / 'tax_rules'}{SEP}tax_rules",
        "--add-data", f"{REPO / 'src' / 'taxlens' / 'web'}{SEP}taxlens/web",
        "--add-data", f"{REPO / 'src' / 'taxlens' / 'demo'}{SEP}taxlens/demo",
        "--collect-submodules", "taxlens",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        str(REPO / "src" / "taxlens" / "__main__.py"),
    ]
    subprocess.check_call(args, cwd=str(REPO))

    exe_name = "taxlens-backend.exe" if os.name == "nt" else "taxlens-backend"
    src = REPO / "dist" / exe_name
    dst = OUT_DIR / exe_name
    shutil.copy2(src, dst)
    if os.name != "nt":
        dst.chmod(0o755)
    print(f"→ {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
