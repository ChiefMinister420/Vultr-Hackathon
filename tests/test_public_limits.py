import json
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from app import llm, runner, server


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    for var in ("VULTR_INFERENCE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "REQUIRE_VULTR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(runner, "docker_ready", lambda: True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None, raw=None):
        conn = HTTPConnection("127.0.0.1", httpd.server_port)
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read()), dict(resp.getheaders())

    yield call
    httpd.shutdown()


class BlockingDocker:
    """Fake subprocess.run: runner containers block until released."""

    def __init__(self):
        self.release = threading.Event()
        self.runs = 0

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ["docker", "run"] and runner.IMAGE in cmd:
            self.runs += 1
            self.release.wait(5)
            return subprocess.CompletedProcess(cmd, 0, stdout="done\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def plan(call, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else None
    return call("POST", "/api/plan", {"task": "Top products"}, headers)


def test_concurrency_cap_returns_429_and_keeps_plan_waiting(api, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "1")
    fake = BlockingDocker()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    first, second = plan(api)[1], plan(api)[1]

    assert api("POST", "/api/approve", {"id": first["id"]})[0] == 202
    status, body, headers = api("POST", "/api/approve", {"id": second["id"], "time_seconds": 5})

    assert status == 429
    assert "already running 1 task" in body["error"]
    assert headers["Retry-After"] == "10"
    assert server.RUNS[second["id"]]["state"] == "awaiting_approval"
    assert server.RUNS[second["id"]]["manifest"]["time_seconds"] == 30  # tighten not applied
    assert api("GET", "/api/health")[1]["concurrent_runs"] == 1

    fake.release.set()
    wait_until_done(first["id"])
    assert api("POST", "/api/approve", {"id": second["id"]})[0] == 202
    wait_until_done(second["id"])
    assert fake.runs == 2


def wait_until_done(rid):
    for _ in range(250):
        if server.RUNS[rid]["state"] != "running":
            return
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_rate_limit_after_threshold_and_per_ip(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "3")
    monkeypatch.setenv("TRUST_PROXY", "true")

    assert [plan(api, "203.0.113.1")[0] for _ in range(3)] == [200, 200, 200]
    status, body, headers = plan(api, "203.0.113.1")
    assert status == 429
    assert "at most 3 plans per minute" in body["error"]
    assert int(headers["Retry-After"]) > 0
    # Red team plans share the plan bucket.
    assert (
        api("POST", "/api/redteam", {"scenario": "escape"}, {"X-Forwarded-For": "203.0.113.1"})[0]
        == 429
    )
    # Another client is unaffected.
    assert plan(api, "203.0.113.2")[0] == 200


def test_total_limit_counts_approvals_too(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "2")
    assert [plan(api)[0] for _ in range(2)] == [200, 200]
    # 4 total per minute: 2 plans used, 2 approvals left (unknown ids still count).
    assert [api("POST", "/api/approve", {"id": "x"})[0] for _ in range(3)] == [404, 404, 429]


def test_forwarded_for_ignored_without_trust_proxy(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "2")
    assert plan(api, "198.51.100.1")[0] == 200
    assert plan(api, "198.51.100.2")[0] == 200
    # Both counted against the socket address, so a spoofed header does not help.
    assert plan(api, "198.51.100.3")[0] == 429


def test_trusted_proxy_uses_last_forwarded_entry(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1")
    monkeypatch.setenv("TRUST_PROXY", "true")
    assert plan(api, "1.2.3.4, 203.0.113.9")[0] == 200
    # A client-chosen first entry does not create a new bucket.
    assert plan(api, "5.6.7.8, 203.0.113.9")[0] == 429


def test_oversized_task_and_body_rejected(api):
    status, body, _ = api("POST", "/api/plan", {"task": "x" * 1001})
    assert status == 400
    assert "longer than 1000 characters" in body["error"]
    assert api("POST", "/api/plan", {"task": "x" * 1000})[0] == 200

    status, body, _ = api("POST", "/api/plan", raw=json.dumps({"task": "t", "pad": "x" * 17000}))
    assert status == 413
    assert "larger than 16 KB" in body["error"]

    assert api("POST", "/api/plan", raw="[1, 2]")[0] == 400


def test_daily_llm_cap_forces_labelled_fallback(api, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("MAX_LLM_CALLS_PER_DAY", "2")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    llm.CALLS.update(day=time.strftime("%Y-%m-%d", time.gmtime()), count=2)

    status, record, _ = plan(api)

    assert status == 200
    assert record["planner"] == "Deterministic planner (daily model call cap reached)"
    assert record["provider"] == "builtin"
    assert record["mode"] == "builtin"
    assert "Daily model call cap reached" in record["codegen_error"]
    health = api("GET", "/api/health")[1]
    assert health["llm_calls_today"] == {"used": 2, "cap": 2}
    assert health["inference"]["status"] == "capped"


def test_llm_chat_refuses_past_the_cap(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("MAX_LLM_CALLS_PER_DAY", "1")
    llm.CALLS.update(day=time.strftime("%Y-%m-%d", time.gmtime()), count=1)
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(llm.LLMError, match=r"Daily model call cap \(1\) reached"):
        llm.chat([{"role": "user", "content": "hi"}], purpose="plan")
    # A new UTC day resets the count.
    llm.CALLS["day"] = "2000-01-01"
    assert llm.calls_today() == {"used": 0, "cap": 1}


def test_eviction_keeps_store_bounded(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1000")
    monkeypatch.setattr(server, "MAX_RECORDS", 5)
    server.RUNS.clear()
    now = time.time()
    server.RUNS["old"] = {"state": "completed", "events": [{"at": now - 3 * 3600}]}
    server.RUNS["stale-plan"] = {"state": "awaiting_approval", "events": [{"at": now - 7300}]}
    server.RUNS["old-but-running"] = {"state": "running", "events": [{"at": now - 3 * 3600}]}

    ids = [plan(api)[1]["id"] for _ in range(8)]

    assert "old" not in server.RUNS
    assert "stale-plan" not in server.RUNS
    assert "old-but-running" in server.RUNS
    assert len(server.RUNS) == 5
    assert set(ids[-4:]) <= set(server.RUNS)
    server.RUNS.clear()


def test_no_cors_wildcard(api, monkeypatch):
    for _status, _, headers in (api("GET", "/api/health"), plan(api)):
        assert "Access-Control-Allow-Origin" not in headers

    monkeypatch.setenv("ALLOWED_ORIGIN", "https://demo.example.com")
    headers = api("GET", "/api/health")[2]
    assert headers["Access-Control-Allow-Origin"] == "https://demo.example.com"


def test_health_reports_deployment_fields(api, monkeypatch):
    health = api("GET", "/api/health")[1]
    assert health["runtime"] == "runc default"
    assert health["require_vultr"] is False
    assert (health["provider"], health["model"]) == (None, None)
    assert health["concurrent_runs"] == 0
    assert health["max_concurrent_runs"] == 2

    monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "vultr-test-key")
    monkeypatch.setenv("REQUIRE_VULTR", "true")
    health = api("GET", "/api/health")[1]
    assert health["runtime"] == "runsc"
    assert health["require_vultr"] is True
    assert health["provider"] == "vultr"
    assert health["model"] == "deepseek-v4.1-flash-1017"
    assert "vultr-test-key" not in json.dumps(health)
