"""Pure-function sensors over the panel; no prose, no LLM."""

from .baseline import Baseline, fit_baseline
from .gap import scan_metric

__all__ = ["Baseline", "fit_baseline", "scan_metric"]
