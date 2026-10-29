"""Unit tests for proxy rotation (multiple proxies on one connection).

Design under test: `api_config['proxies']` holds a list; one is chosen per request
so an upstream that rate-limits by source address sees several addresses. The
single `proxy` field still works and takes effect only when no list is present.

The cursor logic is reused from the API-key rotation module, but in its own state
namespace, so proxy rotation and key rotation must not interfere.

Run: python3 test-proxy-rotation.py
"""
import importlib.util
import os
import sys
import types

BACKEND = os.environ.get(
    'OWUI_BACKEND',
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'backend')),
)
if not os.path.isdir(os.path.join(BACKEND, 'open_webui')):
    BACKEND = os.path.dirname(os.path.abspath(__file__))

pkg = types.ModuleType('open_webui'); pkg.__path__ = []
sys.modules.setdefault('open_webui', pkg)
up = types.ModuleType('open_webui.utils'); up.__path__ = []
sys.modules.setdefault('open_webui.utils', up)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _path(f):
    cand = os.path.join(BACKEND, 'open_webui', 'utils', f)
    return cand if os.path.isfile(cand) else os.path.join(BACKEND, f)


# api_key_rotation must load first: proxy_config imports select_key from it.
rotation = _load('open_webui.utils.api_key_rotation', _path('api_key_rotation.py'))
pc = _load('open_webui.utils.proxy_config', _path('proxy_config.py'))

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + ('' if ok else f'\n         got={got!r}\n         want={want!r}'))


def check_true(name, cond, detail=''):
    global failures
    if not cond:
        failures += 1
        print(f"  FAIL  {name}  {detail}")
    else:
        print(f"  OK    {name}")


# The five real instabox proxies, as they would be configured.
P1 = 'socks5://ibx1:pw1@100.106.8.115:1080'
P2 = 'socks5://ibx2:pw2@100.86.76.1:1080'
P3 = 'socks5://ibx3:pw3@100.97.236.81:1080'
P4 = 'socks5://ibx4:pw4@100.74.185.111:1080'
P5 = 'socks5://ibx5:pw5@100.119.160.46:1080'
FIVE = [P1, P2, P3, P4, P5]

print("=== 1. list normalisation ===")
check(
    'list is preserved in order',
    pc.normalize_proxy_list({'proxies': FIVE}),
    FIVE,
)
check(
    'duplicates removed',
    pc.normalize_proxy_list({'proxies': [P1, P1, P2]}),
    [P1, P2],
)
check(
    'invalid entries dropped, valid ones kept',
    pc.normalize_proxy_list({'proxies': [P1, 'ftp://bad:21', P2]}),
    [P1, P2],
)
check(
    'newline-separated string accepted',
    pc.normalize_proxy_list({'proxies': f'{P1}\n{P2}'}),
    [P1, P2],
)
check(
    'comma-separated string accepted',
    pc.normalize_proxy_list({'proxies': f'{P1}, {P2}'}),
    [P1, P2],
)
check('absent -> empty', pc.normalize_proxy_list({}), [])
check('None -> empty', pc.normalize_proxy_list(None), [])
check(
    'doubled port normalised inside the list',
    pc.normalize_proxy_list({'proxies': ['socks5://u:p@h:1080:1080']}),
    ['socks5://u:p@h:1080'],
)

print("\n=== 2. round robin cycles through every proxy ===")
rotation.reset_state()
picked = [pc.resolve_proxies({'proxies': FIVE}, 'conn-A') for _ in range(10)]
check('order', picked, [P1, P2, P3, P4, P5, P1, P2, P3, P4, P5])
check('all five used', len(set(picked)), 5)

print("\n=== 3. distinct connections keep separate cursors ===")
rotation.reset_state()
a = [pc.resolve_proxies({'proxies': FIVE}, 'conn-A') for _ in range(3)]
b = [pc.resolve_proxies({'proxies': FIVE}, 'conn-B') for _ in range(3)]
check('connection A starts at P1', a, [P1, P2, P3])
check('connection B also starts at P1 (independent)', b, [P1, P2, P3])

