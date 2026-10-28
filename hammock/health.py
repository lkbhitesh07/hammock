"""Health probes: http(s)://… (any answer below 500 means up) and tcp://host:port."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

_TIMEOUT = 2.0


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"health URL has no host: {url}")
    default = {"http": 80, "https": 443}.get(parts.scheme)
    port = parts.port or default
    if port is None:
        raise ValueError(f"health URL needs a port: {url}")
    return parts.hostname, port


def validate_url(url: str) -> None:
    if urlsplit(url).scheme not in ("http", "https", "tcp"):
        raise ValueError(f"health URL must start with http://, https:// or tcp://: {url}")
    _host_port(url)


async def tcp_probe(url: str) -> bool:
    host, port = _host_port(url)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def http_probe(url: str, client: httpx.AsyncClient) -> bool:
    try:
        response = await client.get(url, timeout=_TIMEOUT)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


async def probe(url: str, client: httpx.AsyncClient, light: bool = False) -> bool:
    """Full check for the URL's scheme; `light` downgrades HTTP to a TCP connect (no access-log noise)."""
    if light or urlsplit(url).scheme == "tcp":
        return await tcp_probe(url)
    return await http_probe(url, client)
