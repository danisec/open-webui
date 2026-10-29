"""Round-robin API key selection for an OpenAI-compatible connection.

Why this exists
---------------
One endpoint often hands out several API keys with the same quota model (9router
style). The upstream already load-balances across keys, but Open WebUI stores one
key per connection, so the extra keys were unusable: adding the same base URL
twice does not help either, because `get_merged_models` dedupes by model id with
`if model_id not in models` -- the first connection that advertises a model keeps
it and the duplicate never serves that model.

So the keys are stored on the connection itself, in
`api_config['api_keys']` (a list, in addition to the single legacy `api_key` in
the parallel `OPENAI_API_KEYS` array), and one is picked per request here.

Strategies
----------
``round_robin`` (default)
    Strict rotation, one key per request. Even distribution. Good when every key
    has the same quota and the upstream tolerates interleaving.

``sticky``
    The same key is reused until its TTL expires, then rotation resumes. This is
    what the user asked for ("Round Robin sticky=1"): within the window every
    request shares one key, which matters for upstream prompt caching, per-key
    rate-limit buckets, and provider-side session affinity. ``sticky_ttl`` is the
    window length in seconds (default 300).

State
-----
The counter and sticky deadline live in a process-global dict. That is deliberate
and it is also the honest limitation: with multiple worker processes each process
rotates independently, so the effective order is not globally serialized. For a
single-process deployment (this one) it is exact. A shared store would be needed
for strict global ordering across replicas; that is noted in the docs rather than
half-implemented here.

Failure behaviour: any malformed config falls back to the single legacy key, and
selection never raises. A broken key list must not take chat down.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# connection index (or url) -> {"next": int, "sticky_until": float, "sticky_idx": int}
_state: dict[str, dict[str, float]] = {}
_lock = threading.Lock()

STRATEGY_ROUND_ROBIN = 'round_robin'
STRATEGY_STICKY = 'sticky'
VALID_STRATEGIES = (STRATEGY_ROUND_ROBIN, STRATEGY_STICKY)

DEFAULT_STICKY_TTL = 300


def _state_key(connection_key: Any) -> str:
    return str(connection_key)


def normalize_key_list(value: Any) -> list[str]:
    """Coerce whatever the admin form stored into a clean list of keys.

    Accepts a list, a comma/newline separated string, or None. Blank entries are
    dropped and duplicates removed while preserving order, because a duplicated
    key would silently double that key's share of traffic.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace('\n', ',').split(',')
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def resolve_keys(api_config: dict | None, legacy_key: str | None) -> list[str]:
    """The keys available for one connection, best source first.

    `api_config['api_keys']` wins when non-empty; otherwise the single legacy key
    is used. The legacy key is *not* merged in, because an admin who moved to a
    list expects the list to be authoritative -- silently appending the old key
    would resurrect a key they had removed.
    """
    config = api_config or {}
    keys = normalize_key_list(config.get('api_keys'))
    if keys:
        return keys
    legacy = (legacy_key or '').strip()
    return [legacy] if legacy else []


def strategy_of(api_config: dict | None) -> str:
    config = api_config or {}
    strategy = config.get('api_key_strategy')
    if isinstance(strategy, str) and strategy in VALID_STRATEGIES:
        return strategy
    return STRATEGY_ROUND_ROBIN


def sticky_ttl_of(api_config: dict | None) -> int:
    config = api_config or {}
    raw = config.get('sticky_ttl', DEFAULT_STICKY_TTL)
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STICKY_TTL
    # 0 would mean "rotate every request", which is round_robin; keep it valid but
    # bounded so a typo cannot pin one key for a week.
    return max(0, min(ttl, 86400))


def select_key(
    connection_key: Any,
    keys: list[str],
    strategy: str = STRATEGY_ROUND_ROBIN,
    sticky_ttl: int = DEFAULT_STICKY_TTL,
    now: float | None = None,
) -> str:
    """Pick the key to use for this request. Never raises, never returns '' when
    `keys` is non-empty.

    `now` is injectable so tests do not depend on wall-clock time.
    """
    if not keys:
        return ''

    if len(keys) == 1:
        return keys[0]

    timestamp = time.time() if now is None else now
    state_key = _state_key(connection_key)

    with _lock:
        entry = _state.get(state_key)
        if entry is None:
            entry = {
                'next': 0,
                'sticky_next': 0,
                'sticky_until': 0.0,
                'sticky_idx': 0,
            }
            _state[state_key] = entry

        if strategy == STRATEGY_STICKY:
            # Reuse the pinned key until the window closes, then move on. The
            # sticky cursor is kept separate from the round-robin one: sharing it
            # would let a sticky request silently advance the round-robin order,
            # so switching strategies mid-life would skip keys.
            if timestamp < entry['sticky_until']:
                return keys[int(entry['sticky_idx']) % len(keys)]
            entry['sticky_idx'] = int(entry['sticky_next']) % len(keys)
            entry['sticky_next'] = (entry['sticky_idx'] + 1) % len(keys)
            entry['sticky_until'] = timestamp + max(0, sticky_ttl)
            return keys[int(entry['sticky_idx'])]

        index = int(entry['next']) % len(keys)
        entry['next'] = (index + 1) % len(keys)
        return keys[index]


def reset_state() -> None:
    """Clear rotation state. Used by tests and when connection config changes, so a
    removed key cannot linger as a stale index."""
    with _lock:
        _state.clear()


def describe(api_config: dict | None, legacy_key: str | None) -> dict:
    """Diagnostics for the UI/logs: how many keys, which strategy, next index."""
    keys = resolve_keys(api_config, legacy_key)
    return {
        'key_count': len(keys),
        'strategy': strategy_of(api_config),
        'sticky_ttl': sticky_ttl_of(api_config),
        'masked': [f'{k[:6]}...{k[-4:]}' if len(k) > 12 else '***' for k in keys],
    }
