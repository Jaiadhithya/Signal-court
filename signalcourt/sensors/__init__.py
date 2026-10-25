"""Pure-function sensors over the panel; no prose, no LLM."""

from .baseline import Baseline, fit_baseline
from .gap import normalise, scan_metric
from .per_share import adjust_per_share, share_count_steps

__all__ = ["Baseline", "adjust_per_share", "fit_baseline", "normalise", "scan_metric", "share_count_steps"]
