"""Unit tests for per-connection proxy support.

Two modules are covered:

  proxy_config      -- URL normalisation, validation, masking, request kwargs
  session_pool      -- one cached session per distinct proxy

The session_pool tests run against a fake aiohttp so they need no network. What
they pin is the property that matters: different proxies must not share a
session, and the no-proxy call must keep returning the same object as before,
because 57 existing call sites rely on that.

Run: python3 test-proxy-support.py
"""
import importlib.util
import sys
import types

import os
_here = os.path.dirname(os.path.abspath(__file__))
# Works from the repo (docs/architecture/tests -> backend) and from /tmp where
# the two modules are copied side by side for a container run.
BACKEND = os.environ.get(
    'OWUI_BACKEND',
    os.path.abspath(os.path.join(_here, '..', '..', '..', 'backend')),
)
if not os.path.isdir(os.path.join(BACKEND, 'open_webui')):
    BACKEND = _here

# Load the two modules directly instead of importing the `open_webui` package:
# its __init__ pulls in the full application dependency set (typer, fastapi...),
# which is not installed on a bare host. The modules under test only need
# aiohttp, which is installed everywhere this feature runs.
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# session_pool imports `open_webui.env` and `open_webui.utils.misc`; stub just
# enough of that namespace so the module can load in isolation.
pkg = types.ModuleType('open_webui')
pkg.__path__ = []
sys.modules.setdefault('open_webui', pkg)
utils_pkg = types.ModuleType('open_webui.utils')
utils_pkg.__path__ = []
sys.modules.setdefault('open_webui.utils', utils_pkg)

env_stub = types.ModuleType('open_webui.env')
env_stub.AIOHTTP_CLIENT_TIMEOUT = 300
env_stub.AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT = None
env_stub.AIOHTTP_POOL_CONNECTIONS = 100
env_stub.AIOHTTP_POOL_CONNECTIONS_PER_HOST = 30
env_stub.AIOHTTP_POOL_DNS_TTL = 300
sys.modules.setdefault('open_webui.env', env_stub)

misc_stub = types.ModuleType('open_webui.utils.misc')
async def _noop_stream(content):
    async for chunk in content:
        yield chunk
misc_stub.stream_chunks_handler = _noop_stream
sys.modules.setdefault('open_webui.utils.misc', misc_stub)

def _module_path(name):
    candidate = os.path.join(BACKEND, 'open_webui', 'utils', name)
    if os.path.isfile(candidate):
        return candidate
    return os.path.join(BACKEND, name)


proxy_config = load_module('open_webui.utils.proxy_config', _module_path('proxy_config.py'))
session_pool = load_module('open_webui.utils.session_pool', _module_path('session_pool.py'))

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"         got  = {got!r}")
            print(f"         want = {want!r}")
    else:
        print(f"  OK    {name}")


def check_true(name, cond, detail=''):
    global failures
    if not cond:
        failures += 1
        print(f"  FAIL  {name}  {detail}")
    else:
        print(f"  OK    {name}")


# --------------------------------------------------------------------------
# proxy_config: normalisation
# --------------------------------------------------------------------------
ProxyConfigError = proxy_config.ProxyConfigError
describe_proxy = proxy_config.describe_proxy
is_socks = proxy_config.is_socks
mask_proxy = proxy_config.mask_proxy
normalize_proxy = proxy_config.normalize_proxy
proxy_from_config = proxy_config.proxy_from_config
request_proxy_kwargs = proxy_config.request_proxy_kwargs

print("=== 1. normalisation of real-world inputs ===")

USER_PROXY = 'ibx1:D8zFWhTPfFUTzW5GyNG7qGyF4SL4@100.106.8.115:1080:1080'
check(
    'doubled port is collapsed (the value as pasted)',
    normalize_proxy('socks5://' + USER_PROXY),
    'socks5://ibx1:D8zFWhTPfFUTzW5GyNG7qGyF4SL4@100.106.8.115:1080',
)
check(
    'bare user:pass@host:port assumes socks5',
    normalize_proxy(USER_PROXY),
    'socks5://ibx1:D8zFWhTPfFUTzW5GyNG7qGyF4SL4@100.106.8.115:1080',
)
check('socks5h is preserved', normalize_proxy('socks5h://h:1080'), 'socks5h://h:1080')
check('http proxy preserved', normalize_proxy('http://proxy:8080'), 'http://proxy:8080')
check('empty stays empty', normalize_proxy(''), '')
check('None stays empty', normalize_proxy(None), '')
check('whitespace trimmed', normalize_proxy('  socks5://h:1080  '), 'socks5://h:1080')
check(
    'default SOCKS port applied when absent',
    normalize_proxy('socks5://h'),
    'socks5://h:1080',
)
check(
    'default HTTP port applied when absent',
    normalize_proxy('http://h'),
    'http://h:8080',
)

