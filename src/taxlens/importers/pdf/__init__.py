"""PDF importer for IRS Form 1040 and supporting schedules.

This module is currently a single-file package shell: every public and
private name defined in :mod:`taxlens.importers.pdf._core` is re-exported
here so external imports (``from taxlens.importers.pdf import import_pdf``,
``from taxlens.importers.pdf import _extract_fields``, etc.) keep working.

The shell layout sets up gradual extraction — future commits can move
phases (text extraction, field-pattern registry, echo-guarded scanners,
W-2/Form 8889 supplements) into dedicated submodules without touching
any caller.
"""
from . import _core

_g = globals()
for _name in list(vars(_core)):
    if _name.startswith("__"):
        continue
    _g[_name] = getattr(_core, _name)
del _g, _name
