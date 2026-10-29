"""Admin API for the model-rotator alias configuration.

The rotator runs as a sidecar proxy and owns the alias -> candidate mapping.
Open WebUI proxies to it so admins can manage aliases from Settings, and keeps
the OpenAI connection's model list in sync with the aliases that exist.
"""

import logging
import os
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.internal.db import get_async_session
from open_webui.models.config import Config
from open_webui.models.models import ModelForm, ModelMeta, ModelParams, Models
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
log = logging.getLogger(__name__)

ROTATOR_ADMIN_TOKEN = os.environ.get('MODEL_ROTATOR_ADMIN_TOKEN', '')
ROTATOR_TIMEOUT = 30


class RotatorAliasCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field('', max_length=256)
    priority: list[str] = []


class RotatorAliasUpdate(BaseModel):
    name: str | None = Field(None, max_length=256)
    priority: list[str] | None = None
    # "priority" = strict order (first healthy wins), "round-robin" = weighted
    # rotation across candidates so load spreads instead of piling on the first.
    strategy: str | None = Field(None, pattern='^(priority|round-robin)$')
    # Per-candidate weight for round-robin. Missing entries default to 1.
    weights: dict[str, int] | None = None


class RotatorCandidate(BaseModel):
    id: str = Field(..., min_length=1, max_length=256)


class RotatorPriority(BaseModel):
    priority: list[str] = []


def _is_rotator_url(url: str) -> bool:
    return 'model-rotator' in url or url.rstrip('/').endswith(':8891')


async def _rotator_url() -> str:
    """Return the rotator base URL from the configured OpenAI connections.

    The OpenAI connection URL already carries the /v1 prefix, so strip it to
    keep the config API paths (which start with /v1) from doubling up.
    """
    urls = await Config.get('openai.api_base_urls', []) or []
    for url in urls:
        if _is_rotator_url(url):
            base = url.rstrip('/')
            if base.endswith('/v1'):
                base = base[: -len('/v1')]
            return base
    raise HTTPException(status_code=503, detail='Model rotator connection is not configured')


def _rotator_headers() -> dict:
    headers = {'Content-Type': 'application/json'}
    if ROTATOR_ADMIN_TOKEN:
        headers['Authorization'] = f'Bearer {ROTATOR_ADMIN_TOKEN}'
    return headers


