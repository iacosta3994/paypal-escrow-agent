"""Post-settlement outcome feedback (Wave 1 / A3).

The gap this closes: decisions are appended to the audit log
(``src/audit.py``) and deals then settle — RELEASED (capture),
REFUNDED (void) or SPLIT_SETTLED — but nothing used to record what
happened *after* settlement. Without those labels a past verdict
cannot be evaluated ("was that RELEASE actually fine?") or learned
from. This module is the write and read path for those labels.

Storage: outcomes live in the SAME append-only audit trail
(``config.AUDIT_LOG_PATH``) as their own record type —
``record_type: "outcome"`` — referencing the decision they label.
Decision records are never mutated, rewritten or reordered:
recording an outcome only ever appends one new line (the
append-only invariant is pinned by test). Outcome rows carry no
``cost_usd``, so ``audit.total_spend()`` is unaffected, and the
``record_type`` discriminator lets decision-only consumers (the
dashboard feed in ``src/app.py``, ``e2e_support.audit_rows_for``)
tell the two row types apart. Legacy decision rows predate the
field: a row is a decision iff its ``record_type`` is absent or
``"decision"``.

Why not ``audit.record()``: that function is Decision-typed by
design, so this module appends to the same path itself (the pattern
``src/notifications.py`` uses for its own trail) and ``src/audit.py``
needs no changes.

Outcome vocabulary — refined against the code's actual settling
vocabulary (``Verdict`` / ``DealState``):

  settling verdict   terminal state    post-settlement outcomes
  -----------------  ----------------  --------------------------
  release            released          clean, release_disputed_later
  refund             refunded          clean, refund_contested
  split              split_settled     clean, split_accepted,
                                       split_rejected

``clean`` is the one universal kind: the settlement stood with no
later dispute, contest or rejection, whichever way the money went.
Every other kind names the way one specific settlement was later
challenged or confirmed, so a kind that contradicts the linked
verdict (e.g. ``split_accepted`` on a full RELEASE) is rejected —
a nonsense label would silently corrupt the aggregates below.

A decision record is identified by ``(deal_id, decided_at)`` —
``Decision`` carries no id of its own — so an outcome references its
decision by copying the settling decision's ``decided_at`` into
``decision_decided_at`` and its verdict into ``verdict``.

Duplicate handling — IDEMPOTENT (chosen over reject-explicitly):
an outcome's identity is ``(deal_id, kind, source, note,
decision_decided_at)``. Recording an identical outcome a second
time returns the already-stored record and appends nothing, so a
retried submission (webhook redelivery, operator double-click, a
batch job re-run) can never double-count a label. Timestamps are
deliberately NOT part of the identity: a retry usually carries a
fresh timestamp but is the same outcome. The same kind/source with
a different note is a *different* outcome and appends normally.
"""
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum

from . import audit, config
from .models import DealState, Verdict

RECORD_TYPE = "outcome"
DECISION_RECORD_TYPE = "decision"


class OutcomeKind(str, Enum):
    CLEAN = "clean"
    RELEASE_DISPUTED_LATER = "release_disputed_later"
    REFUND_CONTESTED = "refund_contested"
    SPLIT_ACCEPTED = "split_accepted"
    SPLIT_REJECTED = "split_rejected"


class OutcomeSource(str, Enum):
    BUYER = "buyer"
    SELLER = "seller"
    OPERATOR = "operator"


SOURCES = frozenset(s.value for s in OutcomeSource)

# Settling verdicts (money moved) and the terminal state each lands.
_SETTLING_VERDICTS = frozenset(
    v.value for v in (Verdict.RELEASE, Verdict.REFUND, Verdict.SPLIT))
SETTLED_STATES = frozenset(
    (DealState.RELEASED, DealState.REFUNDED, DealState.SPLIT_SETTLED))
_STATE_TO_VERDICT = {
    DealState.RELEASED: Verdict.RELEASE.value,
    DealState.REFUNDED: Verdict.REFUND.value,
    DealState.SPLIT_SETTLED: Verdict.SPLIT.value,
}

