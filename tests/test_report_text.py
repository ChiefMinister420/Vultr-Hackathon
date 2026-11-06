import json
import re
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from app import redteam, report_text, server

LEVELS = {"ok", "note", "blocked", "problem", "result"}
BANNED = ("--network", "exit code", "137", "limits", "sbx-", "slipstream-", "Errno", "HTTP_PROXY")
KEY_PATTERN = re.compile(r"\b(sk|gsk|vk)[-_][A-Za-z0-9]{8,}|\b[A-Za-z0-9]{32,}\b")
NONE_WORD = re.compile(r"\bNone\b")

ANALYSIS = "CSV loaded: sales.csv\nRows processed: 14\nTerra Bottle: $735.00\n"
TRACEBACK = (
    'Traceback (most recent call last):\n  File "/workspace/input/task.py", line 3\n'
    "FileNotFoundError: [Errno 2] No such file or directory: '/workspace/input/sale.csv'\n"
)
ESCAPE_STDOUT = """\
ATTEMPT exfil_unlisted_host | BLOCKED | URLError: Tunnel connection failed: 403 Forbidden
ATTEMPT direct_socket_bypass | BLOCKED | OSError: [Errno 101] Network unreachable
ATTEMPT write_outside_output | BLOCKED | /etc/redteam.txt: Read-only file system
ATTEMPT write_output_folder | OK | wrote /workspace/output/redteam.txt
ATTEMPT read_secrets | BLOCKED | 0 secret-like variable name(s) present; no values read
ATTEMPT allowed_host | OK | HTTP 200 from example.com via the proxy
"""


def limits(memory=256, cpu=1.0, seconds=30, domains=()):
    return {"memory_mb": memory, "cpu_cores": cpu, "time_seconds": seconds,
            "network_domains": list(domains)}  # fmt: skip


def result(status="completed", stdout="", stderr="", elapsed=0.47, exit_code=0, killed=False,
           lim=None, events=(), files=(), artifact=None):  # fmt: skip
    return {
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_seconds": elapsed,
        "artifact": artifact,
        "engine": "Docker",
        "killed": killed,
        "limits": lim or limits(),
        "net_events": list(events),
        "container": "slipstream-0123456789ab",
        "exit_code": exit_code,
        "files": list(files),
    }


def make_run(mode, results, written_by=None, scenario=None, error=None):
    """A run record shaped exactly as server._run builds it."""
    last = results[-1]
    domains = last["limits"]["network_domains"]
    manifest, _ = server.apply_policy(
        {"network_domains": domains, "memory_mb": last["limits"]["memory_mb"],
         "cpu_cores": last["limits"]["cpu_cores"], "time_seconds": last["limits"]["time_seconds"]}
    )  # fmt: skip
    written_by = written_by or {}
    if mode == server.REDTEAM:
        written_by = dict(server.REDTEAM_SOURCE)
    attempts = [
        server.attempt_record(n, mode, written_by, "print(1)", r) for n, r in enumerate(results, 1)
    ]
    summary = redteam.summarize(scenario, last) if mode == server.REDTEAM else None
    rec = {
        "mode": mode,
        "manifest": manifest,
        "attempts": attempts,
        "result": last,
        "state": last["status"],
        "final_answer": attempts[-1]["stdout"],
        "output_files": last["files"],
        "report": server.audit_report(manifest, last, {"provider": "builtin"}, attempts, summary),
    }
    if summary:
        rec["redteam_scenario"] = scenario
        rec["redteam_attempts"] = summary["attempts"]
        rec["redteam_outcome"] = summary["outcome"]
    if error:
        rec["error"] = error
    return rec


GROQ = {"provider": "groq", "model": "openai/gpt-oss-120b"}
VULTR = {"provider": "vultr", "model": "deepseek-v4.1-flash-1017"}
REPORT_JSON = [{"name": "report.json", "size": 221}]

