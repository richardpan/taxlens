"""Pydantic schemas for the taxlens domain.

This module is currently a single-file package shell: every public and
private name defined in :mod:`taxlens.models._core` is re-exported here
so external imports (``from taxlens.models import Return``,
``from taxlens.models import FilingStatus``, etc.) keep working.

The shell layout sets up gradual extraction — future commits can move
individual classes into dedicated submodules (e.g.
``taxlens.models._return``, ``taxlens.models._result``,
``taxlens.models._rules``) without touching any caller.
"""
from . import _core

# Re-export every name (public and underscore-private) defined in _core.
# The dict-update pattern preserves *all* attributes — including the few
# private helpers that downstream modules and tests legitimately import
# — without forcing us to enumerate each name by hand.
_g = globals()
for _name in list(vars(_core)):
    if _name.startswith("__"):
        continue
    _g[_name] = getattr(_core, _name)
del _g, _name
