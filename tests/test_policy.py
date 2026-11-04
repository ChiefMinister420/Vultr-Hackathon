import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app import server
from app.policy import MAX_DOMAINS, apply_policy


def domains(requested):
    manifest, notes = apply_policy({"network_domains": requested})
    assert manifest["policy_notes"] == notes
    return manifest["network_domains"], notes


@pytest.mark.parametrize("host", ["10.0.0.1", "169.254.169.254", "::1", "[::1]"])
def test_ip_literal_dropped(host):
    kept, notes = domains([host])
    assert kept == []
    assert "IP literal" in notes[0]


def test_wildcard_dropped():
    kept, notes = domains(["*.example.com"])
    assert kept == []
    assert "wildcard" in notes[0]


@pytest.mark.parametrize(
    "host", ["localhost", "db", "metadata.google.internal", "printer.local", "nas.lan"]
)
def test_localhost_and_internal_names_dropped(host):
    kept, notes = domains([host])
    assert kept == []
    assert len(notes) == 1


@pytest.mark.parametrize("host", ["https://example.com", "example.com:443", "exa mple.com", ""])
def test_invalid_hostname_dropped(host):
    kept, notes = domains([host])
    assert kept == []
    assert "not a valid hostname" in notes[0]


def test_valid_host_kept_normalized():
    kept, notes = domains(["  API.Example.COM "])
    assert kept == ["api.example.com"]
    assert notes == []


def test_duplicates_removed():
    kept, notes = domains(["example.com", "EXAMPLE.com"])
    assert kept == ["example.com"]
    assert "duplicate" in notes[0]


def test_capped_at_five():
    hosts = [f"h{i}.example.com" for i in range(MAX_DOMAINS + 2)]
    kept, notes = domains(hosts)
    assert kept == hosts[:MAX_DOMAINS]
    assert len(notes) == 2


def test_fallback_planner_has_no_network():
    manifest, notes = apply_policy({})
    assert manifest["network_domains"] == []
    assert notes == []


def test_secrets_and_files_fixed():
    manifest, _ = apply_policy(
        {"secrets": ["AWS_KEY"], "files": ["/etc/passwd:read"], "summary": "x" * 500}
    )
    assert manifest["secrets"] == []
    assert manifest["files"] == [
        "/workspace/input/sales.csv:read",
        "/workspace/output/report.json:write",
    ]
    assert len(manifest["summary"]) == 180


@pytest.mark.parametrize(
    "draft, expected",
    [
        ({"memory_mb": 1, "cpu_cores": 0.01, "time_seconds": 1}, (64, 0.25, 5)),
        ({"memory_mb": 99999, "cpu_cores": 64, "time_seconds": 9999}, (512, 1, 60)),
        ({}, (256, 1, 30)),
    ],
)
def test_resource_clamps(draft, expected):
    manifest, _ = apply_policy(draft)
    assert (manifest["memory_mb"], manifest["cpu_cores"], manifest["time_seconds"]) == expected


@pytest.fixture
def api(monkeypatch):
    def fake_execute(task, manifest, script=None):
        return {"status": "completed", "engine": "Docker", "elapsed_seconds": 0, "limits": {}}

    monkeypatch.setattr(server, "execute_csv", fake_execute)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def issue(**draft):
        manifest, _ = apply_policy(draft)
        rid = f"test-{len(server.RUNS)}"
        with server.LOCK:
            server.RUNS[rid] = {
                "id": rid,
                "task": "t",
                "manifest": manifest,
                "state": "awaiting_approval",
                "events": [],
            }
        return rid

    def approve(rid, **edits):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/approve",
            data=json.dumps({"id": rid, **edits}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    yield issue, approve
    httpd.shutdown()


@pytest.mark.parametrize(
    "edits",
    [
        {"memory_mb": 512},
        {"cpu_cores": 2},
        {"time_seconds": 31},
        {"network_domains": ["evil.example.net"]},
        {"time_seconds": 2},
        {"memory_mb": "lots"},
    ],
)
def test_wider_approval_rejected(api, edits):
    issue, approve = api
    rid = issue(network_domains=["api.example.com"], memory_mb=256, cpu_cores=0.5)
    status, body = approve(rid, **edits)
    assert status == 400
    assert body["error"]
    assert server.RUNS[rid]["state"] == "awaiting_approval"


def test_tighter_approval_accepted(api):
    issue, approve = api
    rid = issue(network_domains=["a.example.com", "b.example.com"], memory_mb=256, cpu_cores=0.5)
    status, _ = approve(
        rid,
        network_domains=["B.example.com"],
        memory_mb=128,
        cpu_cores=0.25,
        time_seconds=10,
    )
    assert status == 202
    manifest = server.RUNS[rid]["manifest"]
    assert manifest["network_domains"] == ["b.example.com"]
    assert (manifest["memory_mb"], manifest["cpu_cores"], manifest["time_seconds"]) == (
        128,
        0.25,
        10,
    )


def test_second_approval_still_409(api):
    issue, approve = api
    rid = issue()
    assert approve(rid)[0] == 202
    assert approve(rid)[0] == 409
