import asyncio
import logging
import random
import sys
import time
import uuid
from typing import Any, Optional

from aiocache import cached
from fastapi import HTTPException, Request, status
from open_webui.env import BYPASS_MODEL_ACCESS_CONTROL, GLOBAL_LOG_LEVEL
from open_webui.functions import generate_function_chat_completion
from open_webui.models.models import Models
from open_webui.models.users import UserModel
from open_webui.routers.ollama import (
    generate_chat_completion as generate_ollama_chat_completion,
)
from open_webui.routers.openai import (
    generate_chat_completion as generate_openai_chat_completion,
)
from open_webui.routers.pipelines import (
    process_pipeline_inlet_filter,
    process_pipeline_outlet_filter,
)
from open_webui.socket.main import (
    EVENT_QUEUES,
    get_event_call,
    get_event_emitter,
)
from open_webui.utils.dsml import parse_dsml
from open_webui.utils.filter import (
    get_filter_functions,
    process_filter_functions,
)
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.models import check_model_access, get_all_models
from open_webui.utils.payload import convert_payload_openai_to_ollama
from open_webui.utils.response import (
    convert_response_ollama_to_openai,
    convert_streaming_response_ollama_to_openai,
)
from starlette.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)


_DSML_MARKERS = ('<||DSML', '||DSML', '\uff5c\uff5cDSML')


def _hold_tail(buffer: str) -> int:
    """Length of the trailing substring that may be a partial DSML marker."""
    hold = 0
    for marker in _DSML_MARKERS:
        for size in range(1, len(marker)):
            if buffer.endswith(marker[:size]):
                hold = max(hold, size)
    return hold


def _content_delta(text: str) -> bytes:
    payload = {'choices': [{'index': 0, 'delta': {'content': text}}]}
    return f'data: {JSONCodec.dumps(payload)}\n\n'.encode()


def _tool_call_delta(call: dict) -> bytes:
    payload = {
        'choices': [
            {
                'index': 0,
                'delta': {
                    'tool_calls': [
                        {
                            'index': 0,
                            'id': call.get('id'),
                            'type': 'function',
                            'function': call.get('function', {}),
                        }
                    ]
                },
                'finish_reason': 'tool_calls',
            }
        ]
    }
    return f'data: {JSONCodec.dumps(payload)}\n\n'.encode()


def _drain(buffer: str, final: bool) -> tuple[list[bytes], str]:
    """Convert complete DSML blocks in ``buffer`` to SSE events.

    Returns ``(events, remaining_buffer)``. Incomplete DSML is held back until
    more bytes arrive (or ``final`` forces a flush).
    """
    events: list[bytes] = []

    clean, calls = parse_dsml(buffer)
    if calls:
        if clean:
            events.append(_content_delta(clean))
        for call in calls:
            events.append(_tool_call_delta(call))
        return events, ''

    start = None
    for marker in _DSML_MARKERS:
        idx = buffer.find(marker)
        if idx != -1 and (start is None or idx < start):
            start = idx

    if start is not None:
        if buffer[:start]:
            events.append(_content_delta(buffer[:start]))
        held = buffer[start:]
        if final and held:
            events.append(_content_delta(held))
            return events, ''
        return events, held

    if final:
        if buffer:
            events.append(_content_delta(buffer))
        return events, ''

    hold = _hold_tail(buffer)
    safe = buffer[: len(buffer) - hold] if hold else buffer
    if safe:
        events.append(_content_delta(safe))
    return events, (buffer[len(buffer) - hold :] if hold else '')


async def _normalize_dsml_stream(stream):
    """Rewrite DeepSeek DSML text tool calls in an SSE stream to tool_calls.

    Assistant content is buffered just enough to detect DSML blocks; complete
    blocks become OpenAI ``tool_calls`` deltas so Open WebUI's native tool loop
    executes them. Non-DSML content streams through unchanged.
    """
    buffer = ''
    async for raw in stream:
        text = raw.decode('utf-8', 'ignore') if isinstance(raw, (bytes, bytearray)) else raw
        if not text.startswith('data:'):
            yield raw if isinstance(raw, (bytes, bytearray)) else text.encode()
            continue

        payload = text[5:].strip()
        if payload in ('', '[DONE]'):
            events, buffer = _drain(buffer, final=True)
            for event in events:
                yield event
            yield text.encode()
            continue

        try:
            data = JSONCodec.loads(payload)
        except Exception:
            yield text.encode()
            continue

        choices = data.get('choices') or []
        delta = choices[0].get('delta') if choices else None
        content = delta.get('content') if isinstance(delta, dict) else None
        if not content:
            yield text.encode()
            continue

        buffer += content
        events, buffer = _drain(buffer, final=False)
        for event in events:
            yield event


