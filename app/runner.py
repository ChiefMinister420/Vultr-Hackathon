"""Ephemeral, bounded Docker execution for the CSV demo."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.getenv("SLIPSTREAM_RUNNER_IMAGE", "slipstream-runner:local")


def docker_ready() -> bool:
    return (
        bool(shutil.which("docker"))
        and subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        ).returncode
        == 0
    )


def execute_csv(task: str, manifest: dict) -> dict:
    """Execute a constrained CSV job in a disposable container."""
    if not docker_ready():
        if os.getenv("SLIPSTREAM_ALLOW_UNSANDBOXED", "false").lower() != "true":
            raise RuntimeError(
                "Docker is unavailable. No code was run; start Docker and build the runner image."
            )
        return _local_dev_execute(task)

    with tempfile.TemporaryDirectory(prefix="slipstream-") as td:
        base = Path(td)
        inp, out = base / "input", base / "output"
        inp.mkdir()
        out.mkdir()
        # The runner has a non-root UID. This temporary host directory contains
        # only this run's output, so grant that UID write access through the bind mount.
        out.chmod(0o777)
        sample = ROOT / "examples" / "sales.csv"
        shutil.copyfile(sample, inp / "sales.csv")

        inp.chmod(0o755)
        (inp / "sales.csv").chmod(0o644)
        out.chmod(0o777)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"slipstream-{int(time.time())}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "-v",
            f"{inp}:/workspace/input:ro,Z",
            "-v",
            f"{out}:/workspace/output:rw,Z",
        ]
        runtime = os.getenv("SANDBOX_RUNTIME", "").strip()
        if runtime:
            cmd[2:2] = ["--runtime", runtime]
        cmd += [IMAGE, "--task", task[:2000]]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=min(int(manifest["time_seconds"]), 60),
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", cmd[cmd.index("--name") + 1]], capture_output=True)
            return {
                "status": "timeout",
                "stdout": "",
                "stderr": "Execution exceeded 60 seconds.",
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "artifact": None,
            }
        artifact = None
        report = out / "report.json"
        if report.exists():
            artifact = json.loads(report.read_text())
        return {
            "status": "completed" if proc.returncode == 0 else "failed",
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-3000:],
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "artifact": artifact,
            "engine": "Docker" + (f" / {runtime}" if runtime else ""),
        }


def _local_dev_execute(task: str) -> dict:
    # Explicitly opt-in development convenience, never represented as an isolated run.
    with tempfile.TemporaryDirectory(prefix="slipstream-dev-") as td:
        base = Path(td)
        inp = base / "sales.csv"
        out = base / "report.json"
        shutil.copyfile(ROOT / "examples" / "sales.csv", inp)
        env = os.environ | {"SLIPSTREAM_INPUT": str(inp), "SLIPSTREAM_OUTPUT": str(out)}
        proc = subprocess.run(
            ["python3", str(ROOT / "sandbox" / "analyze.py"), "--task", task[:2000]],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        artifact = json.loads(out.read_text()) if out.exists() else None
    return {
        "status": "completed" if proc.returncode == 0 else "failed",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_seconds": 0,
        "artifact": artifact,
        "engine": "UNSANDBOXED DEV MODE",
    }
