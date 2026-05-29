"""TaxLens — local-first multi-year US tax return analyzer."""
from taxlens.models import Return, TaxResult, ComputationStep
from taxlens.engine import compute
from taxlens.rules import load_rules

__all__ = ["Return", "TaxResult", "ComputationStep", "compute", "load_rules"]
__version__ = "0.0.1"
