"""Unit test for the per-key verification loop.

`_verify_single_connection` signals a rejected key by RETURNING a non-200 response
object, not by raising. The first version of the loop only caught HTTPException, so
it reported every key as usable -- including keys the upstream answered with 401.
That bug was found on the live instance (3/3 "usable" for two deliberately invalid
keys) and this test pins the fix.

Runs standalone: the loop is reproduced with the same shape as the route so it can
be exercised without FastAPI.
"""
import sys

class HTTPException(Exception):
    """Stand-in for fastapi.HTTPException: the loop only reads .detail."""

    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class FakeResponse:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.body = body


def classify(candidate_keys, verifier):
    """The exact classification logic from verify_connection."""
    results = []
    first_ok = None
    for candidate in candidate_keys:
        masked = f"{candidate[:6]}...{candidate[-4:]}" if len(candidate) > 12 else "***"
        try:
            result = verifier(candidate)
        except HTTPException as error:
            results.append({"key": masked, "ok": False, "detail": str(error.detail)[:200]})
            continue

        status_code = getattr(result, "status_code", None)
        if status_code is not None and status_code != 200:
            body = getattr(result, "body", b"")
            try:
                detail = body.decode()[:200] if isinstance(body, bytes) else str(body)[:200]
            except Exception:
                detail = "rejected"
            results.append({"key": masked, "ok": False, "detail": f"HTTP {status_code}: {detail}"})
            continue

        results.append({"key": masked, "ok": True, "detail": None})
        if first_ok is None:
            first_ok = result
    return results, first_ok


failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n         got={got!r}\n         want={want!r}"))


KEYS = ["sk-good-key-1234567890", "sk-dead-key-A-1234567890", "sk-dead-key-B-1234567890"]

print("=== a returned 401 must be counted as a failure ===")
by_key = {
    KEYS[0]: FakeResponse(200, b'{"data":[]}'),
    KEYS[1]: FakeResponse(401, b'{"error":{"message":"Invalid token"}}'),
    KEYS[2]: FakeResponse(401, b'{"error":{"message":"Invalid token"}}'),
}
results, first = classify(KEYS, lambda k: by_key[k])
check("ok flags", [r["ok"] for r in results], [True, False, False])
check("usable count", sum(1 for r in results if r["ok"]), 1)
check("failure detail mentions the code", "HTTP 401" in results[1]["detail"], True)
check("first_ok is the good response", first is by_key[KEYS[0]], True)

print("\n=== a raised HTTPException is also a failure ===")


def raiser(k):
    if k == KEYS[0]:
        return FakeResponse(200, b'{"data":[]}')
    raise HTTPException(status_code=500, detail="boom")


results, first = classify(KEYS, raiser)
check("ok flags", [r["ok"] for r in results], [True, False, False])
check("detail captured", "boom" in results[1]["detail"], True)

print("\n=== all dead -> no first_ok, so the route raises ===")
all_dead = lambda k: FakeResponse(401, b"nope")  # noqa: E731
results, first = classify(KEYS, all_dead)
check("no successes", sum(1 for r in results if r["ok"]), 0)
check("first_ok is None", first, None)

print("\n=== all good ===")
all_ok = lambda k: FakeResponse(200, b'{"data":[]}')  # noqa: E731
results, first = classify(KEYS, all_ok)
check("every key usable", [r["ok"] for r in results], [True, True, True])
check("first_ok is set", first is not None, True)

print("\n=== plain dict return (the non-response path) counts as success ===")
dict_ok = lambda k: {"data": [{"id": "m"}]}  # noqa: E731
results, first = classify(KEYS, dict_ok)
check("dict has no status_code -> ok", [r["ok"] for r in results], [True, True, True])
check("first_ok is the dict", first, {"data": [{"id": "m"}]})

print("\n=== keys are masked, never printed whole ===")
long_keys = ["sk-" + "a" * 40, "sk-" + "b" * 40]
results, _ = classify(long_keys, lambda k: FakeResponse(200))
check("masked form", results[0]["key"], "sk-aaa...aaaa")
check("no full key leaked", all(k not in r["key"] for k, r in zip(long_keys, results)), True)

print()
if failures == 0:
    print("ALL PASS")
else:
    print(f"{failures} FAILED")
    sys.exit(1)
