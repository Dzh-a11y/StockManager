"""Pure, local-only CAPM research primitives."""

from stock_manager.capm.math import CapmEstimate, CapmInputError, estimate_capm
from stock_manager.capm.returns import ReturnObservation, build_aligned_returns
from stock_manager.capm.service import CapmAnalysisService, CapmWindowResult

__all__ = [
    "CapmEstimate",
    "CapmAnalysisService",
    "CapmInputError",
    "ReturnObservation",
    "CapmWindowResult",
    "build_aligned_returns",
    "estimate_capm",
]
