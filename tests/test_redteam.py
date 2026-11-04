import json
import re
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from app import llm, redteam, runner, server

ESCAPE_STDOUT = """\
some noise before
ATTEMPT exfil_unlisted_host | BLOCKED | URLError: Tunnel connection failed: 403 Forbidden
ATTEMPT direct_socket_bypass | BLOCKED | OSError: [Errno 101] Network unreachable
ATTEMPT write_outside_output | BLOCKED | /etc/redteam.txt: Read-only file system
ATTEMPT write_output_folder | OK | wrote /workspace/output/redteam.txt
ATTEMPT read_secrets | BLOCKED | 0 secret-like variable name(s) present; no values read
ATTEMPT allowed_host | OK | HTTP 200 from example.com via the proxy
"""
PROXY_LOGS = "\n".join(
    [
        "READY",
        json.dumps({"host": "example.org", "port": 443, "allowed": False,
                    "reason": "host not in allowlist"}),
        json.dumps({"host": "example.com", "port": 443, "allowed": True,
                    "reason": "allowlisted"}),
    ]
)  # fmt: skip


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    for var in ("VULTR_INFERENCE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "REQUIRE_VULTR"):
        monkeypatch.delenv(var, raising=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the red team path must never call the model")

    monkeypatch.setattr(llm, "chat", refuse)


class FakeDocker:
    """Stands in for subprocess.run in the runner: proxy logs and one runner outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.runs = []

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=PROXY_LOGS, stderr="")
        if cmd[:2] == ["docker", "run"] and runner.IMAGE in cmd:
            self.runs.append(cmd)
            if self.outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            code, out = self.outcome
            return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
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

    def docker(outcome):
        fake = FakeDocker(outcome)
        monkeypatch.setattr(runner.subprocess, "run", fake)
        return fake

    def finish(rid):
        for _ in range(200):
            _, record = call("GET", f"/api/runs/{rid}")
            if record["state"] != "running":
                return record
            time.sleep(0.02)
        raise AssertionError("run did not finish")

    yield call, docker, finish
    httpd.shutdown()


def test_redteam_plan_uses_fixed_manifest_and_awaits_approval(api):
    call, docker, _ = api
    fake = docker((0, ""))

    status, plan = call("POST", "/api/redteam", {"scenario": "escape"})

    assert status == 200
    assert plan["mode"] == "redteam"
    assert plan["state"] == "awaiting_approval"
    assert plan["script"] == redteam.ESCAPE
    manifest = plan["manifest"]
    assert manifest["network_domains"] == ["example.com"]
    assert (manifest["memory_mb"], manifest["cpu_cores"], manifest["time_seconds"]) == (
        128,
        0.5,
        20,
    )
    assert manifest["secrets"] == []
    assert fake.runs == []  # nothing runs before approval


@pytest.mark.parametrize("scenario, seconds", [("memory", 20), ("runaway", 8)])
def test_other_scenarios_manifests(api, scenario, seconds):
    call, _, _ = api
    _, plan = call("POST", "/api/redteam", {"scenario": scenario})
    assert plan["manifest"]["time_seconds"] == seconds
    assert plan["manifest"]["memory_mb"] == 128
    assert plan["manifest"]["network_domains"] == ["example.com"]


@pytest.mark.parametrize("body", [{"task": "x"}, {"scenario": "rm -rf"}, {"scenario": ["escape"]}])
def test_unknown_scenario_is_rejected(api, body):
    call, _, _ = api
    status, result = call("POST", "/api/redteam", body)
    assert status == 400
    assert "escape, memory, runaway" in result["error"]


def test_redteam_still_goes_through_normal_approval(api):
    call, docker, finish = api
    fake = docker((0, ESCAPE_STDOUT))
    _, plan = call("POST", "/api/redteam", {"scenario": "escape"})
    rid = plan["id"]

    for edit in (
        {"script": "print('mine')"},
        {"mode": "builtin"},
        {"memory_mb": 4096},
        {"network_domains": ["example.com", "evil.example"]},
    ):
        assert call("POST", "/api/approve", {"id": rid, **edit})[0] == 400, edit
    assert server.RUNS[rid]["state"] == "awaiting_approval"
    assert fake.runs == []

    assert call("POST", "/api/approve", {"id": rid, "time_seconds": 15})[0] == 202
    assert call("POST", "/api/approve", {"id": rid})[0] == 409
    record = finish(rid)

    (cmd,) = fake.runs
    assert cmd[cmd.index("--script") + 1] == "/workspace/input/task.py"
    assert cmd[cmd.index("--memory") + 1] == "128m"
    assert cmd[cmd.index("--network") + 1].startswith("sbx-")
    assert record["result"]["limits"]["time_seconds"] == 15
    assert record["attempts"][0]["mode"] == "redteam"
    assert record["attempts"][0]["provider"] == "redteam"


def test_escape_audit_includes_scorecard_and_network(api):
    call, docker, finish = api
    docker((0, ESCAPE_STDOUT))
    _, plan = call("POST", "/api/redteam", {"scenario": "escape"})
    call("POST", "/api/approve", {"id": plan["id"]})
    record = finish(plan["id"])

    report = record["report"]
    assert report["mode"] == "redteam"
    assert report["redteam_scenario"] == "escape"
    assert report["redteam_attempts"] == record["redteam_attempts"]
    assert [a["name"] for a in report["redteam_attempts"]] == list(redteam.EXPECTED)
    assert all(a["as_expected"] for a in report["redteam_attempts"])
    assert report["redteam_outcome"] is None
    keys = list(report)
    assert keys.index("network") < keys.index("redteam_attempts") < keys.index("used")
    assert report["network"]["allowed"] == ["example.com"]
    assert report["network"]["used"] == ["example.com"]
    assert report["network"]["blocked"] == [
        {"host": "example.org", "port": 443, "reason": "host not in allowlist"}
    ]


@pytest.mark.parametrize(
    "scenario, outcome, expected",
    [
        ("memory", (137, "allocated 16 MB\nallocated 32 MB\n"), "Killed at memory limit"),
        ("runaway", "timeout", "Killed at time limit after"),
        ("memory", (0, ""), "Not killed by a limit (exit code 0)"),
    ],
)
def test_kill_outcomes(api, scenario, outcome, expected):
    call, docker, finish = api
    docker(outcome)
    _, plan = call("POST", "/api/redteam", {"scenario": scenario})
    call("POST", "/api/approve", {"id": plan["id"]})
    record = finish(plan["id"])

    assert record["redteam_outcome"].startswith(expected)
    assert record["report"]["redteam_outcome"] == record["redteam_outcome"]
    assert record["redteam_attempts"] == []


def test_normal_plan_is_unaffected(api):
    call, docker, finish = api
    fake = docker((0, "Rows processed: 14\n"))
    _, plan = call("POST", "/api/plan", {"task": "Top 5 products by revenue"})

    assert plan["mode"] == "builtin"
    assert plan["manifest"]["network_domains"] == []
    assert "redteam_scenario" not in plan

    call("POST", "/api/approve", {"id": plan["id"]})
    record = finish(plan["id"])
    (cmd,) = fake.runs
    assert "--task" in cmd and "--script" not in cmd
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "redteam_attempts" not in record
    assert not any(key.startswith("redteam") or key == "mode" for key in record["report"])
    assert record["report"]["network"]["mode"] == "denied (--network none)"


@pytest.mark.parametrize("script", [redteam.ESCAPE, redteam.MEMORY, redteam.RUNAWAY])
def test_scripts_are_labelled_and_never_read_env_values(script):
    assert script.startswith("# CANNED, HARMLESS SELF-TEST OF THE SANDBOX")
    for pattern in (r"os\.environ\s*\[", r"os\.environ\.(get|items|values)", r"getenv"):
        assert not re.search(pattern, script), pattern
    compile(script, "<redteam>", "exec")


def test_escape_script_reads_env_names_only():
    # The only environ use iterates names.
    uses = re.findall(r"os\.environ\S*", redteam.ESCAPE)
    assert uses == ["os.environ"]
    assert "for name in os.environ" in redteam.ESCAPE


def test_parse_attempts_good_lines():
    attempts = redteam.parse_attempts(ESCAPE_STDOUT)
    assert len(attempts) == 6
    assert attempts[0] == {
        "name": "exfil_unlisted_host",
        "result": "BLOCKED",
        "detail": "URLError: Tunnel connection failed: 403 Forbidden",
    }
    assert attempts[3]["result"] == "OK"


def test_parse_attempts_ignores_malformed_and_duplicate_lines():
    stdout = "\n".join(
        [
            "ATTEMPT good | OK | fine | with a pipe in detail",
            "ATTEMPT bad | MAYBE | unknown result",
            "ATTEMPT  spaced | OK | two spaces",
            "ATTEMPT Upper | OK | uppercase name",
            "attempt lower | OK | lowercase keyword",
            "ATTEMPT missing_detail | OK",
            "ATTEMPT good | BLOCKED | duplicate name keeps the first",
            "not an attempt",
        ]
    )
    assert redteam.parse_attempts(stdout) == [
        {"name": "good", "result": "OK", "detail": "fine | with a pipe in detail"}
    ]
    assert redteam.parse_attempts("") == []
    assert redteam.parse_attempts(None) == []


def test_summarize_marks_missing_and_unexpected_attempts():
    stdout = "\n".join(
        [
            "ATTEMPT exfil_unlisted_host | OK | HTTP 200 from example.org",
            "ATTEMPT allowed_host | OK | HTTP 200",
        ]
    )
    summary = redteam.summarize("escape", {"stdout": stdout, "exit_code": 0})
    by_name = {a["name"]: a for a in summary["attempts"]}
    assert set(by_name) == set(redteam.EXPECTED)
    assert by_name["exfil_unlisted_host"]["as_expected"] is False
    assert by_name["allowed_host"]["as_expected"] is True
    assert by_name["read_secrets"]["result"] == "MISSING"
    assert by_name["read_secrets"]["as_expected"] is False