SCENARIOS = {
    "success, no network (built-in)": lambda: make_run(
        "builtin", [result(stdout=ANALYSIS, files=REPORT_JSON, artifact={"labels": ["a"]})]
    ),
    "success on a retry": lambda: make_run(
        "model-written",
        [
            result("failed", stderr=TRACEBACK, elapsed=0.31, exit_code=1),
            result(stdout=ANALYSIS, elapsed=0.52, files=REPORT_JSON),
        ],
        GROQ,
    ),
    "three failures": lambda: make_run(
        "model-written",
        [result("failed", stderr=TRACEBACK, elapsed=0.3, exit_code=1) for _ in range(3)],
        VULTR,
    ),
    "time-limit kill": lambda: make_run(
        "builtin",
        [
            result(
                "timeout",
                stderr="Execution exceeded 10 seconds.",
                elapsed=10.04,
                exit_code=None,
                killed=True,
                lim=limits(seconds=10),
            )
        ],  # fmt: skip
    ),
    "memory kill": lambda: make_run(
        "builtin", [result("failed", elapsed=1.27, exit_code=137, lim=limits(memory=128))]
    ),
    "allowed host used plus blocked hosts": lambda: make_run(
        "model-written",
        [
            result(
                stdout="Fetched 3 rates.\n",
                elapsed=2.46,
                lim=limits(domains=["api.example.com"]),
                events=[
                    {
                        "host": "api.example.com",
                        "port": 443,
                        "allowed": True,
                        "reason": "allowlisted",
                    },
                    {
                        "host": "evil.example",
                        "port": 443,
                        "allowed": False,
                        "reason": "host not in allowlist",
                    },
                    {
                        "host": "evil.example",
                        "port": 443,
                        "allowed": False,
                        "reason": "host not in allowlist",
                    },
                    {
                        "host": "api.example.com",
                        "port": 80,
                        "allowed": False,
                        "reason": "method not allowed",
                    },
                    {
                        "host": "internal.example.com",
                        "port": 443,
                        "allowed": False,
                        "reason": "resolves to private address 10.0.0.5",
                    },
                ],
                files=[{"name": f"out{i}.csv", "size": 1536 * i} for i in range(1, 8)],
            )
        ],
        VULTR,
    ),  # fmt: skip
    "red team escape": lambda: make_run(
        "redteam",
        [
            result(
                stdout=ESCAPE_STDOUT,
                elapsed=1.84,
                lim=limits(128, 0.5, 20, ["example.com"]),
                events=[
                    {
                        "host": "example.org",
                        "port": 443,
                        "allowed": False,
                        "reason": "host not in allowlist",
                    },
                    {"host": "example.com", "port": 443, "allowed": True, "reason": "allowlisted"},
                ],
                files=[{"name": "redteam.txt", "size": 18}],
            )
        ],
        scenario="escape",
    ),  # fmt: skip
    "red team memory": lambda: make_run(
        "redteam",
        [
            result(
                "failed",
                stdout="allocated 16 MB\nallocated 32 MB\n",
                elapsed=0.85,
                exit_code=137,
                lim=limits(128, 0.5, 20, ["example.com"]),
            )
        ],
        scenario="memory",
    ),  # fmt: skip
    "red team runaway": lambda: make_run(
        "redteam",
        [
            result(
                "timeout",
                elapsed=8.16,
                exit_code=None,
                killed=True,
                lim=limits(128, 0.5, 8, ["example.com"]),
            )
        ],
        scenario="runaway",
    ),  # fmt: skip
}


def texts(points):
    return [p["text"] for p in points]


def assert_clean(points):
    assert points
    for point in points:
        assert point["level"] in LEVELS, point
        text = point["text"]
        for banned in BANNED:
            assert banned not in text, (banned, text)
        assert not KEY_PATTERN.search(text), text
        assert not NONE_WORD.search(text), text
        # The final answer may run to 600 characters; every sentence stays short.
        if point["level"] != "result":
            assert len(text) < 200, text
        else:
            assert len(text) <= report_text.MAX_RESULT_CHARS


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_scenario_is_plain_and_short(name):
    assert_clean(report_text.observations(SCENARIOS[name]()))


def test_success_no_network():
    points = report_text.observations(SCENARIOS["success, no network (built-in)"]())
    assert texts(points) == [
        "It finished in 0.5 seconds.",
        "It used Slipstream's built-in analysis program; no AI-written code ran.",
        "It was given 256 MB of memory, 1 CPU core and 30 seconds, and it used 0.5 seconds.",
        "It stayed inside the memory, CPU and time it was given.",
        "No internet access was given to this task.",
        "It wrote 1 file to its output folder: report.json (0.2 KB).",
        "Everything else was read-only.",
        "No passwords or keys were available to it.",
        "The sandbox was deleted afterwards.",
        ANALYSIS.strip(),
    ]
    assert [p["level"] for p in points][-1] == "result"


