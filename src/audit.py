"""Append-only audit log. Every decision, with evidence, reasoning and cost."""
import json

from . import config
from .models import Decision


def record(decision: Decision) -> None:
    with open(config.AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(decision.to_dict()) + "\n")


def read_all() -> list[dict]:
    try:
        with open(config.AUDIT_LOG_PATH) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def total_spend() -> float:
    return round(sum(d.get("cost_usd", 0.0) for d in read_all()), 4)