print("\n=== 4. sticky holds one proxy for the window ===")
# `now` is injected because the window is 300s of real time; waiting is not an
# option and sleeping would make the test slow and flaky.
rotation.reset_state()
inside = [
    pc.select_proxy('conn-S', FIVE, strategy='sticky', sticky_ttl=300, now=1000 + i)
    for i in range(4)
]
check('same proxy inside the window', len(set(inside)), 1)
check('it is the first proxy', inside[0], P1)
after = [
    pc.select_proxy('conn-S', FIVE, strategy='sticky', sticky_ttl=300, now=1400 + i)
    for i in range(2)
]
check('advanced after the window', after[0], P2)
check('and holds the new one', len(set(after)), 1)

print("\n=== 5. a single proxy still works (backward compatible) ===")
rotation.reset_state()
check('legacy single proxy', pc.resolve_proxies({'proxy': P1}, 'conn-L'), P1)
check('no proxy -> direct', pc.resolve_proxies({}, 'conn-L'), '')
check('None config -> direct', pc.resolve_proxies(None, 'conn-L'), '')

print("\n=== 6. the list wins over the single field ===")
rotation.reset_state()
check(
    'proxies list takes precedence',
    pc.resolve_proxies({'proxy': P5, 'proxies': [P1, P2]}, 'conn-M'),
    P1,
)

print("\n=== 7. proxy and key rotation do not share a cursor ===")
rotation.reset_state()
keys = ['k1', 'k2', 'k3']
seq_keys = [rotation.select_key('conn-X', keys) for _ in range(3)]
seq_proxies = [pc.resolve_proxies({'proxies': FIVE}, 'conn-X') for _ in range(3)]
check('keys rotate independently', seq_keys, ['k1', 'k2', 'k3'])
check('proxies rotate independently', seq_proxies, [P1, P2, P3])
# interleaving must not perturb either sequence
rotation.reset_state()
interleaved = []
for _ in range(3):
    interleaved.append(rotation.select_key('conn-Y', keys))
    interleaved.append(pc.resolve_proxies({'proxies': FIVE}, 'conn-Y'))
check(
    'interleaving does not disturb either',
    interleaved,
    ['k1', P1, 'k2', P2, 'k3', P3],
)

print("\n=== 8. describe_proxy reports the rotation ===")
d = pc.describe_proxy({'proxies': FIVE, 'proxy_strategy': 'round_robin'})
check('configured', d['configured'], True)
check('rotation count', d['rotation']['count'], 5)
check('rotation strategy', d['rotation']['strategy'], 'round_robin')
check('all five listed', len(d['rotation']['proxies']), 5)
check_true('passwords masked', all('pw' not in p for p in d['rotation']['proxies']), d['rotation']['proxies'])
check_true('hosts visible', all('100.' in p for p in d['rotation']['proxies']), d['rotation']['proxies'])

d1 = pc.describe_proxy({'proxy': P1})
check('single proxy reports no rotation', d1['rotation'], None)
check('single proxy still configured', d1['configured'], True)

print("\n=== 9. bad strategy / ttl are clamped, never raise ===")
check('unknown strategy falls back', pc.proxy_strategy_of({'proxy_strategy': 'chaos'}), 'round_robin')
check('sticky accepted', pc.proxy_strategy_of({'proxy_strategy': 'STICKY'}), 'sticky')
check('ttl clamped high', pc.proxy_sticky_ttl_of({'proxy_sticky_ttl': 999999}), 86400)
check('ttl clamped low', pc.proxy_sticky_ttl_of({'proxy_sticky_ttl': -5}), 0)
check('ttl non-numeric', pc.proxy_sticky_ttl_of({'proxy_sticky_ttl': 'abc'}), 300)

print("\n=== 10. an all-invalid list degrades to the single field ===")
rotation.reset_state()
check(
    'invalid list falls back to proxy',
    pc.resolve_proxies({'proxy': P2, 'proxies': ['ftp://bad:21', 'nonsense://']}, 'conn-Z'),
    P2,
)
check(
    'invalid list with no single proxy -> direct',
    pc.resolve_proxies({'proxies': ['ftp://bad:21']}, 'conn-Z'),
    '',
)

print()
if failures == 0:
    print('ALL PASS')
else:
    print(f'{failures} FAILED')
    sys.exit(1)
