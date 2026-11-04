import pytest

from app import llm, server

LIMIT_VARS = (
    "HOST",
    "ALLOWED_ORIGIN",
    "TRUST_PROXY",
    "MAX_CONCURRENT_RUNS",
    "RATE_LIMIT_PER_MIN",
    "MAX_LLM_CALLS_PER_DAY",
    "SANDBOX_RUNTIME",
)


@pytest.fixture(autouse=True)
def reset_limits(monkeypatch):
    """Every test starts with empty rate-limit buckets, no model calls today and default limits."""
    for var in LIMIT_VARS:
        monkeypatch.delenv(var, raising=False)
    server.RATE_HITS.clear()
    llm.CALLS.update(day=None, count=0)
