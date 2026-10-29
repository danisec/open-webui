"""Unit tests for api_key_rotation. Pure logic, no DB, no network."""
import sys

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "api_key_rotation",
    "/home/dani/AI-agent/open-webui/backend/open_webui/utils/api_key_rotation.py",
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

DEFAULT_STICKY_TTL = _m.DEFAULT_STICKY_TTL
STRATEGY_ROUND_ROBIN = _m.STRATEGY_ROUND_ROBIN
STRATEGY_STICKY = _m.STRATEGY_STICKY
describe = _m.describe
normalize_key_list = _m.normalize_key_list
reset_state = _m.reset_state
resolve_keys = _m.resolve_keys
select_key = _m.select_key
sticky_ttl_of = _m.sticky_ttl_of
strategy_of = _m.strategy_of

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n         got={got!r}\n         want={want!r}"))


print("=== normalize_key_list ===")
check("comma string", normalize_key_list("a,b,c"), ["a", "b", "c"])
check("newline string", normalize_key_list("a\nb"), ["a", "b"])
check("mixed separators", normalize_key_list("a, b\nc"), ["a", "b", "c"])
check("list input", normalize_key_list(["a", "b"]), ["a", "b"])
check("strips whitespace", normalize_key_list([" a ", "b "]), ["a", "b"])
check("drops blanks", normalize_key_list("a,,b,  ,c"), ["a", "b", "c"])
check("dedupes preserving order", normalize_key_list("b,a,b,a"), ["b", "a"])
check("None -> empty", normalize_key_list(None), [])
check("number -> empty", normalize_key_list(42), [])
check("empty string -> empty", normalize_key_list(""), [])
check("non-string items dropped", normalize_key_list(["a", 1, None, "b"]), ["a", "b"])

print("\n=== resolve_keys: list wins over legacy ===")
check("list used", resolve_keys({"api_keys": ["k1", "k2"]}, "legacy"), ["k1", "k2"])
check("legacy used when no list", resolve_keys({}, "legacy"), ["legacy"])
check("legacy used when list empty", resolve_keys({"api_keys": []}, "legacy"), ["legacy"])
check(
    "legacy NOT appended to a list",
    resolve_keys({"api_keys": ["k1"]}, "legacy"),
    ["k1"],
)
check("no keys at all", resolve_keys({}, ""), [])
check("none config", resolve_keys(None, "x"), ["x"])

print("\n=== strategy_of ===")
check("default", strategy_of({}), STRATEGY_ROUND_ROBIN)
check("explicit rr", strategy_of({"api_key_strategy": "round_robin"}), STRATEGY_ROUND_ROBIN)
check("explicit sticky", strategy_of({"api_key_strategy": "sticky"}), STRATEGY_STICKY)
check("unknown -> default", strategy_of({"api_key_strategy": "wat"}), STRATEGY_ROUND_ROBIN)
check("non-string -> default", strategy_of({"api_key_strategy": 5}), STRATEGY_ROUND_ROBIN)

print("\n=== sticky_ttl_of ===")
check("default", sticky_ttl_of({}), DEFAULT_STICKY_TTL)
check("explicit", sticky_ttl_of({"sticky_ttl": 60}), 60)
check("string number", sticky_ttl_of({"sticky_ttl": "120"}), 120)
check("garbage -> default", sticky_ttl_of({"sticky_ttl": "abc"}), DEFAULT_STICKY_TTL)
check("negative clamped to 0", sticky_ttl_of({"sticky_ttl": -5}), 0)
check("huge clamped to a day", sticky_ttl_of({"sticky_ttl": 10**9}), 86400)

print("\n=== select_key: round robin ===")
reset_state()
keys = ["k1", "k2", "k3"]
seq = [select_key("conn", keys, STRATEGY_ROUND_ROBIN, now=1000 + i) for i in range(7)]
check("cycles in order", seq, ["k1", "k2", "k3", "k1", "k2", "k3", "k1"])

reset_state()
check("single key short-circuits", select_key("c", ["only"], STRATEGY_ROUND_ROBIN), "only")
check("no keys -> empty string", select_key("c", [], STRATEGY_ROUND_ROBIN), "")

reset_state()
a = [select_key("A", keys, STRATEGY_ROUND_ROBIN, now=1) for _ in range(3)]
b = [select_key("B", keys, STRATEGY_ROUND_ROBIN, now=1) for _ in range(3)]
check("per-connection counters are independent", (a, b), (["k1", "k2", "k3"], ["k1", "k2", "k3"]))

print("\n=== select_key: sticky ===")
reset_state()
# Within the window every call returns the same key.
s = [select_key("s", keys, STRATEGY_STICKY, sticky_ttl=300, now=1000 + i) for i in range(5)]
check("same key inside the window", s, ["k1"] * 5)

# After the window it advances to the next key, then sticks again.
s2 = [select_key("s", keys, STRATEGY_STICKY, sticky_ttl=300, now=1400 + i) for i in range(3)]
check("advances after TTL", s2, ["k2"] * 3)

s3 = [select_key("s", keys, STRATEGY_STICKY, sticky_ttl=300, now=1800 + i) for i in range(3)]
check("advances again", s3, ["k3"] * 3)

reset_state()
s4 = [select_key("s2", keys, STRATEGY_STICKY, sticky_ttl=300, now=2000 + i) for i in range(3)]
check("wraps to k1 after k3", s4, ["k1"] * 3)

reset_state()
z = [select_key("z", keys, STRATEGY_STICKY, sticky_ttl=0, now=3000 + i) for i in range(4)]
check("ttl=0 behaves like round robin", z, ["k1", "k2", "k3", "k1"])

print("\n=== state isolation between strategies ===")
reset_state()
r1 = select_key("mix", keys, STRATEGY_ROUND_ROBIN, now=5000)
st = select_key("mix", keys, STRATEGY_STICKY, sticky_ttl=300, now=5000)
r2 = select_key("mix", keys, STRATEGY_ROUND_ROBIN, now=5001)
check("round robin continues its own counter", (r1, r2), ("k1", "k2"))
# Sticky keeps its own cursor, so it starts from k1 rather than inheriting the
# round-robin position. Sharing one cursor would make the two strategies skip keys
# whenever they were mixed.
check("sticky has its own cursor", st, "k1")

print("\n=== describe ===")
d = describe({"api_keys": ["sk-abcdefghijklmnop", "sk-2"], "api_key_strategy": "sticky"}, "legacy")
check("key_count", d["key_count"], 2)
check("strategy", d["strategy"], "sticky")
check("masks long keys", d["masked"][0].startswith("sk-abc"), True)
check("masks short keys fully", d["masked"][1], "***")

print()
if failures == 0:
    print("ALL PASS")
else:
    print(f"{failures} FAILED")
    sys.exit(1)
