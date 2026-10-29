# Multiple API keys per connection (round-robin / sticky)

One endpoint often hands out several API keys with the same quota model. Open
WebUI used to store exactly one key per connection, so the extra keys were
unusable. This document records what was added, why registering the same base URL
twice is **not** a substitute, and what the feature deliberately does not do.

## Why a second connection with the same URL does not work

Measured on the live instance before writing any code. The config accepts a
duplicate base URL — `api_base_urls` and `api_keys` are parallel arrays and
`api_configs` is keyed by position, not by URL — so it *looks* like the natural
workaround. It is not, because model discovery dedupes by model id:

```python
# backend/open_webui/routers/openai.py, get_merged_models
if model_id and model_id not in models:
    ...
    models[model_id] = merged
```

First connection wins. Proof from the run: with the same URL at index 2 (good key)
and index 5 (invalid key), the result was

```
models owned by good-key connection (idx 2): ['deepseek-v4.1-flash-1017']
models owned by bad-key  connection (idx 5): []
```

The duplicate served nothing, so it could not act as failover or as a second
quota. That is the reason the keys had to live on the connection itself.

## How it works

Keys are stored in `api_config['api_keys']` (a list). When the list has more than
one entry, `get_openai_connection()` picks one per request:

```
api_config['api_keys'] = ['sk-A', 'sk-B', 'sk-C']
api_config['api_key_strategy'] = 'round_robin' | 'sticky'
api_config['sticky_ttl'] = 300          # seconds, sticky only
```

`get_openai_connection()` is the single function all eight OpenAI call sites use
(chat completions, responses, model listing, model catalog, speech, token count,
and the rest), so rotation applies everywhere without touching each route.

### Strategies

| Strategy | Behaviour | Use when |
|---|---|---|
| `round_robin` (default) | Strict rotation, one key per request | Keys are interchangeable and the upstream tolerates interleaving |
| `sticky` | Same key for `sticky_ttl` seconds, then rotate | Upstream prompt caching, per-key rate-limit buckets, provider session affinity |

`sticky_ttl = 0` degrades to round-robin rather than pinning forever. The value is
clamped to `0..86400` so a typo cannot hold one key for a week.

The two strategies keep **separate cursors**. Sharing one would let a sticky
request advance the round-robin order, so mixing them (or switching mid-life)
would skip keys. A unit test pins this.

### Compatibility

A connection with no `api_keys` resolves to its single legacy key and the code
path is unchanged. Verified against the live config: all five existing connections
resolve to exactly one key each.

The form mirrors the first list entry into the legacy `key` field, so the collapsed
row in Settings, the `OPENAI_API_KEYS` array, and every consumer that still reads a
single key keep working. The list is authoritative when present — the legacy key is
**not** appended to it, because an admin who removed a key from the list would
otherwise find it resurrected.

## Verifying the keys

`POST /openai/verify` now tests every key in the list and reports each one:

```json
{
  "data": [...],
  "_key_report": [
    { "key": "sk-a1D...PCuI", "ok": true,  "detail": null },
    { "key": "sk-ROT...ey-A", "ok": false, "detail": "HTTP 401: {\"error\":{\"message\":\"Invalid token\"}}" }
  ]
}
```

Without this an admin who pastes five keys has no way to know which are dead, and
a dead key silently takes a share of traffic.

**A trap worth recording.** `_verify_single_connection` reports a rejected key by
*returning* a `JSONResponse` with a non-200 status, not by raising. The first
version of this loop only caught `HTTPException`, so it reported every key usable —
including two deliberately invalid keys that the upstream answered with 401. The
lesson generalises: when wrapping a helper that can signal failure either by
raising **or** by returning an error response, check both. A unit test now pins it.

## Measured behaviour on the live instance

Round-robin with `[real, dead, dead]`, observed through the chat path:

```
round robin: ['OK', 'DEAD', 'DEAD', 'OK', 'DEAD', 'DEAD']   # k1, k2, k3, k1, k2, k3
sticky(300): ['OK', 'OK', 'OK', 'OK', 'OK', 'OK']           # one key for the window
```

The repeating `OK, DEAD, DEAD` is the proof that all three keys are used in turn.
A single uniform result would have been ambiguous.

## Honest limitations

- **Rotation state is process-global.** With several worker processes each rotates
  independently, so the order is exact only for a single-process deployment (this
  one). Strict global ordering across replicas needs a shared store; that is not
  implemented, and pretending otherwise would be worse than saying so.
- **No health-based failover.** A key that starts returning 401 still gets its turn
  until an admin removes it. The strategy rotates; it does not measure success. The
  live test shows the consequence directly: one dead key in three produces a
  request failure every third call. Remove dead keys — `/openai/verify` tells you
  which ones they are.
- **No per-key usage accounting.** Nothing records which key served which request
  beyond the debug log line.
- **`/openai/models/{url_idx}` returns 500 for connections that declare
  `model_ids`.** Pre-existing and unrelated to rotation; it was the first thing
  tried when looking for an observable signal, which is why the proof above goes
  through the chat path instead.

## Files

| File | Role |
|---|---|
| `backend/open_webui/utils/api_key_rotation.py` | selection logic, pure and unit-tested |
| `backend/open_webui/routers/openai.py` | `get_openai_connection()` calls it; `/verify` reports per key |
| `src/lib/components/AddConnectionModal.svelte` | the "Multiple API Keys" form |

Tests:

| File | Covers |
|---|---|
| `docs/architecture/tests/test-api-key-rotation.py` | 48 assertions: normalisation, strategy parsing, round-robin order, sticky window, cursor isolation, masking |
| `docs/architecture/tests/test-verify-loop.py` | the per-key verification loop, including the returned-401 case that was a real bug |
| `docs/architecture/tests/test-rotation-chat.py` | end-to-end on the host: proves rotation through the chat path and restores everything |

Run the first two anywhere (`python3 <file>`). The third must run on the deploy host
because it drives the live API and touches `is_active`.
