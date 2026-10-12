"""Normalize DeepSeek DSML tool-call text into OpenAI-style tool_calls.

Some DeepSeek deployments emit tool calls as plain text in a proprietary
markup instead of native ``tool_calls`` when the client does not send native
tool schemas. The markup looks like::

    <||DSML|| calls>
    <||DSML|| invoke name="run_command">
    <||DSML|| parameter name="command" string="true">pwd</||DSML|| parameter>
    </||DSML|| invoke>
    </||DSML|| calls>

The markers use either ASCII pipes (``||DSML||``) or fullwidth bars
(``\uff5c\uff5cDSML\uff5c\uff5c``). This module detects such blocks, extracts
the invocations, and returns them as OpenAI ``tool_calls`` so the normal tool
loop can execute them. Any surrounding prose is preserved.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# Match a DSML marker with either ASCII pipes or fullwidth vertical bars.
_BAR = r'(?:\|\||\uff5c\uff5c)'
_CALLS_OPEN = re.compile(rf'<\s*{_BAR}\s*DSML\s*{_BAR}\s*calls\s*>')
_CALLS_CLOSE = re.compile(rf'</\s*{_BAR}\s*DSML\s*{_BAR}\s*calls\s*>')
_INVOKE_OPEN = re.compile(rf'<\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s+name=\"([^\"]+)\"\s*>')
_INVOKE_CLOSE = re.compile(rf'</\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s*>')
_PARAM = re.compile(
    rf'<\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(?:true|false)\")?\s*>(.*?)</\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s*>',
    re.DOTALL,
)

_DSML_HINT = re.compile(rf'{_BAR}\s*DSML\s*{_BAR}')


def contains_dsml(text: str | None) -> bool:
    return bool(text) and bool(_DSML_HINT.search(text))


def _coerce(value: str) -> Any:
    """Best-effort JSON coercion so numeric/boolean params are typed."""
    stripped = value.strip()
    if stripped == '':
        return ''
    try:
        return json.loads(stripped)
    except Exception:
        return value


def parse_dsml(text: str | None) -> tuple[str, list[dict]]:
    """Split ``text`` into (clean_text, tool_calls).

    ``tool_calls`` is empty when no DSML block is present. Each entry follows
    the OpenAI Chat Completions shape consumed by Open WebUI's tool loop.
    """
    if not contains_dsml(text):
        return (text or ''), []

    original = text or ''
    tool_calls: list[dict] = []

    def _parse_block(block: str) -> None:
        for invoke_match in _INVOKE_OPEN.finditer(block):
            name = invoke_match.group(1).strip()
            start = invoke_match.end()
            end_match = _INVOKE_CLOSE.search(block, start)
            body = block[start : end_match.start()] if end_match else block[start:]

            arguments: dict[str, Any] = {}
            for param in _PARAM.finditer(body):
                arguments[param.group(1).strip()] = _coerce(param.group(2))

            tool_calls.append(
                {
                    'id': f'call_{uuid.uuid4().hex[:24]}',
                    'type': 'function',
                    'function': {
                        'name': name,
                        'arguments': json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )

    # Prefer well-formed <...calls>...</...calls> blocks; fall back to any invoke.
    found_block = False
    for calls_match in _CALLS_OPEN.finditer(original):
        block_start = calls_match.end()
        close_match = _CALLS_CLOSE.search(original, block_start)
        block_end = close_match.start() if close_match else len(original)
        _parse_block(original[block_start:block_end])
        found_block = True

    if not found_block and _INVOKE_OPEN.search(original):
        _parse_block(original)

    if not tool_calls:
        return original, []

    # Strip every DSML span (outer calls block, or the bare invoke block) from prose.
    clean = original
    if found_block:
        clean = _CALLS_OPEN.sub('', clean)
        clean = _CALLS_CLOSE.sub('', clean)
        clean = _INVOKE_OPEN.sub('', clean)
        clean = _INVOKE_CLOSE.sub('', clean)
        clean = _PARAM.sub('', clean)
    else:
        clean = _INVOKE_OPEN.sub('', clean)
        clean = _INVOKE_CLOSE.sub('', clean)
        clean = _PARAM.sub('', clean)

    # Collapse leftover marker fragments and stray blank lines.
    clean = re.sub(rf'<\s*/?\s*{_BAR}\s*DSML\s*{_BAR}\s*[^>]*>', '', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
    return clean, tool_calls
