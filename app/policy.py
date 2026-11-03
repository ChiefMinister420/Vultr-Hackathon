"""Fixed capability policy: planner drafts and approval edits never widen a run."""

from __future__ import annotations

import ipaddress
import math
import re

MAX_MEM = 512
MAX_CPU = 1
MAX_TIME = 60
MIN_MEM = 64
MIN_CPU = 0.25
MIN_TIME = 5
MAX_DOMAINS = 5

FILES = [
    "/workspace/input/sales.csv:read",
    "/workspace/output/report.json:write",
]
DEFAULT_SUMMARY = "Analyze the bundled sales CSV and return top products by revenue."

# Dotted DNS name with an alphabetic TLD; single-label names are treated as internal.
HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*\.[a-z]{2,63}$")
INTERNAL_SUFFIXES = (
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".private",
    ".home.arpa",
)


def apply_policy(draft: dict) -> tuple[dict, list[str]]:
    """Build the manifest from a planner draft; return it with one note per dropped request."""
    domains, notes = _network_domains(draft.get("network_domains"))
    manifest = {
        "summary": str(draft.get("summary") or DEFAULT_SUMMARY)[:180],
        "network_domains": domains,
        "files": list(FILES),
        "memory_mb": min(MAX_MEM, max(MIN_MEM, _integer(draft.get("memory_mb"), 256))),
        "cpu_cores": min(MAX_CPU, max(MIN_CPU, _number(draft.get("cpu_cores"), 1))),
        "time_seconds": min(MAX_TIME, max(MIN_TIME, _integer(draft.get("time_seconds"), 30))),
        "secrets": [],
        "policy_notes": notes,
    }
    return manifest, notes


def tighten(issued: dict, edits: dict) -> dict:
    """Apply approval edits that only narrow the issued manifest; raise ValueError otherwise."""
    manifest = dict(issued)

    for key, floor, cast in (
        ("memory_mb", MIN_MEM, int),
        ("cpu_cores", MIN_CPU, float),
        ("time_seconds", MIN_TIME, int),
    ):
        value = edits.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, bool):
                raise TypeError
            value = cast(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None
        if not math.isfinite(value):
            raise ValueError(f"{key} must be a number.")
        if value > issued[key]:
            raise ValueError(
                f"{key} {value} exceeds the issued {issued[key]}; approval can only tighten."
            )
        if value < floor:
            raise ValueError(f"{key} must be at least {floor}.")
        manifest[key] = value

    hosts = edits.get("network_domains")
    if hosts is not None:
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("network_domains must be a list of hostnames.")
        hosts = {h.strip().lower() for h in hosts}
        extra = sorted(hosts - set(issued["network_domains"]))
        if extra:
            raise ValueError(
                f"network_domains {extra} were not in the issued manifest; "
                "approval can only tighten."
            )
        manifest["network_domains"] = [h for h in issued["network_domains"] if h in hosts]

    return manifest


def _network_domains(requested) -> tuple[list[str], list[str]]:
    if requested is None:
        return [], []
    if not isinstance(requested, list):
        return [], [f"Dropped network_domains {str(requested)[:80]!r}: not a list."]

    kept, notes = [], []
    for entry in requested:
        host = str(entry).strip().lower()
        reason = _rejection(host)
        if reason is None and host in kept:
            reason = "duplicate"
        if reason is None and len(kept) >= MAX_DOMAINS:
            reason = f"more than {MAX_DOMAINS} hosts"
        if reason:
            notes.append(f"Dropped network domain {host[:80]!r}: {reason}.")
        else:
            kept.append(host)
    return kept, notes


def _rejection(host: str) -> str | None:
    if "*" in host:
        return "wildcards are not allowed"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "IP literals are not allowed"
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return "localhost is not allowed"
    if "." in host and host.endswith(INTERNAL_SUFFIXES):
        return "internal names are not allowed"
    if len(host) > 253 or not HOSTNAME.match(host):
        if "." not in host and re.fullmatch(r"[a-z0-9-]+", host):
            return "internal names are not allowed"
        return "not a valid hostname"
    return None


def _integer(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