def test_success_on_retry_names_model_and_hides_the_traceback():
    joined = " ".join(texts(report_text.observations(SCENARIOS["success on a retry"]())))
    assert "It finished in 0.5 seconds on attempt 2." in joined
    assert "Groq (a development-only provider), model openai/gpt-oss-120b" in joined
    assert "attempt 2 succeeded" in joined
    assert "Traceback" not in joined and "FileNotFoundError" not in joined


def test_three_failures():
    points = report_text.observations(SCENARIOS["three failures"]())
    assert points[0] == {"level": "problem", "text": "It failed after 3 attempts."}
    assert "all 3 attempts failed" in " ".join(texts(points))
    assert points[-1]["level"] != "result"  # a failed run has no answer to show


def test_time_limit_kill():
    points = report_text.observations(SCENARIOS["time-limit kill"]())
    assert points[0] == {
        "level": "problem",
        "text": "It was stopped because it ran past its time limit of 10 seconds.",
    }
    # Said once, in the first sentence; not repeated, and no "stayed inside" claim.
    assert sum("time limit" in t for t in texts(points)) == 1
    assert "It was stopped when it reached its time limit." not in texts(points)
    assert "It stayed inside the memory, CPU and time it was given." not in texts(points)
    assert "This was the expected result of the test." not in texts(points)  # not a red team run


def test_memory_kill():
    points = report_text.observations(SCENARIOS["memory kill"]())
    assert points[0]["text"] == "It was stopped because it ran out of memory (it was given 128 MB)."
    assert not any("memory cap" in t for t in texts(points))
    assert "It stayed inside the memory, CPU and time it was given." not in texts(points)
    assert "This was the expected result of the test." not in texts(points)


def test_allowed_and_blocked_hosts():
    points = report_text.observations(SCENARIOS["allowed host used plus blocked hosts"]())
    blocked = [p["text"] for p in points if p["level"] == "blocked"]
    assert blocked == [
        "It tried to reach evil.example (2 times): blocked, "
        "because that website was not on the list.",
        "It tried to reach api.example.com: blocked, because it was not a secure connection.",
        "It tried to reach internal.example.com: blocked, because that address is not allowed "
        "(it points to a private or internal network).",
    ]
    all_text = texts(points)
    assert "Only these websites were allowed: api.example.com." in all_text
    assert "It used: api.example.com." in all_text
    files = next(t for t in all_text if t.startswith("It wrote"))
    assert files.startswith("It wrote 7 files to its output folder: out1.csv (1.5 KB)")
    assert files.endswith("out5.csv (7.5 KB) and 2 more.")


def test_builtin_mode():
    points = report_text.observations(SCENARIOS["success, no network (built-in)"]())
    assert {"level": "note", "text": "It used Slipstream's built-in analysis program; "
            "no AI-written code ran."} in points  # fmt: skip


def test_redteam_escape():
    points = report_text.observations(SCENARIOS["red team escape"]())
    all_text = texts(points)
    assert "It ran Slipstream's fixed red team test script; no AI was involved." in all_text
    assert (
        "It tried to send data to example.org: blocked, because that website was not on the list."
        in all_text
    )
    assert {
        "level": "ok",
        "text": "It looked for passwords or keys: none were available, so nothing could be taken.",
    } in points
    # The scorecard covers the example.org block; the internet section does not repeat it.
    assert sum("example.org" in t for t in all_text) == 1
    assert not any(t.startswith("It tried to reach example.org") for t in all_text)
    assert "It tried to save a file in its output folder: allowed, as expected." in all_text
    assert "It wrote 1 file to its output folder: redteam.txt (under 0.1 KB)." in all_text
    assert not any(p["level"] == "result" for p in points)  # raw ATTEMPT lines are not repeated
    assert not any(p["level"] == "problem" for p in points)


def test_redteam_memory_and_runaway_are_contained():
    memory = report_text.observations(SCENARIOS["red team memory"]())
    assert memory[0] == {
        "level": "ok",
        "text": "It was stopped because it ran out of memory (it was given 128 MB).",
    }
    expected = {"level": "ok", "text": "This was the expected result of the test."}
    assert memory[1] == expected
    runaway = report_text.observations(SCENARIOS["red team runaway"]())
    assert runaway[0] == {
        "level": "ok",
        "text": "It was stopped because it ran past its time limit of 8 seconds.",
    }
    assert runaway[1] == expected
    assert (
        "It was given 128 MB of memory, 0.5 of a CPU core and 8 seconds, and it used 8.2 seconds."
        in texts(runaway)
    )
    for points in (memory, runaway):
        assert not any("memory cap" in t or "reached its time limit" in t for t in texts(points))


