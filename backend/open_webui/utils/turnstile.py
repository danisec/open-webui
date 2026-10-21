"""Cloudflare Turnstile verification for the sign-in gate.

The feature is opt-in and fails safe:
  * disabled unless ENABLE_TURNSTILE is true AND a secret key is configured;
  * placeholder keys ("CHANGE_ME", "0x...") are treated as "not configured",
    so a half-finished deployment can never lock everyone out.
"""

from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
TURNSTILE_TIMEOUT_SECONDS = 5

_PLACEHOLDER_MARKERS = ('change_me', 'your_', 'placeholder', 'example')


def _env(name: str, default: str = '') -> str:
    return (os.getenv(name, default) or '').strip()


def _looks_like_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def get_turnstile_site_key() -> str:
    return _env('TURNSTILE_SITE_KEY')


def get_turnstile_secret_key() -> str:
    return _env('TURNSTILE_SECRET_KEY')


def is_turnstile_enabled() -> bool:
    """Turnstile is enforced only when explicitly enabled and fully configured."""
    enabled = _env('ENABLE_TURNSTILE', 'false').lower() in ('1', 'true', 'yes', 'on')
    if not enabled:
        return False
    if _looks_like_placeholder(get_turnstile_site_key()):
        log.warning('ENABLE_TURNSTILE is set but TURNSTILE_SITE_KEY is missing or a placeholder')
        return False
    if _looks_like_placeholder(get_turnstile_secret_key()):
        log.warning('ENABLE_TURNSTILE is set but TURNSTILE_SECRET_KEY is missing or a placeholder')
        return False
    return True


def public_turnstile_config() -> dict:
    """Config handed to the frontend (never includes the secret)."""
    enabled = is_turnstile_enabled()
    return {
        'enabled': enabled,
        'site_key': get_turnstile_site_key() if enabled else '',
    }


async def verify_turnstile_token(token: str | None, remote_ip: str | None = None) -> bool:
    """Validate a Turnstile response token with Cloudflare.

    Returns True when the token is valid. Any transport error is reported as a
    failure so the caller can decide (fail-closed by default).
    """
    if not token or not isinstance(token, str):
        return False

    data = {
        'secret': get_turnstile_secret_key(),
        'response': token,
    }
    if remote_ip:
        data['remoteip'] = remote_ip

    try:
        timeout = aiohttp.ClientTimeout(total=TURNSTILE_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(TURNSTILE_VERIFY_URL, data=data) as response:
                if response.status != 200:
                    log.warning('Turnstile verify returned HTTP %s', response.status)
                    return False
                payload = await response.json(content_type=None)
    except Exception as exc:  # network/timeout/JSON problems
        log.warning('Turnstile verification failed: %s', exc)
        return False

    if not isinstance(payload, dict) or not payload.get('success'):
        codes = payload.get('error-codes') if isinstance(payload, dict) else None
        log.warning('Turnstile rejected the token: %s', codes)
        return False

    return True
