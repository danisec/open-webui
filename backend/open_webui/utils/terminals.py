"""Shared routing helpers for admin-configured terminal servers."""

import asyncio
import base64
import hashlib
import logging
import ntpath
import posixpath
from urllib.parse import quote

from open_webui.env import ENABLE_TOOL_SERVERS
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.skill_files import (
    SKILL_CONTENT_MAX_CHARS,
    bounded_skill_manifest,
    format_skill_content,
    skill_content_page,
)

TERMINAL_CONTEXT_HEADER = 'X-Terminal-Context-Id'
TERMINAL_CONTEXT_DEFAULT = 'default'
TERMINAL_CONTEXT_TYPES = {'chat', 'automation'}
TERMINAL_CONTEXT_ID_SOURCES = {'chat': 'chat_id', 'automation': 'automation_id'}
TERMINAL_CHAT_UPLOAD_MODES = {'default', 'filesystem'}
TERMINAL_IMAGE_MAX_BYTES = 10 * 1024 * 1024
TERMINAL_IMAGE_CONTENT_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
}
MAX_AGENTS_MD_BYTES = 32 * 1024

log = logging.getLogger(__name__)


def is_terminal_orchestrator(connection: dict) -> bool:
    """Return whether this connection points at Terminals, not raw Open Terminal."""
    return connection.get('server_type') == 'orchestrator' or bool(connection.get('policy_id'))


def get_terminal_server_url(connection: dict) -> str:
    """Return the upstream base URL for a terminal connection.

    An explicit policy uses the named-policy route. Connections without one
    keep their existing root route.
    """
    base_url = str(connection.get('url') or '').rstrip('/')
    policy_id = str(connection.get('policy_id') or '').strip()
    if policy_id:
        return f'{base_url}/p/{quote(policy_id, safe="")}'
    return base_url


def terminal_context_config(connection: dict, context: str) -> dict | bool:
    """Return config for an OpenWebUI terminal context.

    Missing config is legacy behavior: available, shared default terminal.
    """
    if not is_terminal_orchestrator(connection):
        return {}

    contexts = (connection.get('config') or {}).get('contexts')
    if not isinstance(contexts, dict):
        return {}

    value = contexts.get(context, {})
    if value is False:
        return False
    return value if isinstance(value, dict) else {}


def terminal_context_available(connection: dict, context: str) -> bool:
    """Return whether this terminal is exposed in an OpenWebUI context."""
    if context not in TERMINAL_CONTEXT_TYPES:
        return False
    return terminal_context_config(connection, context) is not False


def terminal_context_id(
    connection: dict,
    metadata: dict | None = None,
    context: str = 'chat',
) -> str | None:
    """Return the terminal runtime context for trusted request metadata."""
    if not is_terminal_orchestrator(connection) or not terminal_context_available(connection, context):
        return None

    config = terminal_context_config(connection, context)
    context_id_source = config.get('context_id') if isinstance(config, dict) else None
    if not context_id_source or context_id_source == TERMINAL_CONTEXT_DEFAULT:
        return None

    if context_id_source != TERMINAL_CONTEXT_ID_SOURCES.get(context):
        return None

    metadata = metadata or {}

    if context == 'automation':
        automation_id = metadata.get('automation_id')
        return f'automation:{automation_id}' if automation_id else None

    chat_id = metadata.get('chat_id')
    if context == 'chat' and chat_id and is_saved_chat_id(chat_id):
        return f'chat:{chat_id}'
    return None


def terminal_contexts(connection: dict) -> dict:
    """Return normalized sparse context config for clients."""
    if not is_terminal_orchestrator(connection):
        return {}

    contexts = (connection.get('config') or {}).get('contexts')
    if not isinstance(contexts, dict):
        return {}

    result = {}
    for context, value in contexts.items():
        if context not in TERMINAL_CONTEXT_TYPES:
            continue
        if value is False:
            result[context] = False
        elif isinstance(value, dict):
            context_id_source = value.get('context_id')
            if context_id_source in {TERMINAL_CONTEXT_DEFAULT, TERMINAL_CONTEXT_ID_SOURCES[context]}:
                result[context] = {'context_id': context_id_source}
            else:
                result[context] = {}
    return result


def terminal_chat_uploads(connection: dict) -> str:
    """Return normalized main-chat upload behavior for this connection."""
    value = (connection.get('config') or {}).get('chat_uploads')
    return value if value in TERMINAL_CHAT_UPLOAD_MODES else 'default'


