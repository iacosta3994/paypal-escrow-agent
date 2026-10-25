"""Adapter boundaries for the escrow kernel (Wave 1 / A1).

The kernel — the deal state machine (deal_manager), the checklist,
and the Tier 0 decision rules — never talks to PayPal or to an
evidence source directly. It talks to the two narrow interfaces in
:mod:`src.adapters.base`:

  - :class:`MoneyAdapter`    — implemented by PayPalMoneyAdapter
  - :class:`EvidenceAdapter` — implemented by SimulatorEvidenceAdapter

The third boundary, the sensor, already existed before A1: the Laya
forest sits behind its 8 atomic questions (src/laya_questions.py)
and is consumed only through decision_engine's shadow path. It is
documented in ARCHITECTURE.md and deliberately not refactored here.
"""
from .base import EvidenceAdapter, MoneyAdapter
from .paypal import PayPalMoneyAdapter
from .simulator import SimulatorEvidenceAdapter

__all__ = [
    "EvidenceAdapter",
    "MoneyAdapter",
    "PayPalMoneyAdapter",
    "SimulatorEvidenceAdapter",
]
