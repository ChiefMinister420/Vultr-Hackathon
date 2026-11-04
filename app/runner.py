"""Ephemeral, bounded Docker execution for the CSV demo."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.getenv("SLIPSTREAM_RUNNER_IMAGE", "slipstream-runner:local")
PROXY_IMAGE = os.getenv("SLIPSTREAM_PROXY_IMAGE", "slipstream-proxy:local")


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


def image_present(image: str = IMAGE) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
        == 0
    )


def execute_csv(task: str, manifest: dict, script: str | None = None) -> dict:
    """Execute a constrained CSV job (built-in, or a model-written script) in a container."""
    if not docker_ready():
        if script is not None:
            raise RuntimeError(
                "Docker is unavailable. Model-written scripts only run in the Docker sandbox."
            )
        if os.getenv("SLIPSTREAM_ALLOW_UNSANDBOXED", "false").lower() != "true":
            raise RuntimeError(
                "Docker is unavailable. No code was run; start Docker and build the runner image."
            )
        return _local_dev_execute(task)

    # Fail fast so a missing image is never pulled inside the run's time limit.
    if not image_present():
        raise RuntimeError(
            f"Runner image {IMAGE} not found. No code was run; build it with: "
            f"docker build -t {IMAGE} -f sandbox/Dockerfile ."
        )

    domains = list(manifest.get("network_domains") or [])
    if domains and not image_present(PROXY_IMAGE):
        raise RuntimeError(
            f"Proxy image {PROXY_IMAGE} not found. No code was run; build it with: "
            f"docker build -t {PROXY_IMAGE} -f proxy/Dockerfile ."
        )

    # The limits passed to Docker; the audit report reads this same dict.
    limits = {
        "memory_mb": int(manifest["memory_mb"]),
        "cpu_cores": float(manifest["cpu_cores"]),
        "time_seconds": min(int(manifest["time_seconds"]), 60),
        "network_domains": domains,
    }
    # One id names the runner, its proxy and its network.
    run_id = uuid.uuid4().hex[:12]
    name = f"slipstream-{run_id}"
    runtime = os.getenv("SANDBOX_RUNTIME", "").strip()
    engine = "Docker" + (f" / {runtime}" if runtime else "")

    # A script can create files the host user cannot delete; never fail a finished run on that.
    with tempfile.TemporaryDirectory(prefix="slipstream-", ignore_cleanup_errors=True) as td:
        base = Path(td)
        inp, out = base / "input", base / "output"
        inp.mkdir()
        out.mkdir()
        # The runner has a non-root UID. This temporary host directory contains
        # only this run's output, so grant that UID write access through the bind mount.
        out.chmod(0o777)
        sample = ROOT / "examples" / "sales.csv"
        shutil.copyfile(sample, inp / "sales.csv")

        if script is not None:
            # Delivered through the read-only input mount, never baked into the image.
            (inp / "task.py").write_text(script, encoding="utf-8")
            (inp / "task.py").chmod(0o644)

        inp.chmod(0o755)
        (inp / "sales.csv").chmod(0o644)
        out.chmod(0o777)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            f"sbx-{run_id}" if domains else "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            f"{limits['memory_mb']}m",
            "--memory-swap",
            f"{limits['memory_mb']}m",
            "--cpus",
            str(limits["cpu_cores"]),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "-v",
            f"{inp}:/workspace/input:ro,Z",
            "-v",
            f"{out}:/workspace/output:rw,Z",
        ]
        if runtime:
            cmd[2:2] = ["--runtime", runtime]
        if domains:
            proxy_url = f"http://proxy-{run_id}:3128"
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                cmd += ["-e", f"{var}={proxy_url}"]
        if script is not None:
            cmd += [IMAGE, "--script", "/workspace/input/task.py"]
        else:
            cmd += [IMAGE, "--task", task[:2000]]
        try:
            if domains:
                _start_proxy(run_id, domains)
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=limits["time_seconds"],
                )
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", name], capture_output=True)
                proc = None
            elapsed = round(time.monotonic() - started, 2)
        finally:
            net_events = _stop_proxy(run_id) if domains else []

        if proc is None:
            return {
                "status": "timeout",
                "stdout": "",
                "stderr": f"Execution exceeded {limits['time_seconds']} seconds.",
                "elapsed_seconds": elapsed,
                "artifact": None,
                "engine": engine,
                "killed": True,
                "limits": limits,
                "net_events": net_events,
                "container": name,
                "exit_code": None,
                "files": _output_files(out),
            }
        return {
            "status": "completed" if proc.returncode == 0 else "failed",
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-3000:],
            "elapsed_seconds": elapsed,
            "artifact": _chart_artifact(out / "report.json"),
            "engine": engine,
            "killed": False,
            "limits": limits,
            "net_events": net_events,
            "container": name,
            "exit_code": proc.returncode,
            "files": _output_files(out),
        }


def _chart_artifact(report: Path) -> dict | None:
    # Model-written scripts may write anything; only well-formed chart data is rendered.
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    labels, values = data.get("labels"), data.get("values")
    if not (isinstance(labels, list) and isinstance(values, list) and len(labels) == len(values)):
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in values):
        return None
    return data


def _output_files(out: Path) -> list[dict]:
    files = []
    try:
        for path in sorted(out.rglob("*")):
            if path.is_file() and not path.is_symlink() and len(files) < 100:
                files.append({"name": str(path.relative_to(out)), "size": path.stat().st_size})
    except OSError:
        pass
    return files


def _docker(*args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc


def _start_proxy(run_id: str, domains: list[str]) -> None:
    """Create the run's internal network and its allowlisting proxy, the only way out."""
    network, proxy = f"sbx-{run_id}", f"proxy-{run_id}"
    _docker("network", "create", "--internal", "--label", "slipstream=1", network)
    _docker(
        "run",
        "-d",
        "--name",
        proxy,
        "--network",
        network,
        "-e",
        f"ALLOW={','.join(domains)}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "64m",
        "--memory-swap",
        "64m",
        "--label",
        "slipstream=1",
        PROXY_IMAGE,
    )
    _docker("network", "connect", "bridge", proxy)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "READY" in _docker("logs", proxy).stdout:
            return
        time.sleep(0.2)
    raise RuntimeError("Network proxy did not report READY within 5 seconds. No code was run.")