async def get_terminal_json(request, user, metadata: dict, path: str, extra_params: dict | None = None):
    """Read from an admin terminal on the backend or a personal terminal in its browser."""
    if not ENABLE_TOOL_SERVERS:
        return None

    import aiohttp
    from open_webui.env import AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL, AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA
    from open_webui.models.config import Config
    from open_webui.models.groups import Groups
    from open_webui.models.users import UserModel
    from open_webui.utils.access_control import has_connection_access
    from open_webui.utils.tools import build_tool_server_headers

    metadata = metadata or {}
    terminal_id = metadata.get('terminal_id')
    if not terminal_id:
        return None

    user_model = user if isinstance(user, UserModel) else UserModel(**user)
    connections = await Config.get('terminal_server.connections', []) or []
    connection = next((item for item in connections if item.get('id') == terminal_id), None)

    if connection:
        terminal_context = 'automation' if metadata.get('automation_id') else 'chat'
        context_id = terminal_context_id(connection, metadata, terminal_context)
        config = terminal_context_config(connection, terminal_context)
        if (
            not connection.get('enabled', True)
            or not terminal_context_available(connection, terminal_context)
            or (config.get('context_id') in {'chat_id', 'automation_id'} and not context_id)
        ):
            return None
        user_group_ids = {
            group.id for group in await Groups.get_groups_by_member_id(user_model.id, include_inherited=True)
        }
        if not await has_connection_access(user_model, connection, user_group_ids):
            return None

        headers, cookies = await build_tool_server_headers(
            connection,
            request,
            user_model,
            metadata=metadata,
        )
        headers['Accept'] = 'application/json'
        headers['X-User-Id'] = user_model.id
        if metadata.get('chat_id'):
            headers['X-Session-Id'] = metadata['chat_id']
        if context_id:
            headers[TERMINAL_CONTEXT_HEADER] = context_id
        timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f'{get_terminal_server_url(connection)}{path}',
                headers=headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
                allow_redirects=False,
            ) as response:
                return await response.json() if response.status == 200 else None

    event_caller = (extra_params or {}).get('__event_call__')
    if event_caller:
        result = await event_caller(
            {
                'type': 'request:terminal',
                'data': {'terminal_id': terminal_id, 'path': path, 'session_id': metadata.get('session_id')},
            }
        )
        return result.get('data') if isinstance(result, dict) else None
    return None


