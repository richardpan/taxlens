"""Build-time generated version string.

This file is overwritten by ``desktop/scripts/build_backend.py`` before
PyInstaller packaging so the bundled binary reports the correct version
in the UI footer and ``/api/health``. The committed value here is the
last-known release; it is what runs when developers execute the source
tree directly (no install, no PyInstaller).
"""
__version__ = "0.74.0"
