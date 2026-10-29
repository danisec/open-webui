# Why the `deepseek-v4.1-flash` alias failed, and the fix

## The message the user saw

```
This address is temporarily banned for this API key after too many requests for
models this key cannot use. Wait for the ban to lift, then list models to see
which ones this key may call.
```

This string exists in **no** part of the stack: not Open WebUI, not this fork, not
`model-rotator.py`, not 9router, not any Function stored in the database. It is
passed through verbatim from the upstream provider (`cartethyia`, fronting
`api.sakoo.biz.id`). Open WebUI only forwards `error.message`
(`backend/open_webui/events.py:1271-1304`, then `main.py:1737`).

Because the request failed before the first token, nothing was persisted: chat
`3b01dde7-8398-4d62-84c4-7122138e5219` contains exactly one user message ("hai")
and no assistant reply. That is the expected consequence of a pre-stream error,
not a storage bug.

## Root cause: the alias asked for a model the key may not call

The alias had a single candidate:

```json
"deepseek-v4.1-flash": {
  "priority": ["@api/deepseek-v4.1-flash-1017"],
  "strategy": "round-robin"
}
```

`@api` resolves to `api.sakoo.biz.id`, and its key only authorises two models:

```
bansos/deepseek-v4.1-flash   (ctx 1,000,000)
bansos/gpt-6-luna
```

The alias asked for `deepseek-v4.1-flash-1017`, which that key is **not** allowed
to call. The upstream answered 404 and counted it as abuse:

```
HTTP 404  "Model 'deepseek-v4.1-flash-1017' is not available to this API key.
           Warning 1/10 — 9 more invalid requests will temporarily ban this IP
           for this API key."
```

Every request to the alias was one strike. After ~10, the IP was banned, and the
alias then returned the 403 the user saw. The rotator log shows the escalation:

```
12:21:40  failover(stream): -> @api/deepseek-v4.1-flash-1017 got 404, trying next
12:22:04  failover(stream): -> @api/bansos/deepseek-v4.1-flash got 429, trying next
12:24:13  request error: all candidates failed for deepseek-v4.1-flash: (403, ...)
12:26:30  config written: {'deepseek-v4.1-flash': ['@api/deepseek-v4.1-flash-1017']}
```

The 12:26:30 line is the loop that made it self-perpetuating: `gen-model-groups.timer`
runs every 2 minutes, re-adopted the bad candidate from the DB, and dropped
`@api/bansos/deepseek-v4.1-flash` because it sat in the `removed` list.

## The ban is per IP, not per key

Measured with the same key from two addresses:

| Address | `GET /v1/models` | `POST` chat |
|---|---|---|
| `182.253.56.221` (workstation) | **200** | 429 `accounts_rate_limited` |
| `168.110.223.198` (deploy host) | **403** `model_abuse_banned` | **403** `model_abuse_banned` |

The key is valid; the deploy host's IP is blocked. The 429 on the unblocked
address is a separate, temporary condition: every account serving that model is
cooling down (`retryAfterMs` ≈ 3 hours).

## The fix applied

`~/.local/state/model-groups.json`:

```json
"deepseek-v4.1-flash": {
  "priority": [
    "@howtofix/deepseek-v4.1-flash-1017",
    "@api/bansos/deepseek-v4.1-flash"
  ],
  "removed": [
    "@100/deepseek-v4.1-flash",
    "@bansosin/glm-5.3-flash-jb",
    "@bansosin/deepseek-v4.1-flash-1017-jb"
  ],
  "strategy": "priority",
  "weights": {}
}
```

plus a new upstream:

```json
"_upstreams": {
  "howtofix": {
    "url": "https://api.howtofix.id/v1",
    "key": "sk-a1D1...PCuI"
  }
}
```

Measured before choosing it — `api.howtofix.id` answers 200 for **both** model ids:

```
POST deepseek-v4.1-flash-1017  -> 200  2.18s  "OK"  16->1 tokens
POST deepseek-v4.1-flash       -> 200  2.16s  "OK"  16->1 tokens
POST big-pickle                -> timeout 90s (avoid this model)
```

### Why `strategy` changed to `priority`

Round-robin with `weights: {"@api/bansos/deepseek-v4.1-flash": 3}` would send
three of every four requests to an upstream whose accounts are cooling down for
~3 hours. `priority` keeps the proven-healthy upstream first and only falls back
when it actually fails.

## Two traps found while applying it

**1. `removed` is keyed on the bare model id, not on `@upstream/model`.** The
first attempt added `@api/deepseek-v4.1-flash-1017` to `removed` to keep the bad
candidate out. Both `gen-model-groups.py` and the rotator compare with
`bare_id()`, so `deepseek-v4.1-flash-1017` also matched the intended
`@howtofix/deepseek-v4.1-flash-1017` — the primary candidate would have been
silently dropped on the next timer run. The bad candidate does not need to be in
`removed` at all: the reconciler only re-adopts ids that exist in the `model`
table, and `@api/deepseek-v4.1-flash-1017` is not one of them.

**2. The admin API cannot clear `removed`.** `PUT /v1/config/aliases/<id>` accepts
only `name`, `priority`, `strategy` and `weights`. Since a `removed` entry always
wins over `priority`, un-removing `@api/bansos/deepseek-v4.1-flash` required
editing the file directly. The file is bind-mounted, so it is written in place
(`os.replace` fails with `EBUSY`).

## Verification

```
reconciler:            "no changes"          -> the 2-minute timer will not rewrite it
rotator non-stream:    HTTP 200  "OK"  model=deepseek-v4.1-flash-1017
rotator stream:        HTTP 200  events=4  text="OK"
Open WebUI non-stream: HTTP 200  reply="OK"
Open WebUI stream:     HTTP 200  events=4  text="OK"
3 consecutive stream rounds: 200, 200, 200
open-webui health:     200
```

Fallback behaviour was tested by temporarily reducing `priority` to the `@api`
candidate alone. It returned `403 model_abuse_banned`, which is the honest current
state of that upstream from this host, not a rotation failure. Config was restored
immediately.

## Remaining state, stated plainly

- **`@api` is unusable from the deploy host until the ban lifts.** It is kept as
  the second candidate because it will recover on its own; while banned it fails
  over to howtofix, which is first in the list anyway.
- **`bansosin.space` returns 521** (Cloudflare: origin down) and
  **`xcartethyia.risun.web.id` returns 530** (Cloudflare 1033: tunnel down). Both
  are referenced by `glm-5.3-flash`, which therefore still cannot serve requests.
- **`glm-5.3-flash` has an empty candidate list.** Its display name matches no
  active model, so the reconciler adopts nothing.
- **`big-pickle` on howtofix times out** after 90s; do not use it.

## Rollback

```bash
cp /home/ubuntu/.local/state/model-groups.json.bak.1793018626 \
   /home/ubuntu/.local/state/model-groups.json
```

The rotator reloads on mtime change, so no restart is needed.