# When the question has been asked, let silence not be the
# answer. But if the answer must wait, let it come honest.
async def generate_direct_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    models: dict,
):
    log.info('generate_direct_chat_completion')

    metadata = form_data.pop('metadata', {})

    user_id = metadata.get('user_id')
    session_id = metadata.get('session_id')
    request_id = str(uuid.uuid4())  # Generate a unique request ID

    event_caller = await get_event_call(metadata)
    if event_caller is None:
        raise Exception(
            'Direct connection requires an active WebSocket session; '
            'cannot generate completion in this context (e.g. background task).'
        )

    channel = f'{user_id}:{session_id}:{request_id}'
    logging.info('WebSocket channel: %s', channel)

    if form_data.get('stream'):
        queue = asyncio.Queue()
        EVENT_QUEUES[channel] = queue

        # Start processing chat completion in background
        try:
            res = await event_caller(
                {
                    'type': 'request:chat:completion',
                    'data': {
                        'form_data': form_data,
                        'model': models[form_data['model']],
                        'channel': channel,
                        'session_id': session_id,
                    },
                }
            )

            log.info('res: %s', res)

            status = res.get('status', False)
        except BaseException:
            EVENT_QUEUES.pop(channel, None)
            raise

        if status:
            # Define a generator to stream responses
            async def event_generator():
                try:
                    while True:
                        data = await queue.get()  # Wait for new messages
                        if isinstance(data, dict):
                            if 'done' in data and data['done']:
                                break  # Stop streaming when 'done' is received

                            yield f'data: {JSONCodec.dumps(data)}\n\n'
                        elif isinstance(data, str):
                            if 'data:' in data:
                                yield f'{data}\n\n'
                            else:
                                yield f'data: {data}\n\n'
                except Exception as e:
                    log.debug('Error in event generator: %s', e)
                    pass
                finally:
                    EVENT_QUEUES.pop(channel, None)

            # Define a background task to run the event generator
            async def background():
                EVENT_QUEUES.pop(channel, None)

            # Return the streaming response
            return StreamingResponse(event_generator(), media_type='text/event-stream', background=background)
        else:
            EVENT_QUEUES.pop(channel, None)
            raise Exception(str(res))
    else:
        res = await event_caller(
            {
                'type': 'request:chat:completion',
                'data': {
                    'form_data': form_data,
                    'model': models[form_data['model']],
                    'channel': channel,
                    'session_id': session_id,
                },
            }
        )

        if 'error' in res and res['error']:
            raise Exception(res['error'])

        return res


