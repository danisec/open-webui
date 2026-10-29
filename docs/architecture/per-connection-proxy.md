# Per-connection proxy support, with rotation

Each OpenAI-compatible connection can name its own proxy, or a **list of proxies
that rotates**. Only that connection's outbound requests use them; every other
connection is unaffected. SOCKS5, SOCKS4 and HTTP(S) are supported.

## Why this needed code rather than a setting

Open WebUI talks to providers through `aiohttp`. aiohttp has **no SOCKS support**:
its only connectors are `TCPConnector` and `UnixConnector`. A `socks5://` URL
reaching `trust_env` is treated as an *HTTP* proxy, so aiohttp speaks HTTP to a
SOCKS port. Measured inside the running image, against a real proxy:

```
greeting  b'\x05\x02'   -> auth required
auth      b'\x01\x00'   -> accepted
CONNECT   b'\x05\x00…'  -> granted
```

so the proxy was fine. What aiohttp did with it:

```
ClientResponseError: 400, "Bad status line: Expected HTTP/, RTSP/ or ICE/:
                      b'\x05\xff'"
```

`\x05\xff` is the SOCKS5 reply parsed as HTTP. The fix is
`aiohttp_socks.ProxyConnector`, verified in a clean container with the pinned
versions:

```
aiohttp 3.14.3 + aiohttp-socks 0.10.1
  via SOCKS5 -> HTTP 401 (reached OpenAI)
  exit IP    -> 44.242.204.185
```

## Why one session per proxy

A `ClientSession` owns exactly one connector, and a connector owns its proxy
settings. **One shared session therefore cannot serve two connections with
different proxies.** `session_pool.get_session(proxy)` keeps one cached session per
distinct normalised proxy URL.

Callers that pass no proxy receive the original session and behave exactly as
before, which matters because `get_session()` has 57 call sites across the backend.

## Rotation

A list in `api_config['proxies']` rotates one proxy per request. This exists
because an upstream that rate-limits or bans by **source address** can only be
satisfied by changing the source address; a single proxy just moves the problem.

```json
{
  "proxies": [
    "socks5://ibx1:***@100.106.8.115:1080",
    "socks5://ibx2:***@100.86.76.1:1080",
    "socks5://ibx3:***@100.97.236.81:1080",
    "socks5://ibx4:***@100.74.185.111:1080",
    "socks5://ibx5:***@100.119.160.46:1080"
  ],
  "proxy_strategy": "round_robin",
  "proxy_sticky_ttl": 300
}
```

| Strategy | Behaviour | Use when |
|---|---|---|
| `round_robin` (default) | Strict rotation, one proxy per request | Every proxy has equal capacity; spread load |
| `sticky` | Same proxy for `proxy_sticky_ttl` seconds | Upstream session affinity, or a proxy billed per connection |

The cursor logic is **reused from the API-key rotation module** rather than
duplicated, in a separate state namespace (`proxy:`). That matters: the two
strategies keep separate cursors, and mixing proxy and key rotation must not
advance each other. A unit test pins the interleaving.

`proxy` (single) and `proxies` (list) both work. The list wins when present; a
single entry is stored in the legacy `proxy` field, so one-proxy connections
store exactly what they did before rotation existed.

## Configuration

Settings → Connections → OpenAI API → ⚙ on a connection → **Proxy (optional)**.

```
socks5://user:password@host:1080
socks4://user:password@host:1080
http://user:password@host:8080
https://user:password@host:8443
```

Click **Add proxy** for a second entry; the rotation controls appear once there is
more than one.

### Accepted input shapes

| Pasted | Normalised |
|---|---|
| `socks5://u:p@h:1080:1080` | `socks5://u:p@h:1080` (doubled port collapsed) |
| `u:p@h:1080` | `socks5://u:p@h:1080` (bare form assumes SOCKS5) |
| `socks5://h` | `socks5://h:1080` (default SOCKS port) |
| `http://h` | `http://h:8080` (default HTTP port) |
| `socks5://u:p@ss@h:1080` | `socks5://u:p%40ss@h:1080` (`@` encoded) |
| newline- or comma-separated list | split into entries |

The doubled-port case is not hypothetical: it is the exact shape that was pasted
when this feature was requested (`…@100.106.8.115:1080:1080`).

## Which requests use it

Every OpenAI path reads the proxy through `connection_proxy()`:

