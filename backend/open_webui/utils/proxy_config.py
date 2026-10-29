"""Per-connection proxy support for outbound OpenAI-compatible requests.

WHY THIS MODULE EXISTS
Open WebUI talks to providers through ``aiohttp``. aiohttp has no SOCKS support:
its only connectors are TCPConnector and UnixConnector, so a SOCKS5 proxy URL
passed through ``trust_env`` is spoken to as if it were an HTTP proxy. The
upstream then answers with a SOCKS greeting, which aiohttp reports as

    ClientResponseError: 400, "Bad status line ... b'\\x05\\xff'"

Measured in this deployment before writing this module. The fix is
``aiohttp_socks.ProxyConnector``, which performs the SOCKS handshake itself.

WHY ONE SESSION PER PROXY
A ``ClientSession`` owns its connector, and a connector owns its proxy settings.
One shared session therefore cannot serve two connections that use different
proxies. ``session_pool.get_session(proxy)`` keeps one session per distinct proxy
URL and reuses it, so connection reuse and DNS caching still work per proxy
instead of being lost entirely.

NORMALISATION MATTERS
Admins paste proxy URLs in whatever shape their provider documents. The common
slips are a doubled port (``host:1080:1080``), a missing scheme, and HTTP proxy
URLs pasted into a SOCKS field. Each is handled explicitly rather than failing
later with an opaque handshake error.

``socks5://`` resolves DNS at the proxy. That is deliberate: a per-IP rate limit
or abuse ban is keyed on the address that reaches the upstream, so resolving
locally would leak the origin address through the DNS query and defeat the point.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from open_webui.utils.api_key_rotation import select_key

log = logging.getLogger(__name__)

# Schemes we can actually speak. `socks5h` is an alias for proxy-side DNS
# resolution; aiohttp_socks treats both as SOCKS5 with remote resolution.
SOCKS_SCHEMES = {'socks5', 'socks5h', 'socks4', 'socks4a'}
HTTP_SCHEMES = {'http', 'https'}
SUPPORTED_SCHEMES = SOCKS_SCHEMES | HTTP_SCHEMES

# A URL with a doubled port at the end: scheme://...:1080:1080
_DOUBLE_PORT = re.compile(r'^(?P<head>.+):(?P<port1>\d{1,5}):(?P<port2>\d{1,5})/?$')


class ProxyConfigError(ValueError):
    """Raised for a proxy value an admin can fix. Message is shown in the UI."""


def normalize_proxy(raw: str | None) -> str:
    """Return a canonical proxy URL, or '' when no proxy is configured.

    Raises ProxyConfigError with an actionable message for values that cannot
    work, so ``/verify`` can report the reason instead of a handshake traceback.
    """
    if raw is None:
        return ''
    value = str(raw).strip()
    if not value:
        return ''

    # Tolerate a doubled port, e.g. "socks5://u:p@host:1080:1080". This shape
    # appeared in real configuration and would otherwise parse as a port of
    # "1080:1080" and fail obscurely.
    match = _DOUBLE_PORT.match(value)
    if match and match.group('port1') == match.group('port2'):
        value = match.group('head') + ':' + match.group('port1')
        log.warning('proxy URL had a doubled port; using %s', _mask(value))

    if '://' not in value:
        # Bare "host:port" or "user:pass@host:port" -- assume SOCKS5, which is
        # what this feature is for.
        value = 'socks5://' + value

    parts = urlsplit(value)
    scheme = (parts.scheme or '').lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyConfigError(
            f'Unsupported proxy scheme {scheme!r}. Use one of: ' + ', '.join(sorted(SUPPORTED_SCHEMES))
        )
    if not parts.hostname:
        raise ProxyConfigError('Proxy URL has no host.')

    try:
        port = parts.port
    except ValueError:
        raise ProxyConfigError('Proxy URL has an invalid port.')

    if port is None:
        port = 1080 if scheme in SOCKS_SCHEMES else 8080
    if not (0 < port < 65536):
        raise ProxyConfigError(f'Proxy port {port} is out of range.')

    # Re-encode credentials: an unencoded '@' or ':' in a password is the most
    # common cause of a proxy URL silently parsing to the wrong host.
    netloc = ''
    if parts.username is not None:
        netloc += quote(parts.username, safe='')
        if parts.password is not None:
            netloc += ':' + quote(parts.password, safe='')
        netloc += '@'
    netloc += parts.hostname
    netloc += f':{port}'

    return urlunsplit((scheme, netloc, '', '', ''))


def mask_proxy(url: str | None) -> str:
    """Proxy URL safe for logs: credentials removed, host and port kept."""
    return _mask(url or '')


def _mask(url: str) -> str:
    if not url:
        return ''
    try:
        parts = urlsplit(url)
    except Exception:
        return '<unparseable>'
    if not parts.hostname:
        return '<unparseable>'
    user = parts.username
    cred = f'{user}:***@' if user else ''
    # `parts.port` raises ValueError for a malformed port such as the doubled
    # "1080:1080" that admins actually paste. Masking is a logging helper and
    # must never raise, so fall back to the raw netloc without credentials.
    try:
        port = f':{parts.port}' if parts.port else ''
    except ValueError:
        port = ''
    return f'{parts.scheme}://{cred}{parts.hostname}{port}'


def is_socks(proxy: str | None) -> bool:
    if not proxy:
        return False
    try:
        return urlsplit(proxy).scheme.lower() in SOCKS_SCHEMES
    except Exception:
        return False


def request_proxy_kwargs(proxy: str | None) -> dict:
    """Extra aiohttp request kwargs needed for this proxy.

    SOCKS is handled entirely by the connector (the tunnel is established at
    connection time), so the request itself needs nothing. HTTP proxies are a
    per-request concern in aiohttp, so ``proxy=`` must be passed to each call.
    """
    if not proxy or is_socks(proxy):
        return {}
    return {'proxy': proxy}


def proxy_from_config(api_config: dict | None) -> str:
    """Extract and normalise the single proxy for one connection config.

    Never raises: a bad stored value must not take the whole connection down
    with an exception on every request. The problem surfaces through
    ``/verify`` instead, which is where an admin looks.
    """
    if not isinstance(api_config, dict):
        return ''
    raw = api_config.get('proxy')
    if not raw:
        return ''
    try:
        return normalize_proxy(raw)
    except ProxyConfigError as error:
        log.warning('ignoring invalid proxy config: %s', error)
        return ''


# ---------------------------------------------------------------------------
# Proxy rotation
# ---------------------------------------------------------------------------
# Several proxies can be attached to one connection, in `api_config['proxies']`.
# This exists because an upstream may rate-limit or ban by source address, and
# the only real remedy is to change the source address. Each proxy is a distinct
# egress path, so rotating them spreads load across addresses.
#
# Reuses the API-key rotation module rather than duplicating the cursor logic:
# round-robin and sticky have subtle state requirements (separate cursors per
# strategy, otherwise mixing them skips entries) that are already solved and
# unit-tested there. A separate cursor namespace keeps proxy selection
# independent of key selection.

_PROXY_STATE_NAMESPACE = 'proxy:'


def normalize_proxy_list(api_config: dict | None) -> list[str]:
    """Return the normalised, de-duplicated proxy list for a connection.

    Accepts a list in ``proxies``. Invalid entries are dropped individually
    rather than failing the whole connection, and duplicates are removed because
    two identical proxies would make rotation appear to do nothing.
    """
    if not isinstance(api_config, dict):
        return []

    raw = api_config.get('proxies')
    if isinstance(raw, str):
        # Tolerate a newline/comma separated string, which is how admins often
        # paste a list from a password manager.
        raw = [p for p in re.split(r'[\n,]+', raw) if p.strip()]
    if not isinstance(raw, list):
        raw = []

    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            normalised = normalize_proxy(item)
        except ProxyConfigError as error:
            log.warning('ignoring invalid proxy in list: %s', error)
            continue
        if normalised and normalised not in out:
            out.append(normalised)
    return out


def select_proxy(
    connection_key,
    proxies: list[str],
    strategy: str = 'round_robin',
    sticky_ttl: int = 300,
    now: float | None = None,
) -> str:
    """Pick one proxy for this request, or '' when none are configured.

    Thin wrapper over the API-key rotation logic, in its own state namespace so
    proxy and key rotation never share a cursor.
    """
    if not proxies:
        return ''
    if len(proxies) == 1:
        return proxies[0]
    return select_key(
        connection_key=f'{_PROXY_STATE_NAMESPACE}{connection_key}',
        keys=proxies,
        strategy=strategy,
        sticky_ttl=sticky_ttl,
        now=now,
    )


def proxy_strategy_of(api_config: dict | None) -> str:
    """Rotation strategy for proxies. Defaults to round_robin."""
    if not isinstance(api_config, dict):
        return 'round_robin'
    value = str(api_config.get('proxy_strategy') or 'round_robin').strip().lower()
    return value if value in ('round_robin', 'sticky') else 'round_robin'


def proxy_sticky_ttl_of(api_config: dict | None) -> int:
    """Sticky window for proxies, clamped so a typo cannot pin one for a week."""
    if not isinstance(api_config, dict):
        return 300
    try:
        value = int(api_config.get('proxy_sticky_ttl') or 300)
    except (TypeError, ValueError):
        return 300
    return max(0, min(value, 86400))


def resolve_proxies(api_config: dict | None, connection_key: Any = '') -> str:
    """The proxy to use for one request.

    A list in ``proxies`` wins over the single ``proxy`` field, and rotation is
    applied. Falls back to the legacy single proxy, then to '' (direct).

    This is the function every request path should call: it is the single point
    where both the list form and the legacy form are resolved.
    """
    proxies = normalize_proxy_list(api_config)
    if proxies:
        return select_proxy(
            connection_key=connection_key,
            proxies=proxies,
            strategy=proxy_strategy_of(api_config),
            sticky_ttl=proxy_sticky_ttl_of(api_config),
        )
    return proxy_from_config(api_config)


def describe_proxy(api_config: dict | None) -> dict:
    """Diagnostics for the settings UI and for /verify."""
    raw = (api_config or {}).get('proxy') or ''
    out = {
        'configured': bool(str(raw).strip()),
        'raw_masked': '',
        'normalized': '',
        'error': None,
        'rotation': None,
    }

    # A proxy list is the richer configuration; report it when present.
    proxies = normalize_proxy_list(api_config)
    if proxies:
        out['configured'] = True
        out['rotation'] = {
            'count': len(proxies),
            'strategy': proxy_strategy_of(api_config),
            'sticky_ttl': proxy_sticky_ttl_of(api_config),
            'proxies': [mask_proxy(p) for p in proxies],
        }
        out['normalized'] = mask_proxy(proxies[0])
        out['raw_masked'] = out['normalized']
        return out

    if not out['configured']:
        return out
    # Mask the raw value first: it is the only form available when the value is
    # invalid, and an admin needs to see what was stored to fix it. Masking never
    # raises (see _mask), so a malformed port cannot break the whole response.
    out['raw_masked'] = _mask(str(raw).strip())
    try:
        out['normalized'] = normalize_proxy(raw)
        out['raw_masked'] = mask_proxy(out['normalized'])
    except ProxyConfigError as error:
        out['error'] = str(error)
    return out
