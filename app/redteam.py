"""Red team self-test: fixed manifests and canned scripts that probe the sandbox's limits."""

from __future__ import annotations

import re

NETWORK_DOMAINS = ["example.com"]

ESCAPE = r"""# CANNED, HARMLESS SELF-TEST OF THE SANDBOX, shipped with Slipstream (app/redteam.py).
# Not model-written. Each attempt tries one thing the sandbox should refuse (or, for
# write_output_folder and allowed_host, allow), prints one ATTEMPT line and moves on.
# It sends no data anywhere, never reads an environment value, and exits 0.
import os
import socket
import urllib.request


def report(name, result, detail):
    print(f"ATTEMPT {name} | {result} | {detail}"[:400], flush=True)


def describe(exc):
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]


# a. HTTPS to a host that is not on the allowlist (urllib uses the proxy env vars).
try:
    with urllib.request.urlopen("https://example.org/", timeout=5) as resp:
        report("exfil_unlisted_host", "OK", f"HTTP {resp.status} from example.org")
except Exception as exc:
    report("exfil_unlisted_host", "BLOCKED", describe(exc))

# b. Raw socket straight to an IP, ignoring the proxy.
try:
    with socket.create_connection(("1.1.1.1", 443), timeout=3):
        report("direct_socket_bypass", "OK", "connected to 1.1.1.1:443 without the proxy")
except Exception as exc:
    report("direct_socket_bypass", "BLOCKED", describe(exc))

# c. Writes outside the output folder, then one inside it.
try:
    refused, written = [], []
    for path in ("/etc/redteam.txt", "/app/x", "/workspace/input/redteam.txt"):
        try:
            with open(path, "w") as fh:
                fh.write("redteam\n")
            written.append(path)
        except OSError as exc:
            refused.append(f"{path}: {exc.strerror or type(exc).__name__}")
    if written:
        report("write_outside_output", "OK", "wrote " + ", ".join(written))
    else:
        report("write_outside_output", "BLOCKED", "; ".join(refused))
except Exception as exc:
    report("write_outside_output", "BLOCKED", describe(exc))
try:
    with open("/workspace/output/redteam.txt", "w") as fh:
        fh.write("redteam self-test\n")
    report("write_output_folder", "OK", "wrote /workspace/output/redteam.txt")
except Exception as exc:
    report("write_output_folder", "BLOCKED", describe(exc))

# d. Look for credentials in the environment: names only, values are never read or printed.
# GPG_KEY is set by the python base image to its public release-signing fingerprint; it is
# not a credential, so it is named here and not counted.
try:
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
    public = ("GPG_KEY",)
    count = sum(
        1
        for name in os.environ
        if name not in public and any(m in name.upper() for m in markers)
    )
    report(
        "read_secrets",
        "OK" if count else "BLOCKED",
        f"{count} secret-like variable name(s) present; no values read"
        " (base image GPG_KEY, a public fingerprint, not counted)",
    )
except Exception as exc:
    report("read_secrets", "BLOCKED", describe(exc))

# e. HTTPS to the allowlisted host: the allowlist is not just deny-all.
try:
    with urllib.request.urlopen("https://example.com/", timeout=10) as resp:
        report("allowed_host", "OK", f"HTTP {resp.status} from example.com via the proxy")
except Exception as exc:
    report("allowed_host", "BLOCKED", describe(exc))
"""

MEMORY = r"""# CANNED, HARMLESS SELF-TEST OF THE SANDBOX, shipped with Slipstream (app/redteam.py).
# Not model-written. Allocates memory in 16 MB chunks until the container's memory limit
# kills it. Nothing is written or sent anywhere.
CHUNK = 16 * 1024 * 1024
chunks = []
while True:
    chunks.append(b"\x01" * CHUNK)  # filled, so the pages are really committed
    print(f"allocated {len(chunks) * 16} MB", flush=True)
"""

RUNAWAY = r"""# CANNED, HARMLESS SELF-TEST OF THE SANDBOX, shipped with Slipstream (app/redteam.py).
# Not model-written. A CPU loop that never ends, so the run's time limit has to kill it.
print("starting a CPU loop that never ends", flush=True)
n = 0
while True:
    n += 1
"""

SCENARIOS = {
    "escape": {
        "summary": "Red team self-test: try to escape the sandbox (network, files, secrets).",
        "memory_mb": 128,
        "cpu_cores": 0.5,
        "time_seconds": 20,
        "script": ESCAPE,
    },
    "memory": {
        "summary": "Red team self-test: allocate memory until the memory limit stops it.",
        "memory_mb": 128,
        "cpu_cores": 0.5,
        "time_seconds": 20,
        "script": MEMORY,
    },
    "runaway": {
        "summary": "Red team self-test: run a CPU loop until the time limit stops it.",
        "memory_mb": 128,
        "cpu_cores": 0.5,
        "time_seconds": 8,
        "script": RUNAWAY,
    },
}

# What a contained sandbox should do with each escape attempt.
EXPECTED = {
    "exfil_unlisted_host": "BLOCKED",
    "direct_socket_bypass": "BLOCKED",
    "write_outside_output": "BLOCKED",
    "write_output_folder": "OK",
    "read_secrets": "BLOCKED",
    "allowed_host": "OK",
}

ATTEMPT_LINE = re.compile(r"^ATTEMPT ([a-z_]{1,40}) \| (BLOCKED|OK) \| (.*)$")
MAX_ATTEMPTS = 20


def draft(scenario: str) -> dict:
    """The fixed planner draft for a scenario; app.policy still builds the manifest."""
    spec = SCENARIOS[scenario]
    return {
        "summary": spec["summary"],
        "network_domains": list(NETWORK_DOMAINS),
        "memory_mb": spec["memory_mb"],
        "cpu_cores": spec["cpu_cores"],
        "time_seconds": spec["time_seconds"],
        "secrets": [],
    }


def parse_attempts(stdout: str) -> list[dict]:
    """ATTEMPT lines from stdout as {name, result, detail}; other lines are ignored."""
    attempts, seen = [], set()
    for line in (stdout or "").splitlines():
        match = ATTEMPT_LINE.match(line.strip())
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        name, result, detail = match.groups()
        attempts.append({"name": name, "result": result, "detail": detail.strip()[:300]})
        if len(attempts) >= MAX_ATTEMPTS:
            break
    return attempts


def summarize(scenario: str, result: dict) -> dict:
    """The scorecard and how the run ended, for the run record and audit."""
    attempts = parse_attempts(result.get("stdout") or "")
    if scenario == "escape":
        found = {a["name"] for a in attempts}
        attempts += [
            {"name": name, "result": "MISSING", "detail": "no ATTEMPT line in the output"}
            for name in EXPECTED
            if name not in found
        ]
        for attempt in attempts:
            attempt["expected"] = EXPECTED.get(attempt["name"])
            attempt["as_expected"] = attempt["result"] == attempt["expected"]
        outcome = None
    elif result.get("killed"):
        outcome = f"Killed at time limit after {result.get('elapsed_seconds')} seconds"
    elif result.get("exit_code") == 137:
        outcome = "Killed at memory limit"
    else:
        outcome = f"Not killed by a limit (exit code {result.get('exit_code')})"
    return {"scenario": scenario, "attempts": attempts, "outcome": outcome}
