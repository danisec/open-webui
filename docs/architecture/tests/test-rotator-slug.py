"""Unit tests for the rotator's upstream slug and candidate-prefix behaviour.

Two bugs this pins:

1. `_upstream_slug()` used only the first DNS label, so api.howtofix.id and
   api.sakoo.biz.id both became "api". Upstreams are stored in a dict keyed by
   name, so whichever connection registered last silently owned the other's
   candidates -- `@api/<model>` pointed at an upstream the admin never chose.

2. `removed` is compared by BARE model id, so a `removed` entry written as
   `@api/deepseek-v4.1-flash-1017` also blocked `@howtofix/deepseek-v4.1-flash-1017`.
   Every re-add through the UI was dropped by the reconciler within two minutes,
   leaving the alias with zero candidates and a 404 for the user.

Run: python3 test-rotator-slug.py
"""
import os
import sys
from urllib.parse import urlparse

ROTATOR = os.environ.get(
    'OWUI_ROTATOR',
    os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', '..', '..',
            'backend', 'open_webui', 'routers', 'rotator.py',
        )
    ),
)
if not os.path.isfile(ROTATOR):
    ROTATOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rotator.py')

src = open(ROTATOR).read()
start = src.index('def _upstream_slug')
end = src.index('async def _connection_catalog')
ns = {}
exec('from urllib.parse import urlparse\n' + src[start:end], ns)
slug = ns['_upstream_slug']

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + ('' if ok else f'\n         got={got!r}\n         want={want!r}'))


print('=== 1. the real connections must get distinct slugs ===')
REAL = [
    ('https://api.howtofix.id/v1', 'api-howtofix'),
    ('https://api.sakoo.biz.id/v1', 'sakoo-biz'),
    ('http://9router-lb:8899/v1', '9router-lb'),
    ('http://model-rotator:8891/v1', 'model-rotator'),
    ('http://100.100.89.87:8788/v1', '1001008987'),
    ('https://bansosin.space/v1', 'bansosin'),
]
for url, want in REAL:
    check(url, slug(url, 0), want)

slugs = [slug(u, i) for i, (u, _) in enumerate(REAL)]
check('all six are distinct', len(set(slugs)), len(slugs))

print('\n=== 2. the specific collision that broke the alias ===')
a = slug('https://api.howtofix.id/v1', 2)
b = slug('https://api.sakoo.biz.id/v1', 3)
check('howtofix and sakoo differ', a != b, True)
check('neither is the bare "api" that caused the clash', 'api' in (a, b), False)

print('\n=== 3. edge cases ===')
check('single label', slug('http://localhost:8080/v1', 0), 'localhost')
check('two labels', slug('https://example.com/v1', 0), 'example')
check('deep subdomain keeps last three labels', slug('https://xcartethyia.risun.web.id/v1', 0), 'risun-web')
check('IPv4 keeps its digits', slug('http://10.0.0.1:8000/v1', 0), '10001')
check('no host falls back to conn index', slug('', 7), 'conn7')
check('unusual characters stripped', slug('https://my_host-1.example.com/v1', 0), 'my_host-1-example')

print('\n=== 4. slug is stable for the same URL ===')
for url, _ in REAL:
    check(f'stable: {url[:30]}', slug(url, 0), slug(url, 5))

print('\n=== 5. bare-id comparison: why `removed` blocked the wrong candidate ===')


def bare_id(c):
    if isinstance(c, str) and c.startswith('@'):
        name, _, mid = c[1:].partition('/')
        if name and mid:
            return mid
    return c


removed = ['@api/deepseek-v4.1-flash-1017']
candidates = [
    '@api/deepseek-v4.1-flash-1017',
    '@howtofix/deepseek-v4.1-flash-1017',
    '@100/deepseek-v4.1-flash-1017',
]
blocked = {c: bare_id(c) in {bare_id(r) for r in removed} for c in candidates}
check(
    'one removed entry blocks every upstream for that model id',
    blocked,
    {
        '@api/deepseek-v4.1-flash-1017': True,
        '@howtofix/deepseek-v4.1-flash-1017': True,
        '@100/deepseek-v4.1-flash-1017': True,
    },
)
check(
    'a dotted entry is a different bare id (the other blocker that appeared)',
    bare_id('howtofix.deepseek-v4.1-flash-1017'),
    'howtofix.deepseek-v4.1-flash-1017',
)

print()
if failures == 0:
    print('ALL PASS')
else:
    print(f'{failures} FAILED')
    sys.exit(1)