| Path | Function |
|---|---|
| chat completions | `generate_chat_completion` |
| responses API | `generate_openai_response` |
| embeddings | `generate_embeddings` |
| speech / TTS | `speech` |
| Anthropic token count | `count_anthropic_tokens` |
| model listing | `get_models`, `send_get_request` |
| model management | `send_model_management_request` |
| Verify Connection | `_verify_single_connection` |

Each of these resolves the proxy **once per request**. An earlier version called
`connection_proxy()` twice in four places (once for the session, once for the
request kwargs), which with rotation would advance the cursor twice and could pair
a session built for proxy A with a request proxy of B.

`/verify` returns `_proxy_report`, including the rotation:

```json
{
  "_proxy_report": {
    "configured": true,
    "rotation": {
      "count": 5,
      "strategy": "round_robin",
      "sticky_ttl": 300,
      "proxies": ["socks5://ibx1:***@100.106.8.115:1080", "..."]
    }
  }
}
```

An invalid proxy is reported as `Invalid proxy configuration: …` with HTTP 400,
**before** any request is made — so it reads as a proxy problem, not a bad key.

## Security notes

- Passwords are masked in every log line and in `/verify` (`user:***@host:port`).
- `trust_env` is forced **off** on a proxied session. Otherwise `HTTPS_PROXY` from
  the environment would also apply and the two settings would silently conflict.
- Credentials are percent-encoded when normalised, so a `@` or `:` in a password
  cannot silently change which host is contacted.

## Honest limitations

- **Only the OpenAI-compatible path.** Ollama, web search/retrieval, OAuth and the
  internal socket transport have their own request paths and do not honour these
  fields.
- **DNS is resolved at the proxy** (`rdns=True`). That is deliberate: when an
  upstream bans by source address, resolving locally would leak the origin address
  through the DNS query. A proxy that cannot resolve names will fail.
- **No health-based failover.** A dead proxy keeps its turn until it fails, and a
  failed proxy fails that request. There is no silent fallback to a direct
  connection: bypassing a proxy an admin configured would be worse than failing.
  Remove dead proxies; `/verify` tells you which work.
- **Rotation state is process-global**, like key rotation. Exact for this
  single-process deployment; strict ordering across replicas would need a shared
  store.
- **Sessions are never evicted.** Each distinct proxy URL keeps one session for the
  process lifetime.

## Files

| File | Role |
|---|---|
| `backend/open_webui/utils/proxy_config.py` | normalisation, validation, masking, list resolution, rotation |
| `backend/open_webui/utils/session_pool.py` | one cached session per proxy |
| `backend/open_webui/routers/openai.py` | `connection_proxy()` + all call sites |
| `src/lib/components/AddConnectionModal.svelte` | Proxy field, list, rotation controls |
| `backend/requirements.txt` | `aiohttp-socks==0.10.1` |

Tests:

| File | Covers |
|---|---|
| `docs/architecture/tests/test-proxy-support.py` | normalisation, masking, socks/http dispatch, session-per-proxy, missing dependency |
| `docs/architecture/tests/test-proxy-rotation.py` | list normalisation, round-robin order, per-connection cursors, sticky window, key/proxy cursor independence, degraded configs |
| `docs/architecture/tests/test-proxy-rotation-e2e.py` | live: attaches five real proxies and verifies through them |
| `docs/architecture/tests/prove-rotation.py` | live: prints the exit IP per request, proving rotation |

## Measured result on this deployment

The five instabox egress paths, measured from inside the container:

| Proxy | Host | Exit IP |
|---|---|---|
| ibx1 | 100.106.8.115 | 44.242.204.185 |
| ibx2 | 100.86.76.1 | 44.245.200.65 |
| ibx3 | 100.97.236.81 | 44.244.119.140 |
| ibx4 | 100.74.185.111 | 35.93.162.163 |
| ibx5 | 100.119.160.46 | 35.88.80.47 |

**Five distinct exit addresses**, which is what makes rotation useful for per-IP
limits.

Rotation observed request by request:

```
request  1: ibx1 exit=44.242.204.185
request  2: ibx2 exit=44.245.200.65
request  3: ibx3 exit=44.244.119.140
request  4: ibx4 exit=35.93.162.163
request  5: ibx5 exit=35.88.80.47
request  6: ibx1 exit=44.242.204.185      <- cycle repeats
...
distinct exit IPs observed: 5 of 10 requests
```

