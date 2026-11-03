"""Allowlisting HTTPS CONNECT proxy for one Slipstream run. Standard library only."""

import asyncio
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlsplit

PORT = 3128
MAX_CONNECTIONS = 20
IDLE_SECONDS = 30
MAX_HEADER_BYTES = 8192
METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}

ALLOW = frozenset(h.strip().lower() for h in os.getenv("ALLOW", "").split(",") if h.strip())
SLOTS = asyncio.Semaphore(MAX_CONNECTIONS)


def decide(host, port, method, allow, resolved_ips):
    """Return (allowed, reason) for one request. Pure: the caller does DNS resolution."""
    if method != "CONNECT":
        return False, "method not allowed"
    if port != 443:
        return False, "port not allowed"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False, "IP literal not allowed"
    except ValueError:
        pass
    if host not in allow:
        return False, "host not in allowlist"
    if not resolved_ips:
        return False, "host did not resolve"
    for raw in resolved_ips:
        problem = _address_problem(ipaddress.ip_address(raw))
        if problem:
            return False, f"resolves to {problem} address {raw}"
    return True, "allowlisted"


def _address_problem(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in METADATA:
        return "cloud metadata"
    for kind in ("loopback", "private", "link_local", "multicast", "reserved", "unspecified"):
        if getattr(ip, f"is_{kind}"):
            return kind.replace("_", "-")
    if not ip.is_global:
        return "non-global"
    return None


def log(host, port, allowed, reason):
    line = {"t": round(time.time(), 3), "host": host, "port": port}
    print(json.dumps(line | {"allowed": allowed, "reason": reason}), flush=True)


async def reply(writer, status):
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()


async def resolve(host):
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=5
        )
    except (OSError, TimeoutError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


async def handle(reader, writer):
    if SLOTS.locked():
        log(None, None, False, "too many connections")
        await reply(writer, "503 Service Unavailable")
        writer.close()
        return
    async with SLOTS:
        try:
            await serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            writer.close()


async def serve(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_SECONDS)
    except (asyncio.LimitOverrunError, ValueError):
        log(None, None, False, "request header too large")
        return await reply(writer, "431 Request Header Fields Too Large")

    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(parts) != 3:
        log(None, None, False, "malformed request")
        return await reply(writer, "400 Bad Request")
    method, target = parts[0].upper(), parts[1]

    if method != "CONNECT":
        url = urlsplit(target)
        log(url.hostname or target[:200], url.port, False, "method not allowed")
        return await reply(writer, "405 Method Not Allowed")

    host, _, port_text = target.rpartition(":")
    host = host.lower()
    if not port_text.isdigit():
        log(target[:200], None, False, "malformed CONNECT target")
        return await reply(writer, "400 Bad Request")
    port = int(port_text)

    # Only allowlisted names are resolved, and the connection uses the checked addresses.
    ips = await resolve(host) if host in ALLOW and port == 443 else []
    allowed, reason = decide(host, port, method, ALLOW, ips)
    if not allowed:
        log(host, port, False, reason)
        return await reply(writer, "403 Forbidden")

    upstream = None
    for ip in ips:
        try:
            upstream = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=10)
            break
        except (OSError, TimeoutError):
            continue
    if upstream is None:
        log(host, port, False, "upstream connection failed")
        return await reply(writer, "502 Bad Gateway")

    log(host, port, True, reason)
    await reply(writer, "200 Connection Established")
    await pipe(reader, writer, *upstream)


async def pipe(client_reader, client_writer, up_reader, up_writer):
    last = [time.monotonic()]

    async def copy(src, dst):
        while data := await src.read(65536):
            last[0] = time.monotonic()
            dst.write(data)
            await dst.drain()

    tasks = [
        asyncio.create_task(copy(client_reader, up_writer)),
        asyncio.create_task(copy(up_reader, client_writer)),
    ]
    while not any(t.done() for t in tasks) and time.monotonic() - last[0] < IDLE_SECONDS:
        await asyncio.wait(tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    up_writer.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=MAX_HEADER_BYTES)
    print("READY", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
