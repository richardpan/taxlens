"""Core federal tax computation engine.

This module is currently a single-file package shell: every public and
private name defined in :mod:`taxlens.engine._core` is re-exported here
so external imports (``from taxlens.engine import compute``,
``from taxlens.engine import _StepRecorder``, etc.) keep working.

The shell layout sets up gradual extraction — future commits can move
groups of related ``_compute_*`` helpers into dedicated submodules
(e.g. ``engine.income``, ``engine.deductions``, ``engine.tax``,
``engine.credits``, ``engine.totals``) without touching any caller.
"""
from . import _core

_g = globals()
for _name in list(vars(_core)):
    if _name.startswith("__"):
        continue
    _g[_name] = getattr(_core, _name)
del _g, _name