And the practical effect — this host is IP-banned by `api.sakoo.biz.id`:

```
direct   GET /v1/models  -> 403 model_abuse_banned
proxied  GET /v1/models  -> 200, 2 models
/verify through 5 rotating proxies: 6/6 HTTP 200
```

## A bug this feature exposed, and two it fixed

1. **`/verify` returned HTTP 500** on the first live run. `urlsplit().port` raises
   `ValueError` for the doubled port `1080:1080`, and `_mask()` did not catch it.
   Since `describe_proxy()` runs on every verify, one malformed port broke the
   whole endpoint. `_mask()` is now total (it is a logging helper and must never
   raise). Regression tests cover `1080:1080`, `:notaport`, `:1:2`.

2. **`/verify` ignored the proxy list entirely.** It read only
   `api_config['proxy']`, so with a `proxies` list configured it verified over a
   **direct** connection and reported the upstream's IP ban — six 403s while the
   proxies were perfectly healthy. It now resolves the proxy through
   `connection_proxy()`, the same function a real request uses. This is the reason
   the first rotation run showed `0/6` and the second showed `6/6`.


## Why this needed code rather than a setting

Open WebUI talks to providers through `aiohttp`. aiohttp has **no SOCKS support**:
its only connectors are `TCPConnector` and `UnixConnector`. A `socks5://` URL
reaching `trust_env` is treated as an *HTTP* proxy, so aiohttp speaks HTTP to a
SOCKS port. Measured inside the running image, against a real proxy:

```
greeting  b'\x05\x02'   -> auth required
auth      b'\x01\x00'   -> accepted
CONNECT   b'\x05\x00…'  -> granted
```

so the proxy was fine. What aiohttp did with it:

```
ClientResponseError: 400, "Bad status line: Expected HTTP/, RTSP/ or ICE/:
                      b'\x05\xff'"
```

`\x05\xff` is the SOCKS5 reply parsed as HTTP. The fix is
`aiohttp_socks.ProxyConnector`, verified in a clean container with the pinned
versions:

```
aiohttp 3.14.3 + aiohttp-socks 0.10.1
  via SOCKS5 -> HTTP 401 (reached OpenAI)
  exit IP    -> 44.242.204.185
```

## Why one session per proxy

A `ClientSession` owns exactly one connector, and a connector owns its proxy
settings. **One shared session therefore cannot serve two connections with
different proxies.** `session_pool.get_session(proxy)` keeps one cached session per
distinct normalised proxy URL.

Callers that pass no proxy receive the original session and behave exactly as
before, which matters because `get_session()` has 57 call sites across the backend.

## Configuration

Settings → Connections → OpenAI API → ⚙ on a connection → **Proxy (optional)**.

```
socks5://user:password@host:1080
socks4://user:password@host:1080
http://user:password@host:8080
https://user:password@host:8443
```

Stored as `api_config['proxy']`. The field is omitted from the payload when blank,
so a connection without a proxy stores exactly what it did before this feature
existed.

### Accepted input shapes

| Pasted | Normalised |
|---|---|
| `socks5://u:p@h:1080:1080` | `socks5://u:p@h:1080` (doubled port collapsed) |
| `u:p@h:1080` | `socks5://u:p@h:1080` (bare form assumes SOCKS5) |
| `socks5://h` | `socks5://h:1080` (default SOCKS port) |
| `http://h` | `http://h:8080` (default HTTP port) |
| `socks5://u:p@ss@h:1080` | `socks5://u:p%40ss@h:1080` (`@` encoded) |

The doubled-port case is not hypothetical: it is the exact shape that was pasted
when this feature was requested (`…@100.106.8.115:1080:1080`).

## Which requests use it

Every OpenAI path reads `api_config['proxy']`:

| Path | Function |
|---|---|
| chat completions | `generate_chat_completion` |
| responses API | `generate_openai_response` |
| embeddings | `generate_embeddings` |
| speech / TTS | `speech` |
| Anthropic token count | `count_anthropic_tokens` |
| model listing | `get_models`, `send_get_request` |
| model management | `get_model_management_connection` callers |
| Verify Connection | `_verify_single_connection` |

`/verify` now returns `_proxy_report`:

