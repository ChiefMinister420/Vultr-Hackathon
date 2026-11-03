import json
import subprocess

import pytest

from app import runner
from app.server import audit_report

MANIFEST = {"memory_mb": 256, "cpu_cores": 0.5, "time_seconds": 20}


class FakeDocker:
    """Stands in for subprocess.run and records every docker invocation."""

    def __init__(self, image_present=True, timeout=False, fail=False, proxy_logs=""):
        self.image_present = image_present
        self.timeout = timeout
        self.fail = fail
        self.proxy_logs = proxy_logs
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0 if self.image_present else 1)
        if cmd[:2] == ["docker", "run"] and runner.IMAGE in cmd:
            if self.timeout:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            if self.fail:
                raise OSError("docker CLI crashed")
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="READY\n" + self.proxy_logs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def runs(self):
        return [c for c in self.calls if c[:2] == ["docker", "run"] and runner.IMAGE in c]

    def index(self, prefix):
        return next(i for i, c in enumerate(self.calls) if c[: len(prefix)] == prefix)


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        docker = FakeDocker(**kwargs)
        monkeypatch.setattr(runner, "docker_ready", lambda: True)
        monkeypatch.setattr(runner.subprocess, "run", docker)
        monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
        return docker

    return install


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_each_run_gets_a_unique_name(fake):
    docker = fake()
    runner.execute_csv("task", MANIFEST)
    runner.execute_csv("task", MANIFEST)
    first, second = (flag(c, "--name") for c in docker.runs())
    assert first != second
    assert first.startswith("slipstream-")


def test_docker_args_use_manifest_limits(fake):
    docker = fake()
    runner.execute_csv("task", MANIFEST)
    (cmd,) = docker.runs()
    assert flag(cmd, "--memory") == "256m"
    assert flag(cmd, "--memory-swap") == "256m"
    assert flag(cmd, "--cpus") == "0.5"


def test_audit_report_matches_passed_limits(fake):
    docker = fake()
    result = runner.execute_csv("task", MANIFEST)
    (cmd,) = docker.runs()
    used = audit_report(MANIFEST, result)["used"]
    assert f"{used['memory_limit_mb']}m" == flag(cmd, "--memory")
    assert str(used["cpu_limit_cores"]) == flag(cmd, "--cpus")


def test_timeout_kills_container_and_reports_real_limit(fake):
    docker = fake(timeout=True)
    result = runner.execute_csv("task", MANIFEST)
    (cmd,) = docker.runs()
    assert result["status"] == "timeout"
    assert result["killed"] is True
    assert result["engine"] == "Docker"
    assert result["stderr"] == "Execution exceeded 20 seconds."
    assert ["docker", "kill", flag(cmd, "--name")] in docker.calls
    assert "killed" in audit_report(MANIFEST, result)["cleanup"]


def test_missing_image_fails_before_docker_run(fake):
    docker = fake(image_present=False)
    with pytest.raises(RuntimeError, match="docker build"):
        runner.execute_csv("task", MANIFEST)
    assert docker.runs() == []


NET_MANIFEST = MANIFEST | {"network_domains": ["example.com"]}


def env_vars(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]


def test_empty_network_list_keeps_network_none(fake):
    docker = fake()
    result = runner.execute_csv("task", MANIFEST | {"network_domains": []})
    (cmd,) = docker.runs()
    assert flag(cmd, "--network") == "none"
    assert env_vars(cmd) == []
    assert [c[:3] for c in docker.calls] == [
        ["docker", "image", "inspect"],
        ["docker", "run", "--rm"],
    ]
    assert result["net_events"] == []
    assert audit_report(MANIFEST, result)["network"]["mode"].startswith("denied")


def test_network_run_uses_internal_network_and_proxy(fake):
    docker = fake()
    runner.execute_csv("task", NET_MANIFEST)
    (cmd,) = docker.runs()
    run_id = flag(cmd, "--name").removeprefix("slipstream-")
    network, proxy = f"sbx-{run_id}", f"proxy-{run_id}"

    assert ["docker", "network", "create", "--internal", "--label", "slipstream=1", network] in (
        docker.calls
    )
    proxy_run = docker.calls[docker.index(["docker", "run", "-d"])]
    assert flag(proxy_run, "--name") == proxy
    assert flag(proxy_run, "--network") == network
    assert "ALLOW=example.com" in env_vars(proxy_run)
    assert ["docker", "network", "connect", "bridge", proxy] in docker.calls
    assert docker.index(["docker", "run", "-d"]) < docker.calls.index(cmd)

    assert flag(cmd, "--network") == network
    url = f"http://{proxy}:3128"
    assert sorted(env_vars(cmd)) == sorted(
        f"{v}={url}" for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    )


def assert_cleaned_up(docker):
    run_id = flag(docker.calls[docker.index(["docker", "run", "-d"])], "--name").split("-")[1]
    assert ["docker", "rm", "-f", f"proxy-{run_id}", f"slipstream-{run_id}"] in docker.calls
    assert ["docker", "network", "rm", f"sbx-{run_id}"] in docker.calls


def test_network_cleanup_on_success(fake):
    docker = fake()
    runner.execute_csv("task", NET_MANIFEST)
    assert_cleaned_up(docker)


def test_network_cleanup_on_timeout(fake):
    docker = fake(timeout=True)
    result = runner.execute_csv("task", NET_MANIFEST)
    assert result["killed"] is True
    assert_cleaned_up(docker)


def test_network_cleanup_on_exception(fake):
    docker = fake(fail=True)
    with pytest.raises(OSError):
        runner.execute_csv("task", NET_MANIFEST)
    assert_cleaned_up(docker)


def test_audit_report_mixed_network_events(fake):
    events = [
        ("example.com", 443, True, "allowlisted"),
        ("example.com", 443, True, "allowlisted"),
        ("evil.test", 443, False, "host not in allowlist"),
        ("example.com", 80, False, "port not allowed"),
    ]
    logs = "\n".join(
        [
            json.dumps({"t": 1, "host": h, "port": p, "allowed": a, "reason": r})
            for h, p, a, r in events
        ]
        + ["not json"]
    )
    fake(proxy_logs=logs)
    result = runner.execute_csv("task", NET_MANIFEST)
    report = audit_report(NET_MANIFEST, result)
    assert report["network"]["allowed"] == ["example.com"]
    assert report["network"]["used"] == ["example.com"]
    assert report["network"]["blocked"] == [
        {"host": "evil.test", "port": 443, "reason": "host not in allowlist"},
        {"host": "example.com", "port": 80, "reason": "port not allowed"},
    ]
    assert report["blocked_attempts"] == report["network"]["blocked"]
