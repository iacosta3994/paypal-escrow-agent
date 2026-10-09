"""Environment-based configuration."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # deps not installed yet; env vars still work


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


PAYPAL_CLIENT_ID = get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = get("PAYPAL_CLIENT_SECRET")
PAYPAL_MODE = get("PAYPAL_MODE", "sandbox")  # sandbox | live

LLM_TIER1_PROVIDER = get("LLM_TIER1_PROVIDER", "openai")
LLM_TIER1_MODEL = get("LLM_TIER1_MODEL", "gpt-4o-mini")
LLM_TIER2_PROVIDER = get("LLM_TIER2_PROVIDER", "anthropic")
LLM_TIER2_MODEL = get("LLM_TIER2_MODEL", "claude-sonnet-4-5")

DASHBOARD_PORT = int(get("DASHBOARD_PORT", "5000"))
AUDIT_LOG_PATH = get("AUDIT_LOG_PATH", "./audit.log.jsonl")
