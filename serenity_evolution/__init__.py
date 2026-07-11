"""Safety-first evolution primitives for SerenityMonitor."""

from .candidate import WeightCandidateGenerator
from .engine import EvolutionEngine
from .gate import PromotionGate
from .microstructure import AShareExecutionSimulator, AShareRules

__all__ = [
    "AShareExecutionSimulator",
    "AShareRules",
    "EvolutionEngine",
    "PromotionGate",
    "WeightCandidateGenerator",
]

__version__ = "0.1.0"