```json
{
  "_proxy_report": {
    "configured": true,
    "raw_masked": "socks5://ibx1:***@100.106.8.115:1080",
    "normalized": "socks5://ibx1:***@100.106.8.115:1080",
    "error": null
  }
}
```

An invalid proxy is reported as `Invalid proxy configuration: …` with HTTP 400,
**before** any request is made — so it reads as a proxy problem, not a bad key.

## Security notes

- Passwords are masked in every log line and in `/verify` (`user:***@host:port`).
- `trust_env` is forced **off** on a proxied session. Otherwise `HTTPS_PROXY` from
  the environment would also apply and the two settings would silently conflict.
- Credentials are percent-encoded when normalised, so a `@` or `:` in a password
  cannot silently change which host is contacted.

## Honest limitations

- **Only the OpenAI-compatible path.** Ollama, web search/retrieval, OAuth and the
  internal socket transport have their own request paths and do not honour
  `api_config['proxy']`.
- **DNS is resolved at the proxy** (`rdns=True`). That is deliberate: when an
  upstream bans by source address, resolving locally would leak the origin address
  through the DNS query. It does mean a proxy that cannot resolve names will fail.
- **One proxy per connection.** There is no pool of proxies to rotate across for a
  single connection; use separate connections if you need that.
- **A dead proxy fails the whole connection.** There is no automatic fallback to a
  direct connection — silently bypassing a proxy an admin configured would be
  worse than failing.
- **Sessions are never evicted.** Each distinct proxy URL keeps one session for the
  process lifetime. With a handful of connections that is fine; a deployment that
  rotated through thousands of proxy URLs would need eviction.

## Files

| File | Role |
|---|---|
| `backend/open_webui/utils/proxy_config.py` | normalisation, validation, masking, request kwargs |
| `backend/open_webui/utils/session_pool.py` | one cached session per proxy |
| `backend/open_webui/routers/openai.py` | `connection_proxy()` + all call sites |
| `src/lib/components/AddConnectionModal.svelte` | the Proxy field |
| `backend/requirements.txt` | `aiohttp-socks==0.10.1` |

Tests: `docs/architecture/tests/test-proxy-support.py` — 50+ assertions covering
normalisation, credential encoding, masking, socks/http dispatch, session-per-proxy
caching, concurrent creation, and the missing-dependency error. Run it anywhere
`aiohttp` is installed; it loads the two modules directly so it does not need the
full application dependency set.

## Measured result on this deployment

The proxy is not merely a convenience here — it fixes a real failure. `api.sakoo.biz.id`
bans by source address, and the deploy host is currently blocked. Measured inside the
running v28b container, same key, same URL, one variable:

```
GET https://api.sakoo.biz.id/v1/models
  direct   HTTP 403  code=model_abuse_banned
  proxied  HTTP 200  models=['bansos/deepseek-v4.1-flash', 'bansos/gpt-6-luna']

exit IP
  direct   168.110.223.198   (blocked)
  proxied  44.242.204.185    (clean)

pool state
  cached session: direct
  cached session: socks5://ibx1:***@100.106.8.115:1080
```

`/openai/verify` through the same connection returns the proxy report and the model
list, which it could not do directly:

```json
{
  "_proxy_report": {
    "configured": true,
    "normalized": "socks5://ibx1:***@100.106.8.115:1080",
    "error": null
  },
  "data": [{"id": "bansos/deepseek-v4.1-flash"}, {"id": "bansos/gpt-6-luna"}]
}
```

An invalid proxy is rejected before any request is made:

```
POST /openai/verify  {proxy: "ftp://bad:21"}  -> HTTP 400
  Invalid proxy configuration: Unsupported proxy scheme 'ftp'.
  Use one of: http, https, socks4, socks4a, socks5, socks5h
```

## A bug this feature exposed, and the fix

`/openai/verify` returned **HTTP 500** on the first live run with the real proxy.
Cause: `urllib.parse.urlsplit(...).port` raises `ValueError` for the doubled port
`1080:1080`, and `_mask()` did not catch it. Since `describe_proxy()` runs on every
verify, one malformed port broke the entire endpoint — including the per-key report
that already worked.

Fixed by making `_mask()` total (it is a logging helper and must never raise) and by
masking the raw value first so an invalid proxy is still displayed. Regression tests
now cover `1080:1080`, `:notaport` and `:1:2`.


## Applied configuration and its current state

