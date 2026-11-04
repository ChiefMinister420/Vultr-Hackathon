import io
import json
import logging
import threading
import time
import urllib.error
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from app import llm, server
from app.policy import apply_policy

VULTR_KEY = "vultr-test-key-NEVER-LOG-1234"
GROQ_KEY = "groq-test-key-NEVER-LOG-5678"
REASONING = "PRIVATE CHAIN OF THOUGHT"
ENV_VARS = (
    "VULTR_INFERENCE_API_KEY",
    "VULTR_INFERENCE_MODEL",
    "GROQ_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_REASONING_EFFORT",
    "REQUIRE_VULTR",
)
# The planner prompt as it was before the refactor; Vultr requests must not change.
ORIGINAL_PROMPT = (
    "Return only a compact JSON object. No markdown or explanations. "
    "Keys: summary, network_domains, files, memory_mb, cpu_cores, time_seconds, secrets. "
    "Plan a CSV analysis using the sample file. Never request secrets or network access."
)
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def completion(content='{"summary": "Plan from model"}', finish_reason="stop"):
    return {
        "model": "served-model-name",
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "reasoning": REASONING},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


class FakeHTTP:
    """Stands in for urlopen: dicts are 200 bodies, ints are HTTP errors, exceptions raise."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.timeouts = []
        self.sleeps = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            raise urllib.error.HTTPError(
                request.full_url, item, "error", {"Retry-After": "2"}, io.BytesIO(b"")
            )
        return io.BytesIO(json.dumps(item).encode())

    def payload(self, index=0):
        return json.loads(self.requests[index].data)


@pytest.fixture
def http(monkeypatch):
    def install(*responses):
        fake = FakeHTTP(responses)
        monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
        monkeypatch.setattr(llm.time, "sleep", fake.sleeps.append)
        return fake

    return install


def test_groq_preset_request(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(completion(), completion())

    llm.chat(MESSAGES, purpose="plan", max_tokens=1500)
    llm.chat(MESSAGES, purpose="code", max_tokens=1500)

    assert fake.requests[0].full_url == "https://api.groq.com/openai/v1/chat/completions"
    assert fake.requests[0].get_header("Authorization") == f"Bearer {GROQ_KEY}"
    assert fake.requests[0].get_header("User-agent") == "slipstream/0.1"
    plan = fake.payload(0)
    assert plan["model"] == "openai/gpt-oss-120b"
    assert plan["max_completion_tokens"] == 1500
    assert "max_tokens" not in plan
    assert plan["reasoning_effort"] == "low"
    assert fake.payload(1)["reasoning_effort"] == "medium"


def test_groq_reasoning_effort_override_and_non_gpt_oss(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(completion(), completion())
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    llm.chat(MESSAGES, purpose="plan")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    llm.chat(MESSAGES, purpose="plan")
    assert fake.payload(0)["reasoning_effort"] == "high"
    assert "reasoning_effort" not in fake.payload(1)


def test_vultr_preset_request(http, monkeypatch):
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", VULTR_KEY)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    fake = http(completion())
    llm.chat(MESSAGES, purpose="plan", max_tokens=1000)
    payload = fake.payload()
    assert fake.requests[0].full_url == "https://api.vultrinference.com/v1/chat/completions"
    assert payload["max_tokens"] == 1000
    assert "max_completion_tokens" not in payload
    assert "reasoning_effort" not in payload


def test_vultr_planner_request_unchanged(http, monkeypatch):
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", VULTR_KEY)
    fake = http(completion())
    server.infer_manifest("Analyze the CSV")
    request = fake.requests[0]
    assert dict(request.header_items()) == {
        "Authorization": f"Bearer {VULTR_KEY}",
        "Content-type": "application/json",
    }
    assert fake.timeouts == [12]
    assert fake.payload() == {
        "model": "deepseek-v4.1-flash-1017",
        "temperature": 0,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": ORIGINAL_PROMPT},
            {"role": "user", "content": "Analyze the CSV"},
        ],
    }


def test_groq_planner_uses_1500_tokens(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(completion())
    server.infer_manifest("Analyze the CSV")
    assert fake.payload()["max_completion_tokens"] == 1500


def test_empty_content_with_length_raises(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    http(completion(content="", finish_reason="length"))
    with pytest.raises(llm.LLMError, match="token budget exhausted by reasoning") as info:
        llm.chat(MESSAGES, purpose="plan")
    assert info.value.finish_reason == "length"


def test_reasoning_never_in_result_or_logs(http, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    http(completion())
    result = llm.chat(MESSAGES, purpose="plan")
    assert result.text == '{"summary": "Plan from model"}'
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert REASONING not in repr(result)
    assert "reasoning" not in repr(result)
    assert REASONING not in caplog.text


def test_429_retries_once(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(429, completion())
    assert llm.chat(MESSAGES, purpose="plan").finish_reason == "stop"
    assert len(fake.requests) == 2
    assert fake.sleeps == [2.0]


def test_retry_gives_up_after_second_failure(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(503, 503)
    with pytest.raises(llm.LLMError, match="HTTP 503") as info:
        llm.chat(MESSAGES, purpose="plan")
    assert info.value.status == 503
    assert len(fake.requests) == 2


def test_client_error_not_retried(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake = http(401)
    with pytest.raises(llm.LLMError, match="HTTP 401"):
        llm.chat(MESSAGES, purpose="plan")
    assert len(fake.requests) == 1


def test_require_vultr_blocks_groq(http, monkeypatch):
    monkeypatch.setenv("REQUIRE_VULTR", "true")
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", VULTR_KEY)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    fake = http()
    with pytest.raises(llm.LLMError, match="REQUIRE_VULTR"):
        llm.chat(MESSAGES, purpose="plan")
    assert fake.requests == []
    assert llm.status()["require_vultr"] is True


def test_require_vultr_needs_vultr_key(monkeypatch):
    monkeypatch.setenv("REQUIRE_VULTR", "true")
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    with pytest.raises(llm.LLMError, match="VULTR_INFERENCE_API_KEY is not set"):
        llm.settings()


def test_default_provider_selection(monkeypatch):
    assert llm.settings() is None
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    assert llm.settings()["provider"] == "groq"
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", VULTR_KEY)
    assert llm.settings()["provider"] == "vultr"


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        completion(content="", finish_reason="length"),
        completion(content="no json here"),
    ],
)
def test_planner_falls_back_on_error(http, monkeypatch, failure):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    http(failure)
    manifest, source, planned_by = server.infer_manifest("Analyze the CSV")
    assert source == "Deterministic planner (Groq unavailable)"
    assert planned_by["provider"] == "builtin"
    assert manifest == apply_policy({})[0]


def test_authorization_never_logged(http, monkeypatch, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", VULTR_KEY)
    http(completion(), 401, 429, 500, urllib.error.URLError("down"))
    for _ in range(4):
        server.infer_manifest("Analyze the CSV")
    captured = capfd.readouterr()
    for output in (caplog.text, captured.out, captured.err):
        assert VULTR_KEY not in output
        assert "Bearer" not in output
    assert "HTTP 401" in caplog.text


@pytest.fixture
def api(monkeypatch):
    def fake_execute(task, manifest, script=None):
        return {"status": "completed", "engine": "Docker", "elapsed_seconds": 0, "limits": {}}

    monkeypatch.setattr(server, "execute_csv", fake_execute)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        conn = HTTPConnection("127.0.0.1", httpd.server_port)
        conn.request(method, path, body=body and json.dumps(body))
        resp = conn.getresponse()
        return json.loads(resp.read())

    yield call
    httpd.shutdown()


def test_provider_label_in_plan_run_record_and_audit(http, monkeypatch, api):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    http(completion())

    plan = api("POST", "/api/plan", {"task": "Analyze the CSV"})
    assert (plan["provider"], plan["model"], plan["finish_reason"]) == (
        "groq",
        "openai/gpt-oss-120b",
        "stop",
    )

    api("POST", "/api/approve", {"id": plan["id"]})
    for _ in range(50):
        record = api("GET", f"/api/runs/{plan['id']}")
        if record["state"] != "running":
            break
        time.sleep(0.05)
    assert (record["provider"], record["model"]) == ("groq", "openai/gpt-oss-120b")
    assert record["report"]["planner"] == {"provider": "groq", "model": "openai/gpt-oss-120b"}

    health = api("GET", "/api/health")["inference"]
    assert (health["provider"], health["model"], health["require_vultr"]) == (
        "groq",
        "openai/gpt-oss-120b",
        False,
    )