# Which verdicts each kind may label (see module docstring).
_KIND_VERDICTS = {
    OutcomeKind.CLEAN.value: _SETTLING_VERDICTS,
    OutcomeKind.RELEASE_DISPUTED_LATER.value: {Verdict.RELEASE.value},
    OutcomeKind.REFUND_CONTESTED.value: {Verdict.REFUND.value},
    OutcomeKind.SPLIT_ACCEPTED.value: {Verdict.SPLIT.value},
    OutcomeKind.SPLIT_REJECTED.value: {Verdict.SPLIT.value},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_iso(value, field: str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(f"{field} must be an ISO timestamp string, "
                     f"got {value!r}")


def _kind_value(kind) -> str:
    if isinstance(kind, OutcomeKind):
        return kind.value
    if isinstance(kind, str):
        try:
            return OutcomeKind(kind.strip().lower()).value
        except ValueError:
            pass
    raise ValueError(
        f"kind must be one of {sorted(k.value for k in OutcomeKind)}, "
        f"got {kind!r}")


def _source_value(source) -> str:
    if isinstance(source, OutcomeSource):
        return source.value
    if isinstance(source, str) and source.strip().lower() in SOURCES:
        return source.strip().lower()
    raise ValueError(
        f"source must be one of {sorted(SOURCES)}, got {source!r}")


def is_decision_row(row: dict) -> bool:
    """A stored row is a decision iff it is not an outcome row
    (legacy decision rows carry no ``record_type`` at all)."""
    return row.get("record_type") in (None, DECISION_RECORD_TYPE)


def is_outcome_row(row: dict) -> bool:
    return row.get("record_type") == RECORD_TYPE


def _decisions_for(rows: list[dict], deal_id: str) -> list[dict]:
    return [r for r in rows
            if r.get("deal_id") == deal_id and is_decision_row(r)]


def _settling_decision(decisions: list[dict]) -> dict | None:
    """The decision that settled the deal: the LAST decision row
    whose verdict moves money (a deal may first accumulate
    INVESTIGATE rows from monitoring cycles)."""
    settling = [d for d in decisions
                if d.get("verdict") in _SETTLING_VERDICTS]
    return settling[-1] if settling else None


def _outcome_id(deal_id: str, kind: str, source: str, note: str,
                decision_decided_at) -> str:
    identity = json.dumps(
        [deal_id, kind, source, note, decision_decided_at],
        separators=(",", ":"))
    return "outcome-" + hashlib.sha256(
        identity.encode()).hexdigest()[:16]


def _identity(row: dict) -> tuple:
    return (row.get("deal_id"), row.get("kind"), row.get("source"),
            row.get("note"), row.get("decision_decided_at"))


def read_outcomes(deal_id: str | None = None) -> list[dict]:
    """All outcome rows in the audit trail, in file order,
    optionally restricted to one deal."""
    rows = [r for r in audit.read_all() if is_outcome_row(r)]
    if deal_id is not None:
        rows = [r for r in rows if r.get("deal_id") == deal_id]
    return rows


def record_outcome(deal_id: str, kind, source, note: str = "", *,
                   deal=None, decided_at=None,
                   occurred_at=None) -> dict:
    """Record a post-settlement outcome for a settled deal.

    Appends one ``record_type: "outcome"`` row to the audit trail
    referencing the deal's settling decision, and returns the stored
    record. An identical outcome (see module docstring) is a no-op
    that returns the existing record instead of appending.

    Settlement is proven from the audit trail itself: the deal must
    have a decision record with a settling verdict (release /
    refund / split). When the caller holds the ``Deal`` object it
    may pass ``deal=`` — then the deal's terminal state
    (released / refunded / split_settled) is authoritative instead,
    which also covers deals settled by a direct ``settle()`` call
    whose decision was never audited. ``decided_at`` explicitly
    picks which decision record the outcome labels; by default the
    settling decision is linked.

    Raises ValueError — appending nothing — for an invalid kind or
    source, an unknown deal id, a deal that has not settled, a
    ``decided_at`` matching no decision for the deal, or a kind
    that contradicts the linked verdict.
    """
    kind_v = _kind_value(kind)
    source_v = _source_value(source)
    if not isinstance(note, str):
        raise ValueError(f"note must be a string, got {note!r}")
    if deal is not None and deal.id != deal_id:
        raise ValueError(
            f"deal object id {deal.id!r} does not match "
            f"deal_id {deal_id!r}")

    rows = audit.read_all()
    decisions = _decisions_for(rows, deal_id)

    linked = None
    if decided_at is not None:
        ref = _as_iso(decided_at, "decided_at")
        for d in decisions:
            if d.get("decided_at") == ref:
                linked = d
                break
        if linked is None:
            raise ValueError(
                f"no decision record for deal {deal_id!r} with "
                f"decided_at {ref!r}")
        if linked.get("verdict") not in _SETTLING_VERDICTS:
            raise ValueError(
                f"decision at {ref!r} for deal {deal_id!r} has "
                f"verdict {linked.get('verdict')!r}, which does not "
                f"settle a deal; outcomes attach to the settling "
                f"decision")
        verdict = linked["verdict"]
    elif deal is not None:
        if deal.state not in SETTLED_STATES:
            raise ValueError(
                f"deal {deal_id!r} has not settled (state "
                f"{deal.state.value}); outcomes attach only to "
                f"settled deals")
        verdict = _STATE_TO_VERDICT[deal.state]
        settling = _settling_decision(decisions)
        if settling is not None and settling.get("verdict") == verdict:
            linked = settling
    else:
        if not decisions:
            raise ValueError(
                f"unknown deal {deal_id!r}: no decision records in "
                f"the audit log")
        linked = _settling_decision(decisions)
        if linked is None:
            raise ValueError(
                f"deal {deal_id!r} has not settled: no release / "
                f"refund / split decision in the audit log")
        verdict = linked["verdict"]

    if verdict not in _KIND_VERDICTS[kind_v]:
        raise ValueError(
            f"outcome kind {kind_v!r} cannot label a "
            f"{verdict!r} verdict (allowed verdicts: "
            f"{sorted(_KIND_VERDICTS[kind_v])})")

    decision_ref = linked.get("decided_at") if linked else None
    identity = (deal_id, kind_v, source_v, note, decision_ref)
    for row in rows:
        if is_outcome_row(row) and _identity(row) == identity:
            return dict(row)  # idempotent replay: nothing appended

    recorded_at = _now_iso()
    record = {
        "record_type": RECORD_TYPE,
        "outcome_id": _outcome_id(deal_id, kind_v, source_v, note,
                                  decision_ref),
        "deal_id": deal_id,
        "kind": kind_v,
        "source": source_v,
        "note": note,
        "verdict": verdict,
        "decision_decided_at": decision_ref,
        "occurred_at": (_as_iso(occurred_at, "occurred_at")
                        if occurred_at is not None else recorded_at),
        "recorded_at": recorded_at,
    }
    with open(config.AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def labeled_decision(deal_id: str) -> dict | None:
    """The labeled decision for one deal: its original decision
    record(s) plus every outcome attached to them.

    Returns None when the audit trail holds nothing for the deal.
    Otherwise a dict with ``deal_id``, ``decisions`` (all decision
    rows, in file order), ``decision`` (the settling decision, or
    the latest decision if none settled, or None), ``verdict``
    (the settling verdict, if any), ``outcomes`` (outcome rows, in
    file order) and ``is_labeled`` (True once any outcome exists).
    """
    rows = audit.read_all()
    decisions = _decisions_for(rows, deal_id)
    outcomes = [r for r in rows if is_outcome_row(r)
                and r.get("deal_id") == deal_id]
    if not decisions and not outcomes:
        return None
    settling = _settling_decision(decisions)
    decision = settling or (decisions[-1] if decisions else None)
    verdict = settling.get("verdict") if settling else None
    if verdict is None and outcomes:
        verdict = outcomes[-1].get("verdict")
    return {
        "deal_id": deal_id,
        "decisions": decisions,
        "decision": decision,
        "verdict": verdict,
        "outcomes": outcomes,
        "is_labeled": bool(outcomes),
    }


def aggregate_outcomes() -> dict:
    """Outcome counts grouped by the verdict they label.

    ``by_verdict`` answers the dashboard question — of the deals
    decided RELEASE, how many were later disputed, how many closed
    clean — as ``{verdict: {kind: count, ..., "total": n}}`` with
    every settling verdict present even at zero outcomes.
    Counts are of outcome *records* (one deal can carry several
    distinct outcomes over time); ``labeled_deals`` counts distinct
    deals carrying at least one outcome. An outcome row missing its
    denormalized verdict falls back to the deal's settling
    decision, then to the ``"unknown"`` bucket.
    """
    rows = audit.read_all()
    outcomes = [r for r in rows if is_outcome_row(r)]
    settling_by_deal = {}
    for r in rows:
        if is_decision_row(r) and r.get("verdict") in _SETTLING_VERDICTS:
            settling_by_deal[r.get("deal_id")] = r["verdict"]

    by_verdict: dict[str, dict[str, int]] = {
        v: {k.value: 0 for k in OutcomeKind} | {"total": 0}
        for v in sorted(_SETTLING_VERDICTS)
    }
    by_kind = {k.value: 0 for k in OutcomeKind}
    labeled = set()
    for row in outcomes:
        verdict = (row.get("verdict")
                   or settling_by_deal.get(row.get("deal_id"))
                   or "unknown")
        bucket = by_verdict.setdefault(
            verdict,
            {k.value: 0 for k in OutcomeKind} | {"total": 0})
        kind = row.get("kind")
        if kind in bucket:
            bucket[kind] += 1
        bucket["total"] += 1
        if kind in by_kind:
            by_kind[kind] += 1
        labeled.add(row.get("deal_id"))
    return {
        "by_verdict": by_verdict,
        "by_kind": by_kind,
        "total_outcomes": len(outcomes),
        "labeled_deals": len(labeled),
    }
