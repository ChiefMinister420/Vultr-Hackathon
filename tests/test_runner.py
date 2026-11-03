import subprocess

import pytest

from app import runner
from app.server import audit_report

MANIFEST = {"memory_mb": 256, "cpu_cores": 0.5, "time_seconds": 20}


class FakeDocker:
    """Stands in for subprocess.run and records every docker invocation."""

    def __init__(self, image_present=True, timeout=False):
        self.image_present = image_present
        self.timeout = timeout
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0 if self.image_present else 1)
        if cmd[:2] == ["docker", "run"] and self.timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def runs(self):
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


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
