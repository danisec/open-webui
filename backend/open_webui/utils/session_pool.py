"""Shared aiohttp ClientSession pool, with optional per-proxy sessions.

Instead of creating a new ClientSession (and connector) per request, callers
acquire a long-lived session from this module.  The pool manages one connector
per distinct proxy (plus one for direct traffic) with configurable limits,
enabling TCP/SSL connection reuse, shared DNS cache, and bounded concurrency.

All pool parameters are configurable via environment variables:
    - AIOHTTP_POOL_CONNECTIONS (default 100) — max total connections
    - AIOHTTP_POOL_CONNECTIONS_PER_HOST (default 30) — per-host limit
    - AIOHTTP_POOL_DNS_TTL (default 300) — DNS cache TTL in seconds

PROXIES
A connection may name its own proxy (Settings → Connections). aiohttp has no
native SOCKS support, so a SOCKS proxy needs ``aiohttp_socks.ProxyConnector``;
HTTP proxies use aiohttp's built-in ``proxy=`` support. Because a ClientSession
owns exactly one connector, and a connector owns its proxy, one shared session
*cannot* serve two connections with different proxies. Hence one session per
distinct proxy, cached and reused.

Callers that pass no proxy get the original shared session and behave exactly
as before — all existing call sites are unaffected.

Usage:
    from open_webui.utils.session_pool import get_session, cleanup_response

    session = await get_session()              # direct / environment proxy
    session = await get_session(proxy_url)     # per-connection proxy
    r = await session.request(...)
    # When done with the *response* (not the session):
    await cleanup_response(r)

IMPORTANT: Callers must NOT close the shared session.  Only the response
needs cleanup.  Sessions are closed once during application shutdown
via ``close_session()``.
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from open_webui.env import (
    AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_POOL_CONNECTIONS,
    AIOHTTP_POOL_CONNECTIONS_PER_HOST,
    AIOHTTP_POOL_DNS_TTL,
)
from open_webui.utils.misc import stream_chunks_handler
from open_webui.utils.proxy_config import is_socks, mask_proxy, normalize_proxy

log = logging.getLogger(__name__)

# Sessions keyed by normalised proxy URL; '' is the direct (no per-connection
# proxy) session, which keeps the original single-session behaviour.
_sessions: dict[str, aiohttp.ClientSession] = {}
_locks: dict[str, asyncio.Lock] = {}

_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
_CLIENT_STREAM_TIMEOUT = aiohttp.ClientTimeout(
    total=AIOHTTP_CLIENT_TIMEOUT,
    sock_read=AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT,
)


def get_client_timeout(stream: bool = False) -> aiohttp.ClientTimeout:
    return _CLIENT_STREAM_TIMEOUT if stream else _CLIENT_TIMEOUT


def _connector_limits() -> dict:
    limits = {}
    limits['limit'] = AIOHTTP_POOL_CONNECTIONS if AIOHTTP_POOL_CONNECTIONS is not None else 0
    limits['limit_per_host'] = AIOHTTP_POOL_CONNECTIONS_PER_HOST if AIOHTTP_POOL_CONNECTIONS_PER_HOST is not None else 0
    return limits


def _build_connector(proxy: str) -> aiohttp.BaseConnector:
    """Connector for a normalised proxy URL.

    SOCKS needs aiohttp_socks; HTTP(S) proxies are handled by aiohttp's own
    ``proxy=`` argument and therefore keep the plain TCPConnector.
    """
    limits = _connector_limits()

    if not proxy:
        return aiohttp.TCPConnector(
            ttl_dns_cache=AIOHTTP_POOL_DNS_TTL,
            enable_cleanup_closed=True,
            **limits,
        )

    if is_socks(proxy):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as error:  # pragma: no cover - dependency is pinned
            raise RuntimeError(
                'A SOCKS proxy is configured but aiohttp-socks is not installed. '
                'Install it (it is in requirements.txt) or use an http:// proxy.'
            ) from error
        return ProxyConnector.from_url(
            proxy,
            # Proxy-side DNS resolution is the point of a SOCKS proxy for
            # rate-limited upstreams: the request must appear to originate from
            # the proxy's address, not this host's.
            rdns=True,
            **limits,
        )

    # http:// or https:// proxy: aiohttp handles it per-request via proxy=.
    return aiohttp.TCPConnector(
        ttl_dns_cache=AIOHTTP_POOL_DNS_TTL,
        enable_cleanup_closed=True,
        **limits,
    )


async def get_session(proxy: str | None = None) -> aiohttp.ClientSession:
    """Return a shared ClientSession, optionally bound to a proxy.

    ``proxy`` is normalised first; an empty/None value returns the original
    direct session. Each distinct proxy gets its own cached session, because a
    session's connector fixes the proxy for its whole lifetime.
    """
    key = ''
    if proxy:
        key = normalize_proxy(proxy)

    session = _sessions.get(key)
    if session is not None and not session.closed:
        return session

    # Serialise creation per key so two concurrent first requests cannot leak a
    # second session (and its connector) for the same proxy.
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        session = _sessions.get(key)
        if session is not None and not session.closed:
            return session

        connector = _build_connector(key)
        # trust_env must be off when a per-connection proxy is set: otherwise
        # HTTPS_PROXY from the environment would also apply and the two would
        # silently conflict. For the direct session, keep the original
        # behaviour so deployments relying on env proxies are unaffected.
        _sessions[key] = aiohttp.ClientSession(
            connector=connector,
            timeout=get_client_timeout(),
            trust_env=not key,
        )
        log.info(
            'Created aiohttp session pool (proxy=%s, limit=%s, per_host=%s, dns_ttl=%d)',
            mask_proxy(key) or 'direct',
            AIOHTTP_POOL_CONNECTIONS or 'unlimited',
            AIOHTTP_POOL_CONNECTIONS_PER_HOST or 'unlimited',
            AIOHTTP_POOL_DNS_TTL,
        )
        return _sessions[key]


def session_uses_proxy(session: aiohttp.ClientSession) -> bool:
    """True when this session was created for a proxy.

    aiohttp needs ``proxy=`` per request for HTTP proxies but never for SOCKS
    (the connector already tunneled), so callers use this to decide.
    """
    for key, candidate in _sessions.items():
        if candidate is session:
            return bool(key) and not is_socks(key)
    return False


async def close_session():
    """Close every pooled session. Called during application shutdown."""
    global _sessions
    for key, session in list(_sessions.items()):
        if session and not session.closed:
            result = session.close()
            if result is not None:
                await result
            log.info('Closed aiohttp session pool (proxy=%s)', mask_proxy(key) or 'direct')
    _sessions = {}
    _locks.clear()


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession] = None,
):
    """Release and close an aiohttp response, optionally closing the session.

    When using the shared pool, ``session`` should be ``None`` (the pool
    session is never closed per-request).  When a caller creates its own
    one-off session, pass it here to close it after the response.
    """
    if response:
        if not response.closed:
            # aiohttp 3.9+ made ClientResponse.close() synchronous (returns None).
            # Older versions returned a coroutine.  Handle both gracefully.
            result = response.close()
            if result is not None:
                await result
    if session:
        if not session.closed:
            result = session.close()
            if result is not None:
                await result


async def stream_wrapper(response, session=None, passthrough=False):
    """Wrap a stream to ensure cleanup happens even if streaming is interrupted.

    This is more reliable than BackgroundTask which may not run if the client
    disconnects.  When using the shared pool, ``session`` should be ``None``.

    ``passthrough=True`` yields raw network chunks (iter_any) instead of
    lines: byte-identical output without a buffer scan, slice and copy per
    line. Only for streams no internal consumer parses line-by-line.
    """
    try:
        if passthrough:
            stream = response.content.iter_any()
        else:
            stream = stream_chunks_handler(response.content)
        async for chunk in stream:
            yield chunk
    finally:
        await cleanup_response(response, session)