async def _rotator_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f'{await _rotator_url()}{path}'
    timeout = aiohttp.ClientTimeout(total=ROTATOR_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.request(
                method,
                url,
                json=payload,
                headers=_rotator_headers(),
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                if response.status == 404:
                    return {}
                text = await response.text()
                if response.status >= 400:
                    detail = text
                    try:
                        detail = (await response.json(content_type=None)).get('error', text)
                    except Exception:
                        pass
                    raise HTTPException(status_code=response.status, detail=detail)
                if not text:
                    return {}
                try:
                    return await response.json(content_type=None)
                except Exception:
                    return {}
    except HTTPException:
        raise
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=502, detail=f'Cannot reach model rotator: {e}')
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Model rotator error: {e}')


async def _rotator_connection_index() -> int | None:
    """Index of the OpenAI connection that points at the rotator."""
    urls = await Config.get('openai.api_base_urls', []) or []
    for idx, url in enumerate(urls):
        if _is_rotator_url(url):
            return idx
    return None


async def _sync_rotator_model_ids(aliases: list[str]) -> None:
    """Keep the rotator connection's model list aligned with the alias list.

    The rotator connection may only serve aliases, so its model list is rebuilt
    from the current alias list. Ids that are not aliases (other connections'
    models) are left untouched.
    """
    index = await _rotator_connection_index()
    if index is None:
        return

    api_configs = await Config.get('openai.api_configs', {}) or {}
    key = str(index) if str(index) in api_configs else None
    if key is None:
        urls = await Config.get('openai.api_base_urls', []) or []
        for url, cfg in api_configs.items():
            if url in urls and _is_rotator_url(url):
                key = url
                break
    if key is None:
        return

    cfg = dict(api_configs.get(key) or {})
    current = list(cfg.get('model_ids') or [])

    # Ids this connection serves that are not aliases must survive the sync.
    previous_aliases = set(await Config.get('model_rotator.managed_aliases', []) or [])
    keep = [m for m in current if m not in previous_aliases]
    model_ids = [m for m in keep if m not in aliases]
    model_ids.extend(a for a in aliases if a not in model_ids)

    await Config.upsert({'model_rotator.managed_aliases': list(aliases)})

    if current == model_ids:
        return

    cfg['model_ids'] = model_ids
    api_configs[key] = cfg
    await Config.upsert({'openai.api_configs': api_configs})


def _upstream_slug(url: str, idx: int) -> str:
    """Stable, collision-free upstream name for one connection URL.

    Only the first DNS label is not enough: api.howtofix.id and api.sakoo.biz.id
    both reduce to "api", and because upstreams are stored in a dict keyed by name,
    whichever connection was registered last silently owned the other's candidates.
    That made `@api/<model>` ambiguous and pointed an alias at an upstream the admin
    never chose.

    Uses enough of the host to keep distinct providers distinct:
        api.howtofix.id  -> api-howtofix
        api.sakoo.biz.id -> api-sakoo
        9router-lb       -> 9router-lb
        100.100.89.87    -> 100-100-89-87
        localhost        -> localhost
    """
    host = (urlparse(url).hostname or '').strip().lower()
    if not host:
        return f'conn{idx}'

    labels = host.split('.')
    is_ipv4 = len(labels) == 4 and all(part.isdigit() for part in labels)

    if is_ipv4:
        # Keep the dots as separators so the address stays readable.
        slug = ''.join(c for c in host if c.isalnum() or c in '-_')
        return slug or f'conn{idx}'

    if len(labels) >= 3:
        # api.howtofix.id -> "api-howtofix"; api.sakoo.biz.id -> "sakoo-biz".
        # The label before the suffix plus the one before that keeps distinct
        # providers distinct even when they share a subdomain such as "api".
        label = '-'.join(labels[-3:-1])
    else:
        # A bare host (single label) or a two-label host is already distinctive.
        label = labels[0]

    slug = ''.join(c for c in label if c.isalnum() or c in '-_')
    return slug or f'conn{idx}'


async def _connection_catalog(url: str, key: str) -> set[str]:
    """Model ids advertised by one OpenAI-compatible connection."""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f'{url.rstrip("/")}/models',
                headers={'Authorization': f'Bearer {key}'},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                if response.status != 200:
                    return set()
                data = await response.json(content_type=None)
        return {m.get('id') for m in (data.get('data') or []) if m.get('id')}
    except Exception:
        return set()


async def _resolve_candidate_source(model_id: str) -> tuple[str | None, str]:
    """Return (upstream_name, model_id).

    upstream_name is None when the model lives on the rotator's default
    upstream, which keeps the candidate id unchanged for backward compat.
    """
    urls = await Config.get('openai.api_base_urls', []) or []
    keys = await Config.get('openai.api_keys', []) or []

    matches = []
    default_idx = None
    for idx, url in enumerate(urls):
        if _is_rotator_url(url):
            continue
        if default_idx is None:
            default_idx = idx
        key = keys[idx] if idx < len(keys) else ''
        if model_id in await _connection_catalog(url, key):
            matches.append((idx, url, key))

    if not matches:
        return None, model_id
    for idx, _url, _key in matches:
        if idx == default_idx:
            return None, model_id

    idx, url, key = matches[0]
    name = _upstream_slug(url, idx)
    await _rotator_request('PUT', f'/v1/config/upstreams/{name}', {'url': url, 'key': key})
    return name, model_id


async def _sync_upstream_keys() -> None:
    """Refresh stored keys from the live OpenAI connections (keys can rotate)."""
    urls = await Config.get('openai.api_base_urls', []) or []
    keys = await Config.get('openai.api_keys', []) or []
    try:
        data = await _rotator_request('GET', '/v1/config/upstreams')
    except HTTPException:
        return
    for up in data.get('upstreams') or []:
        name, url = up.get('name'), (up.get('url') or '').rstrip('/')
        for idx, candidate_url in enumerate(urls):
            if candidate_url.rstrip('/') == url:
                key = keys[idx] if idx < len(keys) else ''
                await _rotator_request('PUT', f'/v1/config/upstreams/{name}', {'url': candidate_url, 'key': key})
                break


async def _ensure_candidate_model_row(model_id: str, alias_id: str, user_id: str, db: AsyncSession) -> None:
    """Register the candidate in the model table so the reconciler keeps it."""
    try:
        if await Models.get_model_by_id(model_id, db=db):
            return
        alias = await Models.get_model_by_id(alias_id, db=db)
        form = ModelForm(
            id=model_id,
            base_model_id=None,
            name=alias.name if alias else alias_id,
            meta=ModelMeta(**alias.meta.model_dump()) if alias else ModelMeta(),
            params=ModelParams(**alias.params.model_dump()) if alias else ModelParams(),
            is_active=False,
        )
        await Models.insert_new_model(form, user_id, db=db)
    except Exception as e:
        log.warning('Could not register rotator candidate %s: %s', model_id, e)


async def _current_alias_ids() -> list[str]:
    """Alias ids the rotator currently knows about."""
    data = await _rotator_request('GET', '/v1/config/aliases')
    return [a['id'] for a in (data.get('aliases') or []) if a.get('id')]


@router.get('/aliases')
async def get_rotator_aliases(user=Depends(get_admin_user)):
    """List the rotator aliases and their candidate order."""
    data = await _rotator_request('GET', '/v1/config/aliases')
    aliases = data.get('aliases') or []
    await _sync_rotator_model_ids([a['id'] for a in aliases if a.get('id')])
    await _sync_upstream_keys()
    return {'aliases': aliases}


@router.get('/aliases/{alias_id}')
async def get_rotator_alias(alias_id: str, user=Depends(get_admin_user)):
    return await _rotator_request('GET', f'/v1/config/aliases/{alias_id}')


@router.post('/aliases')
async def create_rotator_alias(payload: RotatorAliasCreate, user=Depends(get_admin_user)):
    data = await _rotator_request(
        'POST',
        '/v1/config/aliases',
        {'id': payload.id, 'name': payload.name or payload.id, 'priority': payload.priority},
    )
    await _sync_rotator_model_ids(await _current_alias_ids())
    return data


@router.put('/aliases/{alias_id}')
async def update_rotator_alias(alias_id: str, payload: RotatorAliasUpdate, user=Depends(get_admin_user)):
    body = {}
    if payload.name is not None:
        body['name'] = payload.name
    if payload.priority is not None:
        body['priority'] = payload.priority
    if payload.strategy is not None:
        body['strategy'] = payload.strategy
    if payload.weights is not None:
        # Drop non-positive weights: a zero weight would mean "never pick",
        # which is better expressed by removing the candidate instead.
        body['weights'] = {k: v for k, v in payload.weights.items() if v and v > 0}
    return await _rotator_request('PUT', f'/v1/config/aliases/{alias_id}', body)


@router.delete('/aliases/{alias_id}')
async def delete_rotator_alias(alias_id: str, user=Depends(get_admin_user)):
    data = await _rotator_request('DELETE', f'/v1/config/aliases/{alias_id}')
    await _sync_rotator_model_ids(await _current_alias_ids())
    return data


@router.post('/aliases/{alias_id}/candidates')
async def add_rotator_candidate(
    alias_id: str,
    payload: RotatorCandidate,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    upstream, model_id = await _resolve_candidate_source(payload.id)
    candidate = f'@{upstream}/{model_id}' if upstream else model_id
    data = await _rotator_request('POST', f'/v1/config/aliases/{alias_id}/candidates', {'id': candidate})
    await _ensure_candidate_model_row(model_id, alias_id, user.id, db)
    return data


@router.delete('/aliases/{alias_id}/candidates')
async def remove_rotator_candidate(alias_id: str, payload: RotatorCandidate, user=Depends(get_admin_user)):
    return await _rotator_request('DELETE', f'/v1/config/aliases/{alias_id}/candidates', {'id': payload.id})


@router.put('/aliases/{alias_id}/priority')
async def reorder_rotator_candidates(alias_id: str, payload: RotatorPriority, user=Depends(get_admin_user)):
    return await _rotator_request('PUT', f'/v1/config/aliases/{alias_id}/priority', {'priority': payload.priority})
