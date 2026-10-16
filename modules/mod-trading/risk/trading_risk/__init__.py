"""Non-LLM risk engine for the trading domain (protected path, see CLAUDE.md).

Every order passes `RiskEngine.check` before it reaches a broker, and the kill switch stops all
orders independently of any model. This package depends on nothing but Pydantic so it can be
reviewed on its own.
"""

from trading_risk.engine import (
    AccountView,
    OrderCheck,
    RiskDecision,
    RiskEngine,
    RiskLimits,
    Side,
    Violation,
    ViolationCode,
)
from trading_risk.killswitch import KillSwitch, KillSwitchState

__all__ = [
    "AccountView",
    "KillSwitch",
    "KillSwitchState",
    "OrderCheck",
    "RiskDecision",
    "RiskEngine",
    "RiskLimits",
    "Side",
    "Violation",
    "ViolationCode",
]