print("\n=== 2. credentials with special characters ===")
check(
    'raw @ in password is encoded',
    normalize_proxy('socks5://u:p@ss@host:1080'),
    'socks5://u:p%40ss@host:1080',
)
check(
    'colon in password is encoded',
    normalize_proxy('socks5://u:p:ss@host:1080'),
    'socks5://u:p%3Ass@host:1080',
)

print("\n=== 3. invalid values raise an actionable error ===")
for bad, why in (
    ('ftp://h:21', 'unsupported scheme'),
    ('socks5://', 'no host'),
    ('socks5://h:99999', 'port out of range'),
):
    try:
        normalize_proxy(bad)
        check(f'{why} rejected', 'no error', 'ProxyConfigError')
    except ProxyConfigError as e:
        check_true(f'{why} rejected', len(str(e)) > 5, str(e))

print("\n=== 4. masking never leaks the password ===")
masked = mask_proxy('socks5://ibx1:SECRETVALUE@100.106.8.115:1080')
check_true('password absent', 'SECRETVALUE' not in masked, masked)
check_true('host kept', '100.106.8.115' in masked, masked)
check_true('user kept', 'ibx1' in masked, masked)
check('mask of empty', mask_proxy(''), '')

print("\n=== 5. socks vs http detection drives request kwargs ===")
SOCKS = 'socks5://u:p@h:1080'
HTTP = 'http://u:p@h:8080'
check('socks detected', is_socks(SOCKS), True)
check('http not socks', is_socks(HTTP), False)
check('socks needs no per-request proxy kwarg', request_proxy_kwargs(SOCKS), {})
check('http needs proxy kwarg', request_proxy_kwargs(HTTP), {'proxy': HTTP})
check('no proxy -> no kwargs', request_proxy_kwargs(''), {})
check('None -> no kwargs', request_proxy_kwargs(None), {})

print("\n=== 6. proxy_from_config degrades instead of raising ===")
check('absent key', proxy_from_config({}), '')
check('None config', proxy_from_config(None), '')
check(
    'valid value normalised',
    proxy_from_config({'proxy': 'ibx1:pw@h:1080:1080'}),
    'socks5://ibx1:pw@h:1080',
)
check(
    'invalid value becomes empty, not an exception',
    proxy_from_config({'proxy': 'ftp://h:21'}),
    '',
)

print("\n=== 7. describe_proxy reports the problem for the UI ===")
d = describe_proxy({'proxy': 'socks5://u:pw@h:1080'})
check('configured', d['configured'], True)
check('normalized', d['normalized'], 'socks5://u:pw@h:1080')
check('no error', d['error'], None)
check_true('raw masked', 'pw' not in d['raw_masked'] or '***' in d['raw_masked'], d['raw_masked'])

bad = describe_proxy({'proxy': 'ftp://h:21'})
check('bad configured', bad['configured'], True)
check_true('bad has error', bool(bad['error']), str(bad['error']))

check('not configured', describe_proxy({})['configured'], False)

print("\n=== 7b. regression: a doubled port must not crash describe_proxy ===")
# Found on the live instance: /openai/verify returned HTTP 500 because
# urlsplit().port raises ValueError for "1080:1080" and _mask did not catch it.
# describe_proxy is called on every verify, so this broke the whole endpoint.
for raw in (
    'socks5://ibx1:pw@100.106.8.115:1080:1080',
    'socks5://u:p@h:1080:1080',
    'socks5://u:p@h:notaport',
    'http://h:1:2',
):
    try:
        out = describe_proxy({'proxy': raw})
        check_true(f'no exception for {raw[:34]}', True)
        check_true(
            f'password masked for {raw[:34]}',
            'pw' not in out['raw_masked'] and 'p@' not in out['raw_masked'],
            out['raw_masked'],
        )
    except Exception as error:
        check_true(f'no exception for {raw[:34]}', False, f'{type(error).__name__}: {error}')

check_true(
    'doubled port normalises to a single port',
    describe_proxy({'proxy': 'socks5://ibx1:pw@h:1080:1080'})['normalized'].endswith(':1080'),
)
check(
    'masking a malformed port degrades instead of raising',
    mask_proxy('socks5://u:p@h:abc'),
    'socks5://u:***@h',
)

# --------------------------------------------------------------------------
# session_pool: one session per proxy
# --------------------------------------------------------------------------
print("\n=== 8. session pool keeps one session per proxy ===")

import asyncio  # noqa: E402

sp = session_pool


class FakeConnector:
    def __init__(self, proxy=None, **kw):
        self.proxy = proxy
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, connector, timeout=None, trust_env=True):
        self.connector = connector
        self.timeout = timeout
        self.trust_env = trust_env
        self.closed = False

    def close(self):
        self.closed = True


created = []


def fake_tcp(**kw):
    c = FakeConnector(None, **kw)
    created.append(c)
    return c


class FakeProxyConnector:
    @staticmethod
    def from_url(url, **kw):
        c = FakeConnector(url, **kw)
        created.append(c)
        return c


sp.aiohttp.TCPConnector = fake_tcp
sp.aiohttp.ClientSession = FakeSession

