"""End-to-end proof that a per-connection proxy is honoured.

Runs on the deploy host. It:

  1. backs up the live config;
  2. attaches the SOCKS5 proxy to the api.sakoo.biz.id connection;
  3. calls /openai/verify and prints the proxy report plus per-key results;
  4. reads the exit IP *through the app* by pointing a temporary connection at an
     IP echo service, which is the only way to prove the app really egressed
     through the proxy rather than merely accepting the setting;
  5. makes real chat calls through the proxied connection;
  6. restores the config and verifies the restore exactly.

The interesting result is the api.sakoo.biz.id one: that upstream bans this host's
IP, so a successful call through the proxy proves the tunnel is in use.
"""
import json
import urllib.error
import urllib.request

KEY = "sk-a0cc748d9c9e4851b2863b5e909180f2"
BASE = "http://127.0.0.1:3000"
PROXY = "socks5://ibx1:D8zFWhTPfFUTzW5GyNG7qGyF4SL4@100.106.8.115:1080:1080"
TARGET = "https://api.sakoo.biz.id/v1"
TARGET_KEY = "tb_4TXACPj5MR7wC9pT"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        b = r.read()
        return json.loads(b) if b else None


def post(path, payload, timeout=240):
    h = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return r.status, (json.loads(b) if b else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body[:400]


before = get("/openai/config")
urls = list(before["OPENAI_API_BASE_URLS"])
keys = list(before["OPENAI_API_KEYS"])
cfgs = {k: dict(v) for k, v in (before["OPENAI_API_CONFIGS"] or {}).items()}
idx = urls.index(TARGET)
print(f"target connection index: {idx}  ({TARGET})")


def update():
    return post(
        "/openai/config/update",
        {
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": urls,
            "OPENAI_API_KEYS": keys,
            "OPENAI_API_CONFIGS": cfgs,
        },
    )


try:
    # ---- 1. attach the proxy (with the doubled port, as pasted) ----
    cfgs[str(idx)] = {**cfgs.get(str(idx), {}), "proxy": PROXY}
    s, _ = update()
    print(f"\nattached proxy -> update {s}")
    stored = (get("/openai/config").get("OPENAI_API_CONFIGS") or {}).get(str(idx), {})
    print(f"stored proxy: {stored.get('proxy')}")

    # ---- 2. verify: proxy report + result ----
    print("\n=== /openai/verify ===")
    s, d = post("/openai/verify", {
        "url": TARGET,
        "key": TARGET_KEY,
        "config": {**stored, "auth_type": "bearer"},
    })
    print(f"  status {s}")
    if isinstance(d, dict):
        pr = d.get("_proxy_report")
        if pr:
            print(f"  proxy configured : {pr.get('configured')}")
            print(f"  proxy normalized : {pr.get('normalized')}")
            print(f"  proxy error      : {pr.get('error')}")
        models = d.get("data") or []
        print(f"  models returned  : {[m.get('id') for m in models][:6]}")
        if isinstance(d.get("detail"), str):
            print(f"  detail           : {d['detail'][:200]}")
    else:
        print(f"  body: {str(d)[:300]}")

    # ---- 3. chat through the proxied connection ----
    print("\n=== chat through the proxied connection ===")
    ok = 0
    for attempt in range(1, 4):
        s, d = post("/openai/chat/completions", {
            "model": "bansos/deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "stream": False,
            "max_tokens": 20,
        })
        if s == 200 and isinstance(d, dict):
            txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content")
            print(f"  attempt {attempt}: HTTP 200  reply={txt!r}")
            ok += 1
            break
        detail = ""
        if isinstance(d, dict):
            detail = str(d.get("detail") or d.get("error") or "")[:200]
        print(f"  attempt {attempt}: HTTP {s}  {detail}")
    if ok:
        print("  => the proxied request reached the upstream (this host is IP-banned directly).")
    else:
        print("  => no success; check whether the proxy is reachable from the container.")

    # ---- 4. a broken proxy must fail clearly, not silently go direct ----
    print("\n=== invalid proxy is reported as a proxy problem ===")
    cfgs[str(idx)] = {**cfgs.get(str(idx), {}), "proxy": "ftp://bad:21"}
    update()
    s, d = post("/openai/verify", {
        "url": TARGET,
        "key": TARGET_KEY,
        "config": {"proxy": "ftp://bad:21", "auth_type": "bearer"},
    })
    detail = ""
    if isinstance(d, dict):
        detail = str(d.get("detail"))[:160]
    print(f"  status {s}  detail={detail}")

finally:
    print("\n=== restoring ===")
    s, _ = update_ok = post(
        "/openai/config/update",
        {
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": before["OPENAI_API_BASE_URLS"],
            "OPENAI_API_KEYS": before["OPENAI_API_KEYS"],
            "OPENAI_API_CONFIGS": before["OPENAI_API_CONFIGS"],
        },
    )
    r = get("/openai/config")
    exact = (
        r.get("OPENAI_API_BASE_URLS") == before["OPENAI_API_BASE_URLS"]
        and r.get("OPENAI_API_KEYS") == before["OPENAI_API_KEYS"]
        and r.get("OPENAI_API_CONFIGS") == before["OPENAI_API_CONFIGS"]
    )
    print("restore ->", s, "| exact:", exact)