async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    bypass_filter: bool = False,
    bypass_system_prompt: bool = False,
):
    log.debug('generate_chat_completion: %s', form_data)
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    # Propagate bypass_filter and bypass_system_prompt via request.state so that
    # downstream route handlers (openai/ollama) can read them without exposing
    # them as query parameters.
    request.state.bypass_filter = bypass_filter
    request.state.bypass_system_prompt = bypass_system_prompt

    if hasattr(request.state, 'metadata'):
        if 'metadata' not in form_data:
            form_data['metadata'] = request.state.metadata
        else:
            form_data['metadata'] = {
                **form_data['metadata'],
                **request.state.metadata,
            }

    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        # Merge the direct connection model into server models so that
        # task functions (title, tags, etc.) can resolve a server-side
        # task model while still having the direct model available.
        # dict(...items()) is one HGETALL on a Redis-backed pool; ``{**pool}``
        # would issue HKEYS plus one HGET per model.
        models = {
            **dict(request.app.state.MODELS.items()),
            request.state.model['id']: request.state.model,
        }
        log.debug('direct connection to model: %s', request.state.model['id'])
    else:
        models = request.app.state.MODELS

    model_id = form_data['model']
    # Single lookup — membership check plus getitem would be two Redis
    # round trips on a Redis-backed model pool.
    model = models.get(model_id)
    if model is None:
        raise Exception('Model not found')

    if getattr(request.state, 'direct', False) and model_id == getattr(request.state, 'model', {}).get('id'):
        return await generate_direct_chat_completion(request, form_data, user=user, models=models)
    else:
        # Check if user has access to the model
        if not bypass_filter and user.role == 'user':
            try:
                await check_model_access(user, model)
            except Exception as e:
                raise e

        # Arena model — sub-model was already resolved by process_chat_payload.
        # Inject selected_model_id into the response for the frontend.
        metadata = form_data.get('metadata', {})
        selected_model_id = metadata.pop('selected_model_id', None)
        # Also clear from request.state.metadata to prevent the merge at
        # lines 177-179 from re-adding it on the recursive call.
        if hasattr(request.state, 'metadata'):
            request.state.metadata.pop('selected_model_id', None)

        # Fallback: if generate_chat_completion is called with an arena model
        # from a path that did NOT go through process_chat_payload (e.g.,
        # background tasks for title/follow-up/tags generation), resolve now.
        if not selected_model_id and model.get('owned_by') == 'arena':
            model_ids = model.get('info', {}).get('meta', {}).get('model_ids')
            filter_mode = model.get('info', {}).get('meta', {}).get('filter_mode')
            if model_ids and filter_mode == 'exclude':
                model_ids = [
                    available_model['id']
                    for available_model in list(request.app.state.MODELS.values())
                    if available_model.get('owned_by') != 'arena' and available_model['id'] not in model_ids
                ]

            if isinstance(model_ids, list) and model_ids:
                selected_model_id = random.choice(model_ids)
            else:
                model_ids = [
                    available_model['id']
                    for available_model in list(request.app.state.MODELS.values())
                    if available_model.get('owned_by') != 'arena'
                ]
                selected_model_id = random.choice(model_ids)

            form_data['model'] = selected_model_id

            # bypass_filter recursion below skips the line-200 check; gate the resolved model here.
            if not bypass_filter and user.role == 'user':
                selected_model = request.app.state.MODELS.get(selected_model_id)
                if selected_model:
                    await check_model_access(user, selected_model)

        if selected_model_id:
            if form_data.get('stream') == True:

                async def stream_wrapper(stream):
                    yield f'data: {JSONCodec.dumps({"selected_model_id": selected_model_id})}\n\n'
                    async for chunk in stream:
                        yield chunk

                response = await generate_chat_completion(
                    request,
                    form_data,
                    user,
                    bypass_filter=True,
                    bypass_system_prompt=bypass_system_prompt,
                )
                # Upstream errors come back as a response object.
                if not isinstance(response, StreamingResponse):
                    return response
                return StreamingResponse(
                    stream_wrapper(response.body_iterator),
                    media_type='text/event-stream',
                    background=response.background,
                )
            else:
                response = await generate_chat_completion(
                    request,
                    form_data,
                    user,
                    bypass_filter=True,
                    bypass_system_prompt=bypass_system_prompt,
                )
                if not isinstance(response, dict):
                    return response
                return {**response, 'selected_model_id': selected_model_id}

        if model.get('pipe'):
            # Below does not require bypass_filter because this is the only route the uses this function and it is already bypassing the filter
            return await generate_function_chat_completion(request, form_data, user=user, models=models)
        if model.get('owned_by') == 'ollama':
            # Using /ollama/api/chat endpoint
            form_data = convert_payload_openai_to_ollama(form_data)
            response = await generate_ollama_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
            )
            if form_data.get('stream'):
                response.headers['content-type'] = 'text/event-stream'
                return StreamingResponse(
                    convert_streaming_response_ollama_to_openai(response),
                    headers=dict(response.headers),
                    background=response.background,
                )
            else:
                return convert_response_ollama_to_openai(response)
        else:
            response = await generate_openai_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
            )
            if isinstance(response, StreamingResponse):
                return StreamingResponse(
                    _normalize_dsml_stream(response.body_iterator),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                    background=response.background,
                )
            return response


chat_completion = generate_chat_completion


async def chat_completed(request: Request, form_data: dict, user: Any):
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(request.app.state.MODELS.items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    data = form_data

    if not data.get('id'):
        raise Exception('Missing message id')

    model_id = data['model']
    if model_id not in models:
        raise Exception('Model not found')

    model = models[model_id]

    try:
        data = await process_pipeline_outlet_filter(request, data, user, models)
    except HTTPException:
        raise
    except Exception as e:
        raise Exception(f'Error: {e}')

    if not data.get('id'):
        raise Exception('Missing message id')

    metadata = {
        'chat_id': data['chat_id'],
        'message_id': data['id'],
        'filter_ids': data.get('filter_ids', []),
        'session_id': data['session_id'],
        'user_id': user.id,
    }

    extra_params = {
        '__event_emitter__': await get_event_emitter(metadata),
        '__event_call__': await get_event_call(metadata),
        '__user__': user.model_dump() if isinstance(user, UserModel) else {},
        '__metadata__': metadata,
        '__request__': request,
        '__model__': model,
    }

    try:
        filter_functions = await get_filter_functions(request, model, metadata.get('filter_ids', []))

        result, _ = await process_filter_functions(
            request=request,
            filter_context=None,
            filter_functions=filter_functions,
            filter_type='outlet',
            form_data=data,
            extra_params=extra_params,
        )
        return result
    except Exception as e:
        raise Exception(f'Error: {e}')
