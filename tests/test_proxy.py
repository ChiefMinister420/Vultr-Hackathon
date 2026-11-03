import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "proxy", Path(__file__).resolve().parents[1] / "proxy" / "proxy.py"
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

ALLOW = frozenset({"example.com"})
PUBLIC = ["93.184.215.14"]


def decide(host="example.com", port=443, method="CONNECT", ips=PUBLIC):
    return proxy.decide(host, port, method, ALLOW, ips)


def test_allowed_host():
    assert decide() == (True, "allowlisted")


def test_unlisted_host():
    assert decide(host="example.org") == (False, "host not in allowlist")


@pytest.mark.parametrize("host", ["93.184.215.14", "[2606:2800:21f:cb07:6820:80da:af6b:8b2c]"])
def test_ip_literal(host):
    assert decide(host=host) == (False, "IP literal not allowed")


def test_port_other_than_443():
    assert decide(port=80) == (False, "port not allowed")


def test_non_connect_method():
    assert decide(method="GET") == (False, "method not allowed")


@pytest.mark.parametrize(
    "ip, kind",
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("169.254.169.254", "cloud metadata"),
        ("::ffff:127.0.0.1", "loopback"),
        ("100.64.0.1", "non-global"),
    ],
)
def test_resolves_to_internal_address(ip, kind):
    allowed, reason = decide(ips=PUBLIC + [ip])
    assert allowed is False
    assert reason == f"resolves to {kind} address {ip}"


def test_unresolved_host():
    assert decide(ips=[]) == (False, "host did not resolve")
