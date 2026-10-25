"""Simulator-backed implementation of EvidenceAdapter.

Wraps src/evidence.py's gather_evidence() / TrackingSimulator —
the scripted demo evidence source. Like the PayPal adapter, it
delegates through the evidence MODULE at call time, so existing
fixtures that patch module state (notably
``monkeypatch.setattr(evidence.random, "uniform", ...)`` for a
stable seller history) keep working unchanged.

A second EvidenceAdapter (e.g. a GitHub code-deliverable gatherer)
implements the same gather() signature and evidence-dict contract —
see src/adapters/base.py — and is injected into DealManager in place
of this one; the kernel does not change.
"""
from .. import evidence as _evidence


class SimulatorEvidenceAdapter:
    """EvidenceAdapter over the simulator-backed gatherer."""

    def gather(self, deal, tracking, days_since_funding, messages=None):
        return _evidence.gather_evidence(
            deal, tracking, days_since_funding, messages)