# Inject a fake aiohttp_socks module so the SOCKS branch can be exercised.
import types  # noqa: E402

fake_socks = types.ModuleType('aiohttp_socks')
fake_socks.ProxyConnector = FakeProxyConnector
sys.modules['aiohttp_socks'] = fake_socks


async def pool_tests():
    sp._sessions.clear()
    sp._locks.clear()

    direct_a = await sp.get_session()
    direct_b = await sp.get_session()
    check('direct session reused', direct_a is direct_b, True)
    check('direct trusts env (unchanged behaviour)', direct_a.trust_env, True)

    socks = 'socks5://u:p@h:1080'
    s1 = await sp.get_session(socks)
    s2 = await sp.get_session(socks)
    check('same proxy -> same session', s1 is s2, True)
    check('proxy session does not trust env', s1.trust_env, False)
    check_true('socks connector used', s1.connector.proxy == socks, str(s1.connector.proxy))

    other = 'socks5://u:p@h2:1080'
    s3 = await sp.get_session(other)
    check_true('different proxy -> different session', s3 is not s1)
    check_true('different proxy -> different connector', s3.connector is not s1.connector)

    http = 'http://u:p@h:8080'
    s4 = await sp.get_session(http)
    check_true('http proxy gets its own session', s4 is not s1 and s4 is not direct_a)
    check_true('http proxy uses a plain TCP connector', s4.connector.proxy is None)

    check('distinct sessions cached', len(sp._sessions), 4)

    # equivalent spellings must share one session
    sp._sessions.clear()
    sp._locks.clear()
    a = await sp.get_session('socks5://u:p@h:1080')
    b = await sp.get_session('  socks5://u:p@h:1080  ')
    c = await sp.get_session('socks5://u:p@h:1080:1080')
    check_true('normalised spellings share a session', a is b is c)
    check('only one entry after normalisation', len(sp._sessions), 1)

    # concurrent first use must not create two sessions
    sp._sessions.clear()
    sp._locks.clear()
    results = await asyncio.gather(*[sp.get_session('socks5://u:p@h:1080') for _ in range(8)])
    check('concurrent creation yields one session', len({id(r) for r in results}), 1)

    await sp.close_session()
    check('all sessions closed', len(sp._sessions), 0)


asyncio.run(pool_tests())

print("\n=== 9. a SOCKS proxy without the library fails loudly ===")
# Run in a subprocess: by this point the test has replaced aiohttp.TCPConnector
# with a fake, and aiohttp_socks subclasses the real one at import time, so
# importing it here would fail for the wrong reason.
import subprocess  # noqa: E402
import textwrap  # noqa: E402

probe = textwrap.dedent(
    """
    import asyncio, sys, types
    sys.modules['aiohttp_socks'] = None  # make the import fail as if uninstalled

    # session_pool imports open_webui.env and open_webui.utils.misc; stub the
    # package so the probe exercises only the missing-SOCKS path.
    pkg = types.ModuleType('open_webui'); pkg.__path__ = []
    sys.modules['open_webui'] = pkg
    up = types.ModuleType('open_webui.utils'); up.__path__ = []
    sys.modules['open_webui.utils'] = up
    env = types.ModuleType('open_webui.env')
    env.AIOHTTP_CLIENT_TIMEOUT = 300
    env.AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT = None
    env.AIOHTTP_POOL_CONNECTIONS = 100
    env.AIOHTTP_POOL_CONNECTIONS_PER_HOST = 30
    env.AIOHTTP_POOL_DNS_TTL = 300
    sys.modules['open_webui.env'] = env
    misc = types.ModuleType('open_webui.utils.misc')
    async def _s(content):
        async for c in content:
            yield c
    misc.stream_chunks_handler = _s
    sys.modules['open_webui.utils.misc'] = misc

    import importlib.util
    def _load(name, path):
        s = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(s)
        sys.modules[name] = m
        s.loader.exec_module(m)
        return m

    _load('open_webui.utils.proxy_config', sys.argv[2])
    sp = _load('sp_probe', sys.argv[1])

    async def main():
        try:
            await sp.get_session('socks5://u:p@h:1080')
            print('NO-ERROR')
        except RuntimeError as e:
            print('RUNTIME-ERROR:' + str(e))
        except Exception as e:
            print('OTHER:' + type(e).__name__ + ':' + str(e))

    asyncio.run(main())
    """
)
result = subprocess.run(
    [
        sys.executable,
        '-c',
        probe,
        os.path.join(BACKEND, 'session_pool.py'),
        os.path.join(BACKEND, 'proxy_config.py'),
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
out = (result.stdout or '').strip().splitlines()
last = out[-1] if out else ''
check_true(
    'missing aiohttp-socks raises RuntimeError',
    last.startswith('RUNTIME-ERROR'),
    last or (result.stderr or '')[-200:],
)
check_true('error names the dependency', 'aiohttp-socks' in last, last)

print()
if failures == 0:
    print('ALL PASS')
else:
    print(f'{failures} FAILED')
    sys.exit(1)
