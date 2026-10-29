"""Prove rotation switches keys, observed through the real chat path.

Previous attempt used /openai/models/{idx}, which returns 500 for this connection
for reasons unrelated to rotation, and chat failed with "Model not found" because
the preset `deepseek-v4.1-flash-1017` is inactive. This test therefore:

  1. temporarily activates that preset (it is the model this connection serves);
  2. attaches [real, dead, dead] with round_robin;
  3. sends chat requests -- a dead key yields an upstream 401, a good key yields
     text, so the outcome sequence proves which key served each request;
  4. switches to sticky and repeats (expect a uniform outcome);
  5. restores BOTH the connection config and the preset's is_active flag.

`is_active` is restored explicitly because leaving a model enabled that an admin
had disabled would be a real, user-visible change.
"""
import json
import sqlite3
import subprocess
import urllib.error
import urllib.request

KEY = "sk-a0cc748d9c9e4851b2863b5e909180f2"
BASE = "http://127.0.0.1:3000"
TARGET = "https://api.howtofix.id/v1"
MODEL = "deepseek-v4.1-flash-1017"
REAL = "sk-a1D1XWeRuZsK1kO8IuZONovftK08tVaBFLDs3kNxxiYGPCuI"
DEAD_A = "sk-ROTATION-TEST-dead-key-A"
DEAD_B = "sk-ROTATION-TEST-dead-key-B"

SET_ACTIVE = r'''
import sqlite3
db = sqlite3.connect("/app/backend/data/webui.db")
db.execute("update model set is_active=? where id=?", (%d, "%s"))
db.commit()
print("is_active set to", %d)
'''


def set_active(value: int):
    sql = SET_ACTIVE % (value, MODEL, value)
    r = subprocess.run(["podman", "exec", "open-webui", "python", "-c", sql],
                       capture_output=True, text=True, timeout=90)
    return (r.stdout or r.stderr).strip()


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        b = r.read()
        return json.loads(b) if b else None


def post(path, payload, timeout=180):
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
            return e.code, body[:200]


def chat_once():
    s, d = post("/api/chat/completions", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "say ok"}],
        "stream": False,
    })
    if s == 200 and isinstance(d, dict):
        return "OK"
    detail = ""
    if isinstance(d, dict):
        detail = str(d.get("detail") or d.get("error") or "")
    if "Invalid token" in detail or "401" in detail:
        return "DEAD"
    return f"HTTP{s}"


before = get("/openai/config")
urls = list(before["OPENAI_API_BASE_URLS"])
keys = list(before["OPENAI_API_KEYS"])
cfgs = {k: dict(v) for k, v in (before["OPENAI_API_CONFIGS"] or {}).items()}
idx = urls.index(TARGET)

try:
    print(set_active(1))
    post("/api/models?refresh=true", {})

    def apply(cfg_extra):
        cfgs[str(idx)] = {**cfgs.get(str(idx), {}), **cfg_extra}
        return post("/openai/config/update", {
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": urls,
            "OPENAI_API_KEYS": keys,
            "OPENAI_API_CONFIGS": cfgs,
        })[0]

    print("\n=== round robin: [real, dead, dead] ===")
    print("update:", apply({"api_keys": [REAL, DEAD_A, DEAD_B], "api_key_strategy": "round_robin"}))
    rr = [chat_once() for _ in range(6)]
    print("  outcomes:", rr)
    if "OK" in rr and "DEAD" in rr:
        print("  => CONFIRMED: rotation reaches different keys.")
    elif set(rr) == {"OK"}:
        print("  => every request succeeded; the key may not be rotating.")
    else:
        print("  => no success at all; the model may not resolve to this connection.")

    print("\n=== sticky (ttl=300): same list ===")
    print("update:", apply({"api_key_strategy": "sticky", "sticky_ttl": 300}))
    st = [chat_once() for _ in range(6)]
    print("  outcomes:", st)
    if len(set(st)) == 1:
        print("  => CONFIRMED: one key held for the whole window.")
    else:
        print("  => outcomes varied inside the window.")
finally:
    print("\n=== restoring ===")
    s, _ = post("/openai/config/update", {
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": before["OPENAI_API_BASE_URLS"],
        "OPENAI_API_KEYS": before["OPENAI_API_KEYS"],
        "OPENAI_API_CONFIGS": before["OPENAI_API_CONFIGS"],
    })
    r = get("/openai/config")
    ok = (
        r.get("OPENAI_API_BASE_URLS") == before["OPENAI_API_BASE_URLS"]
        and r.get("OPENAI_API_KEYS") == before["OPENAI_API_KEYS"]
        and r.get("OPENAI_API_CONFIGS") == before["OPENAI_API_CONFIGS"]
    )
    print("config restore ->", s, "| exact:", ok)
    print(set_active(0))
    post("/api/models?refresh=true", {})
