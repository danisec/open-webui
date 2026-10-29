"""Periodic cleanup for the speech cache.

Open WebUI writes every synthesized utterance to ``data/cache/audio/speech`` as
a pair of files (``<sha256>.mp3`` + ``<sha256>.json``) and never removes them:
there is no TTL, no size cap, and no admin endpoint to clear it. On an instance
where Read Aloud is used heavily this reaches hundreds of megabytes within
weeks.

The cache is a pure performance shortcut. A missing entry costs one synthesis
(~1.9s) instead of a cache hit (~0.015s) and nothing else, so evicting old
entries is always safe.

Two bounds are applied, oldest first, so a burst of usage cannot outrun the age
limit before the next sweep:

1. age   — delete entries older than ``audio.tts.cache_max_age_days``
2. size  — while the directory exceeds ``audio.tts.cache_max_size_mb``, delete
           the oldest remaining entries

Files are always removed in pairs. Deleting only the audio would leave an orphan
JSON, and deleting only the JSON would leave an audio file that can never be
served, because the lookup recomputes the key and needs both.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from open_webui.config import CACHE_DIR
from open_webui.models.config import Config

log = logging.getLogger(__name__)

SPEECH_CACHE_DIR = CACHE_DIR / 'audio' / 'speech'

# Run hourly: cheap (a single scandir), and frequent enough that the size bound
# is enforced before a busy hour can overshoot it.
CLEANUP_INTERVAL_SECONDS = int(os.getenv('SPEECH_CACHE_CLEANUP_INTERVAL', '3600'))
# Skip the very first sweep so a restart-heavy period does not repeatedly walk a
# large directory while the app is still warming up.
INITIAL_DELAY_SECONDS = int(os.getenv('SPEECH_CACHE_CLEANUP_INITIAL_DELAY', '300'))

DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MAX_SIZE_MB = 500


def _cache_key_for(path: Path) -> str:
    """Group the audio file and its metadata under one key."""
    return str(path.with_suffix(''))


def _speech_files() -> list[tuple[float, list[Path]]]:
    """Speech cache entries as (mtime, [files]) sorted oldest first."""
    try:
        entries = [p for p in SPEECH_CACHE_DIR.iterdir() if p.is_file()]
    except FileNotFoundError:
        return []
    except OSError:
        return []

    grouped: dict[str, list[Path]] = {}
    for path in entries:
        grouped.setdefault(_cache_key_for(path), []).append(path)

    result = []
    for files in grouped.values():
        try:
            # The oldest sibling decides the entry's age: a .json rewritten later
            # must not keep stale audio alive.
            mtime = min(f.stat().st_mtime for f in files)
        except OSError:
            continue
        result.append((mtime, files))

    result.sort(key=lambda item: item[0])
    return result


def _total_bytes(entries: list[tuple[float, list[Path]]]) -> int:
    total = 0
    for _, files in entries:
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _remove(files: list[Path]) -> int:
    removed = 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def prune_speech_cache_sync(max_age_days: int, max_size_mb: int) -> dict:
    """Apply the age and size bounds. Returns a summary for logging."""
    entries = _speech_files()
    if not entries:
        return {'scanned': 0, 'removed_files': 0, 'freed_mb': 0.0, 'remaining_mb': 0.0}

    now = time.time()
    max_age_seconds = max(0, max_age_days) * 86400
    max_size_bytes = max(0, max_size_mb) * 1024 * 1024

    removed_files = 0
    freed_bytes = 0

    if max_age_seconds > 0:
        survivors = []
        for mtime, files in entries:
            if now - mtime > max_age_seconds:
                freed_bytes += sum(f.stat().st_size for f in files if f.exists())
                removed_files += _remove(files)
            else:
                survivors.append((mtime, files))
        entries = survivors

    if max_size_bytes > 0:
        total = _total_bytes(entries)
        index = 0
        while total > max_size_bytes and index < len(entries):
            _, files = entries[index]
            size = sum(f.stat().st_size for f in files if f.exists())
            removed_files += _remove(files)
            total -= size
            freed_bytes += size
            index += 1
        entries = entries[index:]

    return {
        'scanned': len(entries),
        'removed_files': removed_files,
        'freed_mb': round(freed_bytes / 1024 / 1024, 1),
        'remaining_mb': round(_total_bytes(entries) / 1024 / 1024, 1),
    }


async def prune_speech_cache_once() -> dict:
    """Read the configured bounds and prune once."""
    max_age_days = await Config.get('audio.tts.cache_max_age_days')
    max_size_mb = await Config.get('audio.tts.cache_max_size_mb')

    try:
        max_age_days = int(max_age_days) if max_age_days is not None else DEFAULT_MAX_AGE_DAYS
    except (TypeError, ValueError):
        max_age_days = DEFAULT_MAX_AGE_DAYS
    try:
        max_size_mb = int(max_size_mb) if max_size_mb is not None else DEFAULT_MAX_SIZE_MB
    except (TypeError, ValueError):
        max_size_mb = DEFAULT_MAX_SIZE_MB

    # A zero or negative age disables the age rule; the same for size. Both zero
    # means "keep everything", which is a valid opt-out.
    return await asyncio.to_thread(prune_speech_cache_sync, max_age_days, max_size_mb)


async def periodic_speech_cache_cleanup():
    """Background loop; started from main.py lifespan."""
    # Let the app finish warming up before walking a possibly large directory;
    # a restart-heavy period would otherwise rescan it on every boot.
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while True:
        try:
            summary = await prune_speech_cache_once()
            if summary['removed_files']:
                log.info(
                    'Speech cache pruned: removed %s file(s), freed %s MB, %s MB remaining',
                    summary['removed_files'],
                    summary['freed_mb'],
                    summary['remaining_mb'],
                )
            else:
                log.debug('Speech cache prune: nothing to remove (%s MB)', summary['remaining_mb'])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Speech cache cleanup failed')
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
