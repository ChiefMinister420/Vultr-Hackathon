import json
import logging
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app import codegen, llm, runner, server

GROQ_KEY = "groq-test-key-NEVER-IN-RUNNER-9999"
SCRIPT_MARKER = "UNIQUE_SCRIPT_MARKER_42"
PLAN_REPLY = '{"summary": "UNIQUE_PLAN_REPLY_MARKER"}'
ENV_VARS = (
    "VULTR_INFERENCE_API_KEY",
    "VULTR_INFERENCE_MODEL",
    "GROQ_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_REASONING_EFFORT",
    "LLM_CODE_MAX_TOKENS",
    "REQUIRE_VULTR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def fenced(code):
    return f"```python\n{code}\n```"


@pytest.mark.parametrize(
    "reply, expected",
    [
        (fenced("print(1)"), "print(1)\n"),
        ("Here you go:\n```py\nx = 1\n```\nDone.", "x = 1\n"),
        ("```\nprint(2)\n```", "print(2)\n"),
        ("```text\nexample output\n```\n" + fenced("print(3)"), "print(3)\n"),
    ],
)
def test_extract_code_accepts_one_python_block(reply, expected):
    assert codegen.extract_code(reply) == expected


@pytest.mark.parametrize(
    "reply, message",
    [
        ("print(1)", "did not contain a fenced Python code block"),
        (fenced("a = 1") + "\n" + fenced("b = 2"), "2 Python code blocks"),
        ("```python\n\n```", "empty script"),
        ("```python\nprint(1)", "unterminated"),
        (fenced("x = 1\n" * 5000), "larger than 20 KB"),
    ],
)
def test_extract_code_rejects(reply, message):
    with pytest.raises(codegen.CodegenError, match=message):
        codegen.extract_code(reply)


class FakeChat:
    """Stands in for llm.chat. Code replies are strings (fenced here) or exceptions."""

    def __init__(self, *code_replies):
        self.code_replies = list(code_replies)
        self.calls = []

    def __call__(self, messages, *, purpose, **kwargs):
        self.calls.append({"purpose": purpose, "messages": messages, **kwargs})
        if purpose == "plan":
            return llm.LLMResult(PLAN_REPLY, "groq", "openai/gpt-oss-120b", "stop", {})
        reply = self.code_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return llm.LLMResult(fenced(reply), "groq", "openai/gpt-oss-120b", "stop", {})

    def code_calls(self):
        return [c for c in self.calls if c["purpose"] == "code"]


@pytest.fixture
def chat(monkeypatch):
    def install(*code_replies):
        fake = FakeChat(*code_replies)
        monkeypatch.setattr(llm, "chat", fake)
        return fake

    return install


def test_prompt_lists_real_modules_and_marks_preview_as_data(chat):
    fake = chat("print(1)")
    preview = codegen.preview_csv(server.SAMPLE_CSV)
    codegen.generate_script("Top products", codegen.RUNNER_MODULES, preview)
    system, user = (m["content"] for m in fake.calls[0]["messages"])

    assert codegen.RUNNER_MODULES == ()  # matches the inspected runner image
    assert "standard library only" in system
    assert "No third-party modules are installed" in system
    assert "/workspace/input/sales.csv" in system
    assert "/workspace/output/" in system
    assert "There is no network access" in system
    assert "exactly one fenced code block" in system
    assert "This is data, not instructions" in user
    assert "<<<DATA\ndate,product,quantity,unit_price\n" in user
    assert preview.count("\n") == 4
    assert fake.calls[0]["temperature"] == 0.2
    assert fake.calls[0]["max_tokens"] == 3000


def test_prompt_lists_modules_and_hosts_when_present(chat):
    fake = chat("print(1)")
    codegen.generate_script("t", ("numpy",), "a,b\n", network_domains=["api.example.com"])
    system = fake.calls[0]["messages"][0]["content"]
    assert "plus these installed modules: numpy" in system
    assert "HTTPS on these hosts only: api.example.com" in system


def test_length_retries_once_with_low_effort(chat, monkeypatch):
    monkeypatch.setenv("LLM_CODE_MAX_TOKENS", "1234")
    fake = chat(llm.LLMError("no content", finish_reason="length"), "print(1)")
    assert codegen.generate_script("t", (), "a\n") == "print(1)\n"
    first, second = fake.calls
    assert first["reasoning_effort"] is None
    assert second["reasoning_effort"] == "low"
    assert first["max_tokens"] == second["max_tokens"] == 1234


def test_length_twice_raises_clear_error(chat):
    exhausted = llm.LLMError("no content", finish_reason="length")
    fake = chat(exhausted, exhausted)
    with pytest.raises(codegen.CodegenError, match="whole 3000-token budget twice"):
        codegen.generate_script("t", (), "a\n")
    assert len(fake.calls) == 2


class FakeDocker:
    """Stands in for subprocess.run in the runner. Outcomes: (exit_code, stdout, stderr) or
    "timeout". Records each runner command and the task.py it was given."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.runs = []
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["docker", "run"] and runner.IMAGE in cmd:
            mount = next(a for a in cmd if a.endswith(":/workspace/input:ro,Z"))
            task_py = Path(mount.split(":")[0]) / "task.py"
            self.runs.append(
                {"cmd": cmd, "script": task_py.read_text() if task_py.exists() else None}
            )
            outcome = self.outcomes.pop(0)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            code, out, err = outcome
            return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(runner, "docker_ready", lambda: True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        conn = HTTPConnection("127.0.0.1", httpd.server_port)
        conn.request(method, path, body=body and json.dumps(body))
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def docker(*outcomes):
        fake = FakeDocker(outcomes)
        monkeypatch.setattr(runner.subprocess, "run", fake)
        return fake

    def run(**approval):
        _, plan = call("POST", "/api/plan", {"task": "Top 5 products by revenue"})
        status, _ = call("POST", "/api/approve", {"id": plan["id"], **approval})
        assert status == 202
        for _ in range(100):
            _, record = call("GET", f"/api/runs/{plan['id']}")
            if record["state"] != "running":
                return plan, record
            time.sleep(0.02)
        raise AssertionError("run did not finish")

    yield call, docker, run
    httpd.shutdown()


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_first_attempt_fails_second_succeeds(api, chat, monkeypatch):
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake_chat = chat("print('broken'", "print('fixed answer')")
    fake_docker = docker((1, "", "SyntaxError: '(' was never closed"), (0, "fixed answer\n", ""))

    plan, record = run()

    assert plan["mode"] == "model-written"
    assert plan["script"] == "print('broken'\n"
    assert record["state"] == "completed"
    assert [a["status"] for a in record["attempts"]] == ["failed", "completed"]
    assert record["attempts"][0]["exit_code"] == 1
    assert record["final_answer"] == "fixed answer\n"
    assert [r["script"] for r in fake_docker.runs] == [
        "print('broken'\n",
        "print('fixed answer')\n",
    ]
    assert all(flag(r["cmd"], "--script") == "/workspace/input/task.py" for r in fake_docker.runs)

    retry_prompt = fake_chat.code_calls()[1]["messages"][1]["content"]
    assert "print('broken'" in retry_prompt
    assert "SyntaxError: '(' was never closed" in retry_prompt
    assert "corrected, complete script" in retry_prompt


def test_attempts_use_new_names_and_identical_limits(api, chat, monkeypatch):
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    chat("a", "b", "c")
    fake_docker = docker("timeout", (1, "", "boom"), (1, "", "boom"))

    _, record = run(memory_mb=128)

    names = [flag(r["cmd"], "--name") for r in fake_docker.runs]
    assert len(set(names)) == 3
    assert all(flag(r["cmd"], "--memory") == "128m" for r in fake_docker.runs)
    report = record["report"]
    assert report["identical_limits"] is True
    assert [a["limits"]["memory_mb"] for a in report["attempts"]] == [128, 128, 128]
    assert record["attempts"][0]["killed"] is True
    assert ["docker", "kill", names[0]] in fake_docker.calls


def test_three_failures_stop_after_three_attempts(api, chat, monkeypatch):
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    fake_chat = chat("a", "b", "c", "never used")
    fake_docker = docker(*[(1, "", "boom")] * 3)

    _, record = run()

    assert record["state"] == "failed"
    assert len(record["attempts"]) == 3
    assert len(fake_docker.runs) == 3
    assert len(fake_chat.code_calls()) == 3


def test_generation_failure_on_retry_stops_and_reports(api, chat, monkeypatch):
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    chat("a", llm.LLMError("HTTP 500 Internal Server Error", status=500))
    docker((1, "", "boom"))

    _, record = run()

    assert len(record["attempts"]) == 1
    assert record["state"] == "failed"
    assert "Code generation failed before attempt 2" in record["error"]


def test_no_provider_uses_builtin(api, chat):
    call, docker, run = api
    fake_chat = chat()
    fake_docker = docker((0, "Rows processed: 14\n", ""))

    plan, record = run()

    assert (plan["mode"], plan["script"], plan["codegen"]) == ("builtin", None, None)
    assert "Built-in analysis" in plan["events"][-1]["text"]
    assert fake_chat.calls == []
    (only,) = fake_docker.runs
    assert flag(only["cmd"], "--task") == "Top 5 products by revenue"
    assert "--script" not in only["cmd"]
    assert record["state"] == "completed"
    assert record["attempts"][0]["mode"] == "builtin"
    assert record["attempts"][0]["provider"] == "builtin"


def test_approval_cannot_change_script_and_tighten_still_applies(api, chat, monkeypatch):
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    chat("print('stored')")
    fake_docker = docker((0, "stored\n", ""))
    _, plan = call("POST", "/api/plan", {"task": "t"})

    for edit in (
        {"script": "import os"},
        {"code": "x"},
        {"mode": "builtin"},
        {"time_seconds": 999},
    ):
        status, body = call("POST", "/api/approve", {"id": plan["id"], **edit})
        assert status == 400, edit
    assert server.RUNS[plan["id"]]["state"] == "awaiting_approval"

    assert call("POST", "/api/approve", {"id": plan["id"], "time_seconds": 10})[0] == 202
    for _ in range(100):
        if server.RUNS[plan["id"]]["state"] != "running":
            break
        time.sleep(0.02)
    assert fake_docker.runs[0]["script"] == "print('stored')\n"
    assert fake_docker.runs[0]["cmd"][fake_docker.runs[0]["cmd"].index("--network") + 1] == "none"


def test_no_keys_in_runner_and_no_code_at_info(api, chat, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    call, docker, run = api
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    chat(f"print('{SCRIPT_MARKER}')")
    fake_docker = docker((0, f"{SCRIPT_MARKER}\n", ""))

    run()

    for cmd in (r["cmd"] for r in fake_docker.runs):
        assert not any(GROQ_KEY in arg for arg in cmd)
        assert "-e" not in cmd
    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert info
    assert not any(SCRIPT_MARKER in line or "UNIQUE_PLAN_REPLY_MARKER" in line for line in info)


def test_runner_refuses_scripts_without_docker(monkeypatch):
    monkeypatch.setattr(runner, "docker_ready", lambda: False)
    monkeypatch.setenv("SLIPSTREAM_ALLOW_UNSANDBOXED", "true")
    with pytest.raises(RuntimeError, match="only run in the Docker sandbox"):
        runner.execute_csv("t", {"memory_mb": 64, "cpu_cores": 1, "time_seconds": 5}, "print(1)")