The five instabox proxies are now attached to the `api.sakoo.biz.id` connection
(round-robin), read from Doppler project `instabox` config `prd`
(`SOCKS5_IBX1..5_USER` / `SOCKS5_IBX5_PASS`).

Per-proxy health, measured after the change:

```
ibx1 /v1/models  -> HTTP 200     ibx1 chat -> HTTP 429 accounts_rate_limited
ibx2 /v1/models  -> HTTP 200     ibx2 chat -> HTTP 429 accounts_rate_limited
ibx3 /v1/models  -> HTTP 200     ibx3 chat -> HTTP 429 accounts_rate_limited
ibx4 /v1/models  -> HTTP 200     ibx4 chat -> HTTP 429 accounts_rate_limited
ibx5 /v1/models  -> HTTP 200     ibx5 chat -> HTTP 429 accounts_rate_limited
```

Read this carefully, because the two halves mean different things:

- **`/v1/models` returns 200 through every proxy.** Before the proxies, this host
  received `403 model_abuse_banned`. So the IP ban is gone and the tunnels work.
- **Chat returns `429 accounts_rate_limited` through every proxy.** That is the
  *provider's own accounts* cooling down (`retryAfterMs` was ~3 hours when
  measured). No proxy can fix it: the limit is on their accounts, not on the
  caller's address. It clears on its own.

So the proxy layer is doing its job; the remaining error is upstream capacity.

## Rollback

The previous config is saved on the deploy host as
`/tmp/openai-config-before-proxy-<epoch>.json`. Re-apply it with:

```bash
python3 - <<'PY'
import json, urllib.request
KEY = "sk-a0cc748d9c9e4851b2863b5e909180f2"
before = json.load(open("/tmp/openai-config-before-proxy-1793022815.json"))
payload = {
    "ENABLE_OPENAI_API": True,
    "OPENAI_API_BASE_URLS": before["OPENAI_API_BASE_URLS"],
    "OPENAI_API_KEYS": before["OPENAI_API_KEYS"],
    "OPENAI_API_CONFIGS": before["OPENAI_API_CONFIGS"],
}
req = urllib.request.Request(
    "http://127.0.0.1:3000/openai/config/update",
    data=json.dumps(payload).encode(),
    headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
)
print(urllib.request.urlopen(req, timeout=120).status)
PY
```

Or remove the proxies from just that connection in the UI.

## howtofix connection: proxies applied later, and why that needed care

`https://api.howtofix.id/v1` was left direct when the sakoo proxies were applied,
because it never showed a per-IP limit and it is the **primary candidate for the
`deepseek-v4.1-flash` rotator alias**. Proxying a healthy path adds a failure point
to the alias. It was applied afterwards on request, with these safeguards:

1. **Pre-flight before writing.** Each proxy had to reach howtofix's `/v1/models`
   with HTTP 200 first. If that had failed, nothing would have been written — the
   script aborts before touching the config.
2. **Only connection 2 changed.** The script diffs every other connection against
   the pre-change snapshot and reports `other connections changed: none`.
3. **Alias regression check after.** Stream and non-stream both re-tested, because
   breaking howtofix would break the alias.

Measured result:

```
pre-flight /verify with proxies -> HTTP 200
  models: ['deepseek-v4.1-flash', 'deepseek-v4.1-flash-1017']

applied -> HTTP 200, 5 proxies round_robin
other connections changed : none
base URLs unchanged       : True
keys unchanged            : True

post-check /verify -> HTTP 200, rotation: 5 proxies
chat through the proxies -> 200, 200, 200, 200  (reply 'OK')

alias deepseek-v4.1-flash
  non-stream -> HTTP 200 reply='OK'
  stream     -> HTTP 200 events=4 text='OK'
```

Rotation observed per request on this connection:

```
request 1: ibx1 exit=44.242.204.185
request 2: ibx2 exit=44.245.200.65
request 3: ibx3 exit=44.244.119.140
request 4: ibx4 exit=35.93.162.163
request 5: ibx5 exit=35.88.80.47
request 6: ibx1 exit=44.242.204.185
distinct exits: 5 of 6
```

Rollback for this change:

```bash
python3 /tmp/rollback-config.py /tmp/openai-config-before-howtofix-proxy-1793023095.json
```

`rollback-config.py` re-reads the config after writing and asserts the restore was
exact, because a rollback that silently half-applies is worse than none.
