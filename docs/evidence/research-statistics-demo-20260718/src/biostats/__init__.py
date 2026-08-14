"""Small, dependency-free helpers for reproducible differential-expression reports."""

from .report import build_report
from .stats import benjamini_hochberg, bootstrap_mean_ci, summarize_replicates

__all__ = [
    "benjamini_hochberg",
    "bootstrap_mean_ci",
    "summarize_replicates",
    "build_report",
]