def test_expected_result_only_when_the_scenario_matches_the_stop():
    # A memory test stopped by the time limit, or with no scenario recorded, claims nothing.
    run = SCENARIOS["red team runaway"]()
    run["redteam_scenario"] = "memory"
    assert "This was the expected result of the test." not in texts(report_text.observations(run))
    del run["redteam_scenario"]
    assert "This was the expected result of the test." not in texts(report_text.observations(run))


def test_normal_runs_keep_internet_blocked_sentences():
    # The same example.org block as the red team escape, in a normal run, stays in.
    run = SCENARIOS["red team escape"]()
    run["mode"] = "model-written"
    points = report_text.observations(run)
    assert {
        "level": "blocked",
        "text": "It tried to reach example.org: blocked, because that website was not on the list.",
    } in points
    assert not any("send data" in t for t in texts(points))


def test_redteam_unknown_and_missing_steps():
    run = SCENARIOS["red team escape"]()
    run["redteam_attempts"] = [
        {"name": "new_probe", "result": "BLOCKED", "as_expected": None},
        {"name": "read_secrets", "result": "MISSING", "as_expected": False},
        {
            "name": "exfil_unlisted_host",
            "result": "OK",
            "expected": "BLOCKED",
            "as_expected": False,
        },
    ]
    points = report_text.observations(run)
    assert {"level": "blocked", "text": "It tried the step 'new probe': blocked."} in points
    assert {
        "level": "problem",
        "text": "The test step 'read secrets' did not report a result.",
    } in points
    assert {"level": "problem", "text": "It tried to send data to example.org: "
            "it got through, which should not happen."} in points  # fmt: skip


@pytest.mark.parametrize(
    "run",
    [
        {},
        {"state": "failed"},
        {"attempts": [{}]},
        {"attempts": [None, "x"], "report": "not a dict"},
        {
            "mode": "model-written",
            "attempts": [{"status": "completed", "limits": {"memory_mb": None}}],
        },
        {
            "attempts": [{"status": "completed", "elapsed_seconds": "fast"}],
            "report": {"network": {}},
        },
        {"mode": "redteam", "attempts": [{"status": "completed"}], "redteam_attempts": [{}, None]},
    ],
)
def test_missing_fields_skip_sentences(run):
    points = report_text.observations(run)
    for point in points:
        assert not NONE_WORD.search(point["text"]), point
    assert all(p["level"] in LEVELS for p in points)


def test_missing_fields_examples():
    assert report_text.observations({}) == []
    assert texts(report_text.observations({"state": "failed"})) == [
        "It did not run, because the sandbox could not be started."
    ]
    # Completed without a time: no number is invented.
    assert texts(report_text.observations({"attempts": [{"status": "completed"}]})) == [
        "It finished.",
        "It stayed inside the memory, CPU and time it was given.",
    ]


def test_record_and_audit_get_report_points(monkeypatch):
    # The server loads the local .env; no provider means no model call in this test.
    for var in ("VULTR_INFERENCE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "REQUIRE_VULTR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        server, "execute_csv", lambda *a, **k: result(stdout=ANALYSIS, files=REPORT_JSON)
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        conn = HTTPConnection("127.0.0.1", httpd.server_port)
        conn.request(method, path, body=body and json.dumps(body))
        resp = conn.getresponse()
        return json.loads(resp.read())

    try:
        plan = call("POST", "/api/plan", {"task": "Top products"})
        call("POST", "/api/approve", {"id": plan["id"]})
        for _ in range(200):
            record = call("GET", f"/api/runs/{plan['id']}")
            if record["state"] != "running":
                break
            time.sleep(0.02)
    finally:
        httpd.shutdown()

    assert record["report_points"] == record["report"]["report_points"]
    assert record["report_points"][0]["text"] == "It finished in 0.5 seconds."
    # The technical audit is still all there.
    for key in ("allowed", "network", "used", "blocked_attempts", "cleanup", "planner", "attempts"):
        assert key in record["report"]


if __name__ == "__main__":
    for title, build in SCENARIOS.items():
        print(f"\n## {title}")
        for point in report_text.observations(build()):
            print(f"  [{point['level']:7}] {point['text']}")