def _stop_proxy(run_id: str) -> list[dict]:
    """Collect proxy logs, then remove the proxy and network. Never raises."""
    network, proxy = f"sbx-{run_id}", f"proxy-{run_id}"
    try:
        logs = subprocess.run(
            ["docker", "logs", proxy], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        logs = ""
    try:
        # The runner is normally gone already (--rm); this covers kills and crashes.
        subprocess.run(
            ["docker", "rm", "-f", proxy, f"slipstream-{run_id}"], capture_output=True, timeout=30
        )
        # A killed runner may still be detaching from the network; retry briefly.
        for _ in range(10):
            removed = subprocess.run(
                ["docker", "network", "rm", network], capture_output=True, timeout=30
            )
            if removed.returncode == 0:
                break
            time.sleep(0.3)
    except (OSError, subprocess.SubprocessError):
        pass
    return _parse_events(logs)


def _parse_events(logs: str) -> list[dict]:
    events = []
    for line in logs.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "allowed" in event:
            events.append(event)
    return events


def cleanup_stale() -> int:
    """Remove containers and networks labelled slipstream=1 left by an interrupted run."""
    if not docker_ready():
        return 0

    def ids(*args):
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
        return proc.stdout.split()

    containers = ids("ps", "-a", "--format", "{{.Names}}", "--filter", "label=slipstream=1")
    networks = ids("network", "ls", "-q", "--filter", "label=slipstream=1")
    for network in networks:
        # Runner containers carry no label; anything still attached to our network is ours.
        containers += ids(
            "network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", network
        )
    containers = list(dict.fromkeys(containers))
    if containers:
        subprocess.run(["docker", "rm", "-f", *containers], capture_output=True, timeout=60)
    if networks:
        subprocess.run(["docker", "network", "rm", *networks], capture_output=True, timeout=60)
    return len(containers) + len(networks)


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
