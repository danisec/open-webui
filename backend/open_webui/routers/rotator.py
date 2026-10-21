"""Admin API for the model-rotator alias configuration.

The rotator runs as a sidecar proxy and owns the alias -> candidate mapping.
Open WebUI proxies to it so admins can manage aliases from Settings, and keeps
the OpenAI connection's model list in sync with the aliases that exist.
"""

import os

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.models.config import Config
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field

router = APIRouter()

ROTATOR_ADMIN_TOKEN = os.environ.get('MODEL_ROTATOR_ADMIN_TOKEN', '')
ROTATOR_TIMEOUT = 30


class RotatorAliasCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field('', max_length=256)
    priority: list[str] = []


class RotatorAliasUpdate(BaseModel):
    name: str | None = Field(None, max_length=256)
    priority: list[str] | None = None


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
    return await _rotator_request('PUT', f'/v1/config/aliases/{alias_id}', body)


@router.delete('/aliases/{alias_id}')
async def delete_rotator_alias(alias_id: str, user=Depends(get_admin_user)):
    data = await _rotator_request('DELETE', f'/v1/config/aliases/{alias_id}')
    await _sync_rotator_model_ids(await _current_alias_ids())
    return data


@router.post('/aliases/{alias_id}/candidates')
async def add_rotator_candidate(alias_id: str, payload: RotatorCandidate, user=Depends(get_admin_user)):
    return await _rotator_request('POST', f'/v1/config/aliases/{alias_id}/candidates', {'id': payload.id})


@router.delete('/aliases/{alias_id}/candidates')
async def remove_rotator_candidate(alias_id: str, payload: RotatorCandidate, user=Depends(get_admin_user)):
    return await _rotator_request('DELETE', f'/v1/config/aliases/{alias_id}/candidates', {'id': payload.id})


@router.put('/aliases/{alias_id}/priority')
async def reorder_rotator_candidates(alias_id: str, payload: RotatorPriority, user=Depends(get_admin_user)):
    return await _rotator_request('PUT', f'/v1/config/aliases/{alias_id}/priority', {'priority': payload.priority})