async def get_terminal_agents_md(request, user, metadata: dict, extra_params: dict | None = None) -> str | None:
    """Load the selected terminal user's home AGENTS.md afresh for this turn."""
    try:
        async with asyncio.timeout(5):
            data = await get_terminal_json(request, user, metadata, '/files/cwd', extra_params)
            home = data.get('home') if isinstance(data, dict) else None
            if not isinstance(home, str) or not (posixpath.isabs(home) or ntpath.isabs(home)):
                return None
            path = quote(posixpath.join(home, 'AGENTS.md'), safe='')
            data = await get_terminal_json(request, user, metadata, f'/files/read?path={path}', extra_params)

        content = data.get('content') if isinstance(data, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        if len(content.encode('utf-8')) > MAX_AGENTS_MD_BYTES:
            log.warning('Skipping terminal AGENTS.md: exceeds %s bytes', MAX_AGENTS_MD_BYTES)
            return None
        return f'# AGENTS.md\n\n{content}'
    except Exception as e:
        log.debug('Failed to load terminal AGENTS.md (%s)', type(e).__name__)
        return None


async def get_terminal_image_data_uri(request, user, metadata: dict, path: str) -> str | None:
    """Inline an image from the caller's terminal workspace as a data URI.

    The terminal maps X-User-Id to a per-user home and rejects paths in another
    user's home, so only paths under the home returned by /files/cwd are read.
    """
    if not ENABLE_TOOL_SERVERS or not isinstance(path, str):
        return None

    normalized = posixpath.normpath(path)
    if not posixpath.isabs(normalized):
        return None
    content_type = TERMINAL_IMAGE_CONTENT_TYPES.get(posixpath.splitext(normalized)[1].lower())
    if not content_type:
        return None

    metadata = metadata or {}
    chat_id = metadata.get('chat_id')
    if not is_saved_chat_id(chat_id):
        return None

    import aiohttp
    from open_webui.env import AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL, AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA
    from open_webui.models.config import Config
    from open_webui.models.groups import Groups
    from open_webui.models.users import UserModel
    from open_webui.utils.access_control import has_connection_access
    from open_webui.utils.tools import build_tool_server_headers

    user_model = user if isinstance(user, UserModel) else UserModel(**user)
    connections = await Config.get('terminal_server.connections', []) or []
    terminal_id = metadata.get('terminal_id')
    group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_model.id, include_inherited=True)}

    for connection in connections:
        if not connection.get('enabled', True) or terminal_chat_uploads(connection) != 'filesystem':
            continue
        if terminal_id and connection.get('id') != terminal_id:
            continue
        if not terminal_context_available(connection, 'chat'):
            continue
        if not await has_connection_access(user_model, connection, group_ids):
            continue

        headers, cookies = await build_tool_server_headers(
            connection,
            request,
            user_model,
            metadata=metadata,
        )
        headers['Accept'] = '*/*'
        headers['X-User-Id'] = user_model.id
        headers['X-Session-Id'] = chat_id
        context_id = terminal_context_id(connection, metadata, 'chat')
        if context_id:
            headers[TERMINAL_CONTEXT_HEADER] = context_id

        base_url = get_terminal_server_url(connection)
        timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(
                    f'{base_url}/files/cwd',
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        continue
                    cwd = await response.json(content_type=None)

                home = cwd.get('home') if isinstance(cwd, dict) else None
                if not isinstance(home, str) or not posixpath.isabs(home):
                    continue
                home = posixpath.normpath(home)
                try:
                    if normalized == home or posixpath.commonpath([home, normalized]) != home:
                        continue
                except ValueError:
                    continue

                async with session.get(
                    f'{base_url}/files/read?path={quote(normalized, safe="")}',
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        continue

                    response_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                    if response_type not in (content_type, 'application/octet-stream'):
                        continue

                    declared_size = response.headers.get('Content-Length')
                    try:
                        if declared_size and int(declared_size) > TERMINAL_IMAGE_MAX_BYTES:
                            continue
                    except ValueError:
                        pass

                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > TERMINAL_IMAGE_MAX_BYTES:
                            data = bytearray()
                            break
                    if not data:
                        continue

                    encoded = base64.b64encode(bytes(data)).decode('utf-8')
                    return f'data:{content_type};base64,{encoded}'
        except Exception as e:
            log.debug('Terminal image read failed (%s): %s', normalized, type(e).__name__)

    return None


def add_terminal_agents_md(messages: list[dict], agents_md: str) -> list[dict]:
    """Place file instructions before user requests without changing their content."""
    for index, message in enumerate(messages):
        if message.get('role') == 'user':
            return [*messages[:index], {'role': 'user', 'content': agents_md}, *messages[index:]]
    return messages


async def get_terminal_skill(
    request,
    user,
    metadata: dict,
    skill_name: str,
    extra_params: dict | None = None,
    *,
    offset: int = 0,
    max_chars: int = SKILL_CONTENT_MAX_CHARS,
    refresh: bool = True,
) -> dict | None:
    skill = await get_terminal_json(
        request, user, metadata, f'/skills/read?name={quote(skill_name, safe="")}', extra_params
    )

    if not isinstance(skill, dict) or not isinstance(skill.get('content'), str):
        return None

    context = metadata.get('chat_context') or {}
    metadata['chat_context'] = context
    fingerprints = context.setdefault('terminal_skill_fingerprints', {}).setdefault(metadata['terminal_id'], {})
    skill_id = f'terminal:{quote(skill_name, safe="")}'
    fingerprint = hashlib.sha256(skill['content'].encode('utf-8')).hexdigest()
    previous = fingerprints.get(skill_id)
    if not refresh and previous is not None and previous != fingerprint:
        raise ValueError('Skill changed while reading. Call view_skill again and restart reading.')
    fingerprints[skill_id] = fingerprint

    location = skill.get('location') or skill.get('path') or ''
    directory = location.rsplit('/', 1)[0] if '/' in location else location
    resources = skill.get('resources') if isinstance(skill.get('resources'), list) else []
    return {
        'name': skill.get('name'),
        'description': skill.get('description'),
        **skill_content_page(skill['content'], offset, max_chars),
        'directory': directory,
        **bounded_skill_manifest(resources, 'resources'),
    }


def format_terminal_skill_context(skill: dict, skill_id: str, tools_enabled: bool) -> str:
    resources = skill.get('resources') if isinstance(skill.get('resources'), list) else []
    parts = [f'<skill name="{skill.get("name") or ""}">', format_skill_content(skill, skill_id, tools_enabled)]
    if skill.get('directory'):
        parts.append(f'<directory>{skill["directory"]}</directory>')
    if resources:
        parts.append('<resources>')
        parts.extend(f'<file>{resource}</file>' for resource in resources)
        parts.append('</resources>')
    if skill.get('notice'):
        parts.append(skill['notice'])
    parts.append('</skill>')
    return '\n'.join(parts)


def format_terminal_skill_manifest_entry(skill: dict) -> str:
    location = skill.get('location') or skill.get('path') or ''
    location_tag = f'<location>{location}</location>\n' if location else ''
    return (
        f'<skill>\n<id>{skill["id"]}</id>\n<name>{skill["name"]}</name>\n'
        f'<description>{skill.get("description") or ""}</description>\n'
        f'<source>terminal</source>\n'
        f'{location_tag}'
        f'</skill>\n'
    )


async def record_terminal_upload(chat_id: str, user_id: str, path: str) -> None:
    """Remember a file uploaded into a terminal workspace, keyed by chat.

    Uploads bypass Open WebUI's file table (chat_uploads=filesystem), so this
    registry is what lets the chat-deleted cleanup remove the workspace copy.
    """
    import time

    from open_webui.internal.db import get_async_db_context
    from sqlalchemy import text

    if not chat_id or not path:
        return

    try:
        async with get_async_db_context() as db:
            await db.execute(
                text(
                    'INSERT INTO terminal_upload (id, chat_id, user_id, path, created_at) '
                    'SELECT :id, :chat_id, :user_id, :path, :ts '
                    'WHERE NOT EXISTS (SELECT 1 FROM terminal_upload WHERE chat_id = :chat_id AND path = :path)'
                ),
                {
                    'id': f'{chat_id}:{path}',
                    'chat_id': chat_id,
                    'user_id': user_id or '',
                    'path': path,
                    'ts': int(time.time()),
                },
            )
            await db.commit()
    except Exception:
        log.exception('Failed to record terminal upload for chat %s', chat_id)


def _with_skip_kg_hint(body: bytes, filename: str) -> bytes:
    """Rewrite the multipart part to carry LightRAG's skip-KG filename hint.

    LightRAG reads per-file process options from the filename (``name.[-!].ext``)
    and the upload endpoint takes no form field for them, so the part is
    rewritten in place. Returns the body unchanged when it cannot match.
    """
    stem, ext = posixpath.splitext(filename)
    if not stem or stem.endswith('.[-!]'):
        return body
    old = f'filename="{filename}"'.encode()
    if old not in body:
        return body
    return body.replace(old, f'filename="{stem}.[-!]{ext}"'.encode(), 1)


async def forward_terminal_upload_to_lightrag(
    body: bytes, content_type: str | None, workspace: str, filename: str
) -> None:
    """Index an uploaded file in LightRAG, in the uploader's own workspace.

    The client's multipart body is forwarded unchanged: the terminal upload and
    LightRAG's ``/documents/upload`` both take a single ``file`` part, so the
    payload is already the right shape. The workspace header is what keeps each
    user's documents separate. A 409 means LightRAG already has that filename in
    the workspace, which is a success for our purposes.
    """
    from open_webui.env import (
        LIGHTRAG_INDEX_EXTS,
        LIGHTRAG_INDEX_TIMEOUT,
        LIGHTRAG_INDEX_TOKEN,
        LIGHTRAG_INDEX_URL,
    )

    if not LIGHTRAG_INDEX_URL or not body or not workspace:
        return

    ext = posixpath.splitext(filename)[1].lower().lstrip('.')
    if LIGHTRAG_INDEX_EXTS and ext not in LIGHTRAG_INDEX_EXTS:
        log.debug('LightRAG index skipped for %s: extension not indexed', filename)
        return

    from open_webui.env import LIGHTRAG_SKIP_KG_OVER_MB

    size_mb = len(body) / 1048576
    if LIGHTRAG_SKIP_KG_OVER_MB and size_mb >= LIGHTRAG_SKIP_KG_OVER_MB:
        hinted = _with_skip_kg_hint(body, filename)
        if hinted is not body:
            body = hinted
            log.info(
                'LightRAG: %s is %.1f MB, indexing without knowledge-graph extraction',
                filename,
                size_mb,
            )

    import aiohttp

    headers = {'Accept': 'application/json', 'LIGHTRAG-WORKSPACE': workspace}
    if content_type:
        headers['Content-Type'] = content_type
    if LIGHTRAG_INDEX_TOKEN:
        headers['Authorization'] = f'Bearer {LIGHTRAG_INDEX_TOKEN}'

    try:
        timeout = aiohttp.ClientTimeout(total=LIGHTRAG_INDEX_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(f'{LIGHTRAG_INDEX_URL}/documents/upload', data=body, headers=headers) as response:
                if response.status == 409:
                    log.info('LightRAG already holds %s (workspace %s)', filename, workspace)
                elif response.status >= 300:
                    detail = (await response.text())[:200]
                    log.warning(
                        'LightRAG index failed for %s (workspace %s): HTTP %s %s',
                        filename,
                        workspace,
                        response.status,
                        detail,
                    )
                else:
                    log.info('LightRAG indexed %s into workspace %s', filename, workspace)
    except Exception:
        log.exception('LightRAG index request failed for %s', filename)


async def lightrag_status_for_paths(workspace: str, paths: list[str]) -> dict[str, dict]:
    """Indexing status per uploaded path, read-only.

    Keys are the requested paths; a path LightRAG has no document for is absent
    from the result, so the caller can tell "not indexed yet" from "failed".
    """
    from open_webui.env import LIGHTRAG_INDEX_TIMEOUT, LIGHTRAG_INDEX_TOKEN, LIGHTRAG_INDEX_URL

    if not LIGHTRAG_INDEX_URL or not workspace or not paths:
        return {}

    import aiohttp

    headers = {'Accept': 'application/json', 'LIGHTRAG-WORKSPACE': workspace}
    if LIGHTRAG_INDEX_TOKEN:
        headers['Authorization'] = f'Bearer {LIGHTRAG_INDEX_TOKEN}'

    try:
        timeout = aiohttp.ClientTimeout(total=min(LIGHTRAG_INDEX_TIMEOUT, 15))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(f'{LIGHTRAG_INDEX_URL}/documents/paginated', json={}, headers=headers) as response:
                if response.status >= 300:
                    return {}
                payload = await response.json()
    except Exception:
        log.warning('LightRAG status lookup failed (workspace %s)', workspace)
        return {}

    docs = payload.get('documents') if isinstance(payload, dict) else payload
    by_name: dict[str, dict] = {}
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        name = posixpath.basename(str(doc.get('file_path') or ''))
        if name:
            by_name[name] = doc

    result: dict[str, dict] = {}
    for path in paths:
        doc = by_name.get(posixpath.basename(path))
        if doc is None:
            continue
        result[path] = {
            'status': str(doc.get('status') or ''),
            'chunks': doc.get('chunks_count'),
            'error': doc.get('error_msg') or None,
            'updated_at': doc.get('updated_at'),
        }
    return result


async def delete_terminal_upload_from_lightrag(workspace: str, path: str) -> None:
    """Drop a deleted workspace file from LightRAG.

    The workspace copy is already gone when this runs, so the match is by
    filename — that is what LightRAG keeps as a document's ``file_path``. A path
    that no longer has a document is not an error: the file may never have been
    indexed (unsupported extension, LightRAG unreachable at upload time).
    """
    from open_webui.env import (
        LIGHTRAG_INDEX_TIMEOUT,
        LIGHTRAG_INDEX_TOKEN,
        LIGHTRAG_INDEX_URL,
    )

    if not LIGHTRAG_INDEX_URL or not workspace or not path:
        return

    filename = posixpath.basename(path.rstrip('/'))
    if not filename:
        return

    import aiohttp

    headers = {'Accept': 'application/json', 'LIGHTRAG-WORKSPACE': workspace}
    if LIGHTRAG_INDEX_TOKEN:
        headers['Authorization'] = f'Bearer {LIGHTRAG_INDEX_TOKEN}'

    try:
        timeout = aiohttp.ClientTimeout(total=LIGHTRAG_INDEX_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(f'{LIGHTRAG_INDEX_URL}/documents/paginated', json={}, headers=headers) as response:
                if response.status >= 300:
                    log.warning(
                        'LightRAG lookup failed (workspace %s): HTTP %s',
                        workspace,
                        response.status,
                    )
                    return
                payload = await response.json()

            docs = payload.get('documents') if isinstance(payload, dict) else payload
            doc_ids = [
                doc.get('id')
                for doc in (docs or [])
                if isinstance(doc, dict)
                and posixpath.basename(str(doc.get('file_path') or '')) == filename
                and doc.get('id')
            ]
            if not doc_ids:
                return

            async with session.delete(
                f'{LIGHTRAG_INDEX_URL}/documents/delete_document',
                json={'doc_ids': doc_ids, 'delete_llm_cache': True},
                headers=headers,
            ) as response:
                if response.status >= 300:
                    detail = (await response.text())[:200]
                    log.warning(
                        'LightRAG delete failed for %s (workspace %s): HTTP %s %s',
                        filename,
                        workspace,
                        response.status,
                        detail,
                    )
                else:
                    log.info('LightRAG removed %s from workspace %s', filename, workspace)
    except Exception:
        log.exception('LightRAG delete request failed for %s', filename)
